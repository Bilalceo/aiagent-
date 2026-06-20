"""Real streaming STT adapter for Deepgram, behind the StreamingSTTProvider
interface (A29).

Design goals:
- Same interface as MockStreamingSTTProvider (drop-in via STREAMING_STT_PROVIDER).
- Testable WITHOUT any network: a small DeepgramConnection / DeepgramConnector
  protocol is dependency-injected; unit tests use a fake connection. The real
  adapter (WebsocketsDeepgramConnector) lazy-imports `websockets` (opt-in extra).
- Safe: raw audio is sent to the connection only; it is NEVER logged/persisted.
  The API key lives only in a connect header - never in a URL, log, or metadata.
- Robust: connect/send/receive failures degrade the session (the session service
  marks it degraded) and never crash the Twilio WebSocket. Close is best-effort.

Transcript flow: interim Deepgram results -> partial TranscriptEvent (drives
barge-in); final results -> final TranscriptEvent (drives the AI turn). Empty
transcripts are ignored. Finals get a monotonic local event_id for dedup.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from abc import ABC, abstractmethod
from typing import Optional

from app.services.voice.streaming_stt import (
    StreamingContext,
    StreamingSTTProvider,
    StreamingSTTSession,
    TranscriptEvent,
    _detect_language,
)

_DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
_DRAIN_CAP = 64  # max messages drained per call (bounds the loop)


# --- connection protocol (injectable) --------------------------------------
class DeepgramConnection(ABC):
    """One open streaming connection to Deepgram (audio out, transcripts in)."""

    @abstractmethod
    async def send_audio(self, data: bytes) -> None:
        raise NotImplementedError

    @abstractmethod
    async def recv(self, *, timeout: float) -> Optional[str]:
        """Return the next available message text, or None when none is ready."""
        raise NotImplementedError

    @abstractmethod
    async def finish(self) -> None:
        """Signal end-of-audio (CloseStream) so the provider flushes a final."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class DeepgramConnector(ABC):
    @abstractmethod
    async def connect(self, *, url: str, headers: dict) -> DeepgramConnection:
        raise NotImplementedError


