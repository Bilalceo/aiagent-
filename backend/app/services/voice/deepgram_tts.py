"""Real streaming TTS adapter for Deepgram, behind the StreamingTTSProvider
interface (A30).

Design goals (mirror the A29 Deepgram STT adapter):
- Same interface as MockStreamingTTSProvider (drop-in via STREAMING_TTS_PROVIDER).
  `synthesize(text, *, language, voice) -> bytes` returns RAW audio bytes; the
  caller (TwilioPlaybackService) still owns chunking + base64 of outbound Twilio
  media payloads, so there is no double-base64 and no container/WAV header.
- Testable WITHOUT any network: a small DeepgramTTSConnection / DeepgramTTSConnector
  protocol is dependency-injected; unit tests use a fake connection. The real
  adapter (WebsocketsDeepgramTTSConnector) lazy-imports `websockets` (opt-in extra).
- Safe: the API key lives only in a connect header - never in a URL, log, metadata,
  or exception. Raw audio bytes are returned to the caller but NEVER logged/persisted.
- Robust: connect/send/flush/receive failures RAISE so TwilioPlaybackService marks
  the playback degraded (error="tts_error") and the Twilio WebSocket never crashes.
  The connection is best-effort closed in every path.

Twilio compatibility: encoding=mulaw, sample_rate=8000, container=none give RAW
8k mu-law frames Twilio Media Streams can play directly (no WAV/RIFF header).
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Optional

from app.services.voice.deepgram_stt import _header_kwarg_name
from app.services.voice.streaming_tts import StreamingTTSProvider

_DEEPGRAM_TTS_URL = "wss://api.deepgram.com/v1/speak"
_DRAIN_CAP = 4096  # max recv iterations per synthesize (bounds the loop)
_MAX_TOTAL_AUDIO_BYTES = 5_000_000  # bound returned audio (~minutes of 8k mu-law)
# Control messages that end one Flush's audio stream.
_DONE_TYPES = ("Flushed", "Cleared", "Close", "Closed")


# --- connection protocol (injectable) --------------------------------------
class DeepgramTTSConnection(ABC):
    """One open streaming connection to Deepgram TTS (text out, audio in)."""

    @abstractmethod
    async def send_text(self, text: str) -> None:
        """Queue text to synthesize (Deepgram `Speak`)."""
        raise NotImplementedError

    @abstractmethod
    async def flush(self) -> None:
        """Flush so the provider emits the remaining audio (Deepgram `Flush`)."""
        raise NotImplementedError

    @abstractmethod
    async def recv(self, *, timeout: float) -> Optional[object]:
        """Next message: `bytes` = an audio frame, `str` = a JSON control message,
        `None` = nothing available before the timeout."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class DeepgramTTSConnector(ABC):
    @abstractmethod
    async def connect(self, *, url: str, headers: dict) -> DeepgramTTSConnection:
        raise NotImplementedError


# --- control-message parsing (pure, never raises) ---------------------------
def parse_deepgram_tts_control(raw: object) -> Optional[dict]:
    """Parse a Deepgram TTS JSON control message into a small safe dict.

    Returns `{"type": <str|None>}` for valid JSON objects, or None for non-JSON /
    non-object messages. Only the message TYPE is kept - never any payload. Never
    raises."""
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    return {"type": t if isinstance(t, str) else None}


