"""Streaming TTS playback (mock-first) for Twilio Media Streams.

When a streaming FINAL transcript produces an AI text turn, this layer synthesizes
the reply (mock by default) and streams it back to Twilio over the SAME WebSocket
as `media` (base64 audio) + a trailing `mark` event.

This is the FIRST playback-architecture milestone, not production low-latency
voice: the mock does not produce real speech, there is no barge-in, and no real
TTS provider is wired. Safety: raw audio / base64 payloads are NEVER logged or
persisted - only safe counts (chunks/bytes) and the mark name end up in metadata.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

# An async sink that delivers one outbound Twilio JSON message (e.g.
# websocket.send_json). Injectable so unit tests can capture messages.
SendFn = Callable[[dict], Awaitable[None]]


def _resolve_voice(language: Optional[str], voice: Optional[str], uz: str, ru: str) -> str:
    if voice:
        return voice
    return ru if (language or "").startswith("ru") else uz


# --- Twilio outbound event builders ----------------------------------------
def build_media_message(stream_sid: Optional[str], payload_b64: str) -> dict:
    """Outbound Twilio `media` event carrying one base64 audio chunk."""
    return {"event": "media", "streamSid": stream_sid, "media": {"payload": payload_b64}}


def build_mark_message(stream_sid: Optional[str], name: str) -> dict:
    """Outbound Twilio `mark` event; Twilio echoes it back when playback reaches it."""
    return {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}


def build_clear_message(stream_sid: Optional[str]) -> dict:
    """Outbound Twilio `clear` event (flush buffered playback).

    Provided for completeness / a future barge-in implementation. It is NOT used
    yet - A26 does not implement barge-in.
    """
    return {"event": "clear", "streamSid": stream_sid}


def chunk_bytes(data: bytes, size: int) -> list[bytes]:
    """Split audio bytes into <= size frames (never logs the bytes)."""
    size = max(1, size)
    return [data[i : i + size] for i in range(0, len(data), size)]


# --- provider --------------------------------------------------------------
class StreamingTTSProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def synthesize(
        self, text: str, *, language: str, voice: Optional[str] = None
    ) -> bytes:
        """Return audio bytes for the text. Real providers may stream/connect."""
        raise NotImplementedError


class MockStreamingTTSProvider(StreamingTTSProvider):
    """Deterministic fake audio (no synthesis, no external calls)."""

    name = "mock"

    async def synthesize(
        self, text: str, *, language: str, voice: Optional[str] = None
    ) -> bytes:
        return b"MOCK-TTS:" + text.encode("utf-8")


# --- playback service ------------------------------------------------------
class TwilioPlaybackService:
    """Synthesize an AI turn's reply and stream it back over a Twilio Media Stream.

    Sends N `media` frames then one `mark`. Never raises: a synth/send failure is
    captured as a degraded playback summary so the WebSocket cannot crash. The
    summary holds only safe counts + the mark name (no raw audio / no base64)."""

    def __init__(
        self,
        provider: StreamingTTSProvider,
        *,
        chunk_size: int = 400,
        max_text_chars: int = 2000,
        max_chunks: int = 200,
        voice_uz: str = "uz-UZ-MadinaNeural",
        voice_ru: str = "ru-RU-SvetlanaNeural",
    ) -> None:
        self._provider = provider
        self._chunk_size = max(1, chunk_size)
        self._max_text_chars = max(1, max_text_chars)
        self._max_chunks = max(1, max_chunks)
        self._voice_uz = voice_uz
        self._voice_ru = voice_ru

    async def play(
        self,
        send: SendFn,
        *,
        stream_sid: Optional[str],
        ai_text: str,
        language: Optional[str] = None,
        voice: Optional[str] = None,
        turn_order: int = 0,
    ) -> dict:
        """Stream the reply as media + mark. Returns a SAFE playback summary."""
        resolved_voice = _resolve_voice(language, voice, self._voice_uz, self._voice_ru)
        mark_name = f"{stream_sid or 'stream'}:turn:{turn_order}"
        summary = {
            "provider": getattr(self._provider, "name", "mock"),
            "enabled": True,
            "voice": resolved_voice,
            "chunks_sent": 0,
            "bytes_sent": 0,
            "mark_name": mark_name,
            "truncated": False,
            "degraded": False,
            "error": None,
            # Playback lifecycle (updated by the barge-in/mark handling):
            "status": "playing",  # playing | completed | interrupted | degraded
            "mark_received": False,
            "clear_sent": False,
            "interrupted": False,
            "interruption_reason": None,
        }

        text = (ai_text or "").strip()
        if not text:
            summary["degraded"] = True
            summary["status"] = "degraded"
            summary["error"] = "empty_text"
            return summary
        if len(text) > self._max_text_chars:
            text = text[: self._max_text_chars]
            summary["truncated"] = True

        try:
            audio = await self._provider.synthesize(
                text, language=language or "uz-UZ", voice=resolved_voice
            )
        except Exception:  # synth failure -> degraded, never crash the WS
            summary["degraded"] = True
            summary["status"] = "degraded"
            summary["error"] = "tts_error"
            return summary

        chunks = chunk_bytes(audio, self._chunk_size)
        if len(chunks) > self._max_chunks:
            chunks = chunks[: self._max_chunks]
            summary["truncated"] = True

        try:
            for ch in chunks:
                # Encode each chunk to base64 exactly once.
                payload = base64.b64encode(ch).decode("ascii")
                await send(build_media_message(stream_sid, payload))
                summary["chunks_sent"] += 1
                summary["bytes_sent"] += len(ch)  # raw audio bytes, never the payload
            await send(build_mark_message(stream_sid, mark_name))
        except Exception:  # send failure (socket broken) -> degraded, never crash
            summary["degraded"] = True
            summary["status"] = "degraded"
            summary["error"] = "send_error"
            return summary

        return summary


class BargeInController:
    """Barge-in state machine for one media stream (mock-first).

    Tracks the currently-active playback (the turn's playback summary dict) and,
    when the caller speaks again (a streaming partial/final transcript), sends a
    Twilio `clear` to interrupt the queued audio. It also handles Twilio `mark`
    echoes to mark a playback completed. All state lives on the SAME playback
    summary dict that is persisted with the stream metadata - no raw audio/base64.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        on_partial: bool = True,
        on_final: bool = True,
        min_chars: int = 1,
    ) -> None:
        self.enabled = enabled
        self._on_partial = on_partial
        self._on_final = on_final
        self._min_chars = max(1, min_chars)
        self.active: Optional[dict] = None  # active playback summary, awaiting mark/interrupt

    def begin_playback(self, summary: Optional[dict]) -> None:
        """Track a freshly-sent playback. Only a successful (non-degraded) playback
        can be interrupted; a degraded one is never marked active."""
        self.active = summary if (summary and not summary.get("degraded")) else None

    def on_mark(self, name: Optional[str]) -> None:
        """Handle a Twilio `mark` echo: complete the active playback if it matches.

        Unknown names and duplicate/late marks are no-ops (idempotent, no crash)."""
        a = self.active
        if a is None or not name:
            return
        if a.get("mark_name") == name and not a.get("mark_received"):
            a["mark_received"] = True
            if not a.get("interrupted"):
                a["status"] = "completed"
            self.active = None  # playback finished

    def _qualifies(self, events) -> bool:
        """True if any event is caller speech that should interrupt playback."""
        for e in events:
            if len((e.text or "").strip()) < self._min_chars:
                continue
            if e.is_final and self._on_final:
                return True
            if (not e.is_final) and self._on_partial:
                return True
        return False

    async def maybe_barge_in(self, events, send: SendFn, stream_sid: Optional[str]) -> bool:
        """If the caller is speaking during active playback, send ONE `clear`.

        Returns True if a clear was sent. Never sends a duplicate clear for the same
        playback. A send failure marks the playback degraded but never raises."""
        if not self.enabled:
            return False
        a = self.active
        if a is None or a.get("clear_sent") or a.get("interrupted"):
            return False
        if not self._qualifies(events):
            return False
        try:
            await send(build_clear_message(stream_sid))
        except Exception:  # clear failed -> mark degraded, do not crash the WS
            a["interrupted"] = True
            a["interruption_reason"] = "caller_speech"
            a["interruption_error"] = "clear_send_error"
            a["status"] = "degraded"
            a["degraded"] = True
            self.active = None
            return False
        a["clear_sent"] = True
        a["interrupted"] = True
        a["interruption_reason"] = "caller_speech"
        a["status"] = "interrupted"
        self.active = None  # interrupted; no further barge-in for this playback
        return True