# --- transcript parsing (pure, never raises) -------------------------------
def parse_deepgram_message(raw: str, *, max_chars: int = 2000) -> Optional[dict]:
    """Parse a Deepgram streaming message into {text, is_final, confidence}.

    Returns None for non-transcript messages (Metadata/SpeechStarted/UtteranceEnd),
    malformed JSON, or empty transcripts. Never raises."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    msg_type = data.get("type")
    if msg_type not in (None, "Results"):
        return None  # ignore Metadata / SpeechStarted / UtteranceEnd / errors
    channel = data.get("channel")
    if not isinstance(channel, dict):
        return None
    alts = channel.get("alternatives")
    if not isinstance(alts, list) or not alts or not isinstance(alts[0], dict):
        return None
    text = str(alts[0].get("transcript") or "").strip()
    if not text:
        return None  # ignore empty transcripts
    if len(text) > max_chars:
        text = text[:max_chars]
    conf = alts[0].get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) else None
    return {"text": text, "is_final": bool(data.get("is_final")), "confidence": confidence}


# --- session ----------------------------------------------------------------
class DeepgramStreamingSession(StreamingSTTSession):
    """Connects lazily on the first audio frame; drains available transcripts after
    each send. Any failure raises StreamingSTTError-style (the session service then
    marks the session degraded); the connection is best-effort closed on failure."""

    def __init__(
        self,
        context: StreamingContext,
        *,
        connector: DeepgramConnector,
        api_key: str,
        model: str,
        language: str,
        encoding: str,
        sample_rate: int,
        interim_results: bool,
        endpointing: str,
        connect_timeout: float,
        recv_timeout: float,
        max_chars: int,
    ) -> None:
        self._ctx = context
        self._connector = connector
        self._api_key = api_key  # connect header only; never logged/persisted
        self._model = model
        self._language = language
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._interim = interim_results
        self._endpointing = endpointing
        self._connect_timeout = connect_timeout
        self._recv_timeout = recv_timeout
        self._max_chars = max_chars
        self._conn: Optional[DeepgramConnection] = None
        self._closed = False
        self._final_seq = 0
        self._token = context.stream_sid or "dg-stream"

    def _build_url(self) -> str:
        params = [
            f"model={self._model}",
            f"encoding={self._encoding}",
            f"sample_rate={self._sample_rate}",
            f"interim_results={'true' if self._interim else 'false'}",
        ]
        if self._language:
            params.append(f"language={self._language}")
        if self._endpointing:
            params.append(f"endpointing={self._endpointing}")
        return f"{_DEEPGRAM_URL}?{'&'.join(params)}"

    async def _ensure_connected(self) -> DeepgramConnection:
        if self._conn is not None:
            return self._conn
        # The API key travels ONLY in the Authorization header (never in the URL).
        headers = {"Authorization": f"Token {self._api_key}"}
        self._conn = await asyncio.wait_for(
            self._connector.connect(url=self._build_url(), headers=headers),
            timeout=self._connect_timeout,
        )
        return self._conn

    async def _drain(self) -> list[TranscriptEvent]:
        out: list[TranscriptEvent] = []
        if self._conn is None:
            return out
        for _ in range(_DRAIN_CAP):
            msg = await self._conn.recv(timeout=self._recv_timeout)
            if msg is None:
                break
            parsed = parse_deepgram_message(msg, max_chars=self._max_chars)
            if parsed is None:
                continue
            if parsed["is_final"]:
                out.append(self._event(parsed, is_final=True))
            elif self._interim:
                out.append(self._event(parsed, is_final=False))
        return out

    def _event(self, parsed: dict, *, is_final: bool) -> TranscriptEvent:
        event_id = None
        if is_final:
            event_id = f"{self._token}:dg:{self._final_seq}"
            self._final_seq += 1
        return TranscriptEvent(
            text=parsed["text"],
            language=_detect_language(parsed["text"]),
            is_final=is_final,
            provider="deepgram",
            confidence=parsed.get("confidence"),
            timestamp_ms=None,
            metadata={"provider": "deepgram", "model": self._model, "is_final": is_final},
            event_id=event_id,
        )

    async def accept_audio_frame(self, frame) -> list[TranscriptEvent]:
        try:
            conn = await self._ensure_connected()
            await conn.send_audio(frame.payload_bytes or b"")
            return await self._drain()
        except Exception:
            await self._safe_close()
            raise  # session service marks the session degraded

    async def finish_stream(self) -> list[TranscriptEvent]:
        if self._conn is None:
            return []
        try:
            await self._conn.finish()
            return await self._drain()
        except Exception:
            await self._safe_close()
            return []

    async def close(self) -> None:
        await self._safe_close()

    async def _safe_close(self) -> None:
        if self._closed or self._conn is None:
            self._closed = True
            return
        self._closed = True
        try:
            await self._conn.close()
        except Exception:
            pass


# --- provider ---------------------------------------------------------------
class DeepgramStreamingSTTProvider(StreamingSTTProvider):
    name = "deepgram"

    def __init__(
        self,
        *,
        api_key: str,
        connector: Optional[DeepgramConnector] = None,
        model: str = "nova-2",
        language: str = "",
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        interim_results: bool = True,
        endpointing: str = "",
        connect_timeout: float = 5.0,
        recv_timeout: float = 0.05,
        max_message_bytes: int = 1_000_000,
        max_chars: int = 2000,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramStreamingSTTProvider requires an api_key")
        self._api_key = api_key
        self._connector = connector or WebsocketsDeepgramConnector(
            connect_timeout=connect_timeout,
            recv_timeout=recv_timeout,
            max_message_bytes=max_message_bytes,
        )
        self._model = model
        self._language = language
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._interim = interim_results
        self._endpointing = endpointing
        self._connect_timeout = connect_timeout
        self._recv_timeout = recv_timeout
        self._max_chars = max_chars

    def start_stream(self, context: StreamingContext) -> StreamingSTTSession:
        return DeepgramStreamingSession(
            context,
            connector=self._connector,
            api_key=self._api_key,
            model=self._model,
            language=self._language,
            encoding=self._encoding,
            sample_rate=self._sample_rate,
            interim_results=self._interim,
            endpointing=self._endpointing,
            connect_timeout=self._connect_timeout,
            recv_timeout=self._recv_timeout,
            max_chars=self._max_chars,
        )


# --- real connector (production only; opt-in `websockets`) ------------------
def _header_kwarg_name(connect_fn) -> str:
    """Pick the websockets connect header kwarg by signature.

    websockets >= 14 (modern asyncio client) uses `additional_headers`; older
    versions use `extra_headers`. Default to the modern name. This keeps the adapter
    working across the supported range without pinning a single version."""
    try:
        params = inspect.signature(connect_fn).parameters
    except (TypeError, ValueError):
        return "additional_headers"
    if "extra_headers" in params and "additional_headers" not in params:
        return "extra_headers"
    return "additional_headers"


class WebsocketsDeepgramConnector(DeepgramConnector):
    """Real connector using the `websockets` package (opt-in extra). Never used by
    tests (which inject `connect_fn`). Lazy-imports so the default install/test run
    needs no extra dependency. The API key is passed ONLY as an Authorization
    header (never in the URL)."""

    def __init__(
        self,
        *,
        connect_timeout: float,
        recv_timeout: float,
        max_message_bytes: int,
        connect_fn=None,  # injectable for tests (a websockets.connect-like callable)
        closed_exc=None,  # the provider's "connection closed" exception class
    ) -> None:
        self._connect_timeout = connect_timeout
        self._recv_timeout = recv_timeout
        self._max_message_bytes = max_message_bytes
        self._connect_fn = connect_fn
        self._closed_exc = closed_exc

    async def connect(self, *, url: str, headers: dict) -> DeepgramConnection:
        connect_fn = self._connect_fn
        closed_exc = self._closed_exc
        if connect_fn is None:
            try:
                import websockets  # lazy; `pip install -e ".[stt-streaming]"`
            except Exception as exc:  # missing optional dependency -> clear, no secret
                raise RuntimeError(
                    "STREAMING_STT_PROVIDER=deepgram needs the 'websockets' package "
                    "(pip install -e '.[stt-streaming]')"
                ) from exc
            connect_fn = websockets.connect
            closed_exc = getattr(getattr(websockets, "exceptions", None), "ConnectionClosed", None)
        # Use the header kwarg the installed websockets version actually accepts.
        hdr = {_header_kwarg_name(connect_fn): list(headers.items())}
        ws = await connect_fn(url, max_size=self._max_message_bytes, **hdr)
        return _WebsocketsConnection(ws, self._recv_timeout, closed_exc=closed_exc)


class _WebsocketsConnection(DeepgramConnection):
    def __init__(self, ws, recv_timeout: float, *, closed_exc=None) -> None:
        self._ws = ws
        self._recv_timeout = recv_timeout
        self._closed_exc = closed_exc

    async def send_audio(self, data: bytes) -> None:
        await self._ws.send(data)

    async def recv(self, *, timeout: float) -> Optional[str]:
        try:
            msg = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return None  # no message available now (non-blocking drain)
        except Exception as exc:
            # A normal connection-closed ends the drain safely; any OTHER receive /
            # protocol error propagates so the session marks itself degraded.
            if self._closed_exc is not None and isinstance(exc, self._closed_exc):
                return None
            raise
        return msg.decode("utf-8") if isinstance(msg, (bytes, bytearray)) else str(msg)

    async def finish(self) -> None:
        await self._ws.send(json.dumps({"type": "CloseStream"}))

    async def close(self) -> None:
        await self._ws.close()