# --- provider ---------------------------------------------------------------
class DeepgramStreamingTTSProvider(StreamingTTSProvider):
    """Synthesize an AI reply to RAW mu-law/8k bytes over a Deepgram TTS WebSocket.

    One connection per synthesize() call: connect -> Speak(text) -> Flush -> drain
    binary audio until the provider signals done (or the recv times out) -> close.
    Any failure raises so the playback layer degrades safely; the key is header-only.
    """

    name = "deepgram"

    def __init__(
        self,
        *,
        api_key: str,
        connector: Optional[DeepgramTTSConnector] = None,
        model: str = "aura-asteria-en",
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        container: str = "none",
        speed: str = "",
        connect_timeout: float = 5.0,
        recv_timeout: float = 5.0,
        max_message_bytes: int = 1_000_000,
        max_chars: int = 2000,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramStreamingTTSProvider requires an api_key")
        self._api_key = api_key  # connect header only; never logged/persisted
        self._connector = connector or WebsocketsDeepgramTTSConnector(
            connect_timeout=connect_timeout,
            recv_timeout=recv_timeout,
            max_message_bytes=max_message_bytes,
        )
        self._model = model
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._container = container
        self._speed = speed
        self._connect_timeout = connect_timeout
        self._recv_timeout = recv_timeout
        self._max_chars = max(1, max_chars)

    def _resolve_model(self, voice: Optional[str]) -> str:
        # The interface passes an Azure-style `voice` (e.g. uz-UZ-MadinaNeural) that
        # is meaningless to Deepgram. Only honor it when it looks like a Deepgram
        # model id ("aura..."); otherwise use the configured DEEPGRAM_TTS_MODEL.
        if voice and voice.lower().startswith("aura"):
            return voice
        return self._model

    def _build_url(self, voice: Optional[str]) -> str:
        params = [
            f"encoding={self._encoding}",
            f"sample_rate={self._sample_rate}",
            f"container={self._container}",
        ]
        model = self._resolve_model(voice)
        if model:
            params.append(f"model={model}")
        if self._speed:
            params.append(f"speed={self._speed}")
        return f"{_DEEPGRAM_TTS_URL}?{'&'.join(params)}"

    async def synthesize(
        self, text: str, *, language: str, voice: Optional[str] = None
    ) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""  # empty -> do NOT call the provider, return no audio
        if len(text) > self._max_chars:
            text = text[: self._max_chars]
        # The API key travels ONLY in the Authorization header (never in the URL).
        headers = {"Authorization": f"Token {self._api_key}"}
        conn = None
        try:
            conn = await asyncio.wait_for(
                self._connector.connect(url=self._build_url(voice), headers=headers),
                timeout=self._connect_timeout,
            )
            await conn.send_text(text)
            await conn.flush()
            return await self._drain(conn)
        finally:
            if conn is not None:
                await self._safe_close(conn)

    async def _drain(self, conn: DeepgramTTSConnection) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for _ in range(_DRAIN_CAP):
            msg = await conn.recv(timeout=self._recv_timeout)
            if msg is None:
                break  # no more audio before the timeout -> done (avoid hang)
            if isinstance(msg, (bytes, bytearray)):
                b = bytes(msg)
                if b:
                    chunks.append(b)
                    total += len(b)
                    if total >= _MAX_TOTAL_AUDIO_BYTES:
                        break  # bound returned audio
                continue
            # A JSON control message: stop on a flush/close signal; ignore the rest
            # (Metadata / Warning / unknown) so no provider payload is ever kept.
            ctrl = parse_deepgram_tts_control(msg)
            if ctrl is not None and ctrl.get("type") in _DONE_TYPES:
                break
        return b"".join(chunks)

    @staticmethod
    async def _safe_close(conn: DeepgramTTSConnection) -> None:
        try:
            await conn.close()
        except Exception:
            pass


# --- real connector (production only; opt-in `websockets`) ------------------
class WebsocketsDeepgramTTSConnector(DeepgramTTSConnector):
    """Real connector using the `websockets` package (opt-in extra). Never used by
    tests (which inject `connect_fn`). Lazy-imports so the default install/test run
    needs no extra dependency. The API key is passed ONLY as an Authorization
    header (never in the URL). Reuses the STT adapter's header-kwarg compatibility
    shim so it works across websockets versions."""

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

    async def connect(self, *, url: str, headers: dict) -> DeepgramTTSConnection:
        connect_fn = self._connect_fn
        closed_exc = self._closed_exc
        if connect_fn is None:
            try:
                import websockets  # lazy; `pip install -e ".[stt-streaming]"`
            except Exception as exc:  # missing optional dependency -> clear, no secret
                raise RuntimeError(
                    "STREAMING_TTS_PROVIDER=deepgram needs the 'websockets' package "
                    "(pip install -e '.[stt-streaming]')"
                ) from exc
            connect_fn = websockets.connect
            closed_exc = getattr(getattr(websockets, "exceptions", None), "ConnectionClosed", None)
        # Use the header kwarg the installed websockets version actually accepts.
        hdr = {_header_kwarg_name(connect_fn): list(headers.items())}
        ws = await connect_fn(url, max_size=self._max_message_bytes, **hdr)
        return _WebsocketsTTSConnection(ws, self._recv_timeout, closed_exc=closed_exc)


class _WebsocketsTTSConnection(DeepgramTTSConnection):
    def __init__(self, ws, recv_timeout: float, *, closed_exc=None) -> None:
        self._ws = ws
        self._recv_timeout = recv_timeout
        self._closed_exc = closed_exc

    async def send_text(self, text: str) -> None:
        await self._ws.send(json.dumps({"type": "Speak", "text": text}))

    async def flush(self) -> None:
        await self._ws.send(json.dumps({"type": "Flush"}))

    async def recv(self, *, timeout: float) -> Optional[object]:
        try:
            msg = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return None  # no audio available now -> drain ends
        except Exception as exc:
            # A normal connection-closed ends the drain safely; any OTHER receive /
            # protocol error propagates so the playback layer degrades.
            if self._closed_exc is not None and isinstance(exc, self._closed_exc):
                return None
            raise
        # bytes = audio frame; str = JSON control message.
        return msg

    async def close(self) -> None:
        try:
            await self._ws.send(json.dumps({"type": "Close"}))
        except Exception:
            pass
        await self._ws.close()
