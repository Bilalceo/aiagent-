"""TelephonyStreamService — Twilio Media Streams lifecycle (WebSocket spike).

Tracks a media stream and counts frames/bytes. It NEVER stores or logs the raw
audio payload. This is a parser/lifecycle spike: no streaming STT/TTS, no
barge-in. Base64 media is decoded only to measure size and validate.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telephony_call import TelephonyCall
from app.models.telephony_stream import TelephonyStream

# Keys from the start event's mediaFormat we keep (safe, non-sensitive).
_MEDIA_FORMAT_KEYS = ("encoding", "sampleRate", "channels")


class TelephonyStreamError(Exception):
    """Malformed stream event; the WebSocket handler closes safely."""


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def decode_media_payload(payload, max_frame_bytes: int) -> bytes:
    """Decode a base64 media payload ONCE, rejecting oversized frames BEFORE
    allocating. Returns decoded bytes, or b"" for missing/invalid/oversized
    payloads. The raw base64/string is never logged. Callers reuse the returned
    bytes for both counting and the streaming frame (no second decode)."""
    if not isinstance(payload, str) or not payload:
        return b""
    # Pre-decode size guard: base64 expands ~4/3; reject before decoding so a huge
    # frame is never materialized in memory.
    if len(payload) > (max_frame_bytes * 4 // 3) + 4:
        return b""
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return b""


class TelephonyStreamService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        max_frame_bytes: int = 8000,
        max_frames_per_call: int = 50_000,
    ) -> None:
        self._session = session
        self._max_frame_bytes = max_frame_bytes
        self._max_frames_per_call = max_frames_per_call

    async def start_stream(self, event: dict) -> TelephonyStream:
        start = event.get("start") or {}
        if not isinstance(start, dict):
            raise TelephonyStreamError("invalid start event")
        stream_sid = event.get("streamSid") or start.get("streamSid")
        call_sid = start.get("callSid")

        telephony_call_id = None
        if call_sid:
            tel = await self._find_call(call_sid)
            if tel is not None:
                telephony_call_id = tel.id

        metadata = self._safe_start_metadata(start)
        stream = TelephonyStream(
            provider="twilio",
            provider_call_id=call_sid,
            stream_sid=stream_sid,
            telephony_call_id=telephony_call_id,
            status="active",
            media_frames_count=0,
            media_bytes_count=0,
            stream_metadata=metadata or None,
            started_at=datetime.now(timezone.utc),
        )
        self._session.add(stream)
        await self._session.commit()
        await self._session.refresh(stream)
        return stream

    async def record_media_frame(
        self, stream: TelephonyStream, event: dict, *, decoded: Optional[bytes] = None
    ) -> int:
        """Count one media frame. Returns the (clamped) decoded byte count.

        `decoded` lets a caller pass bytes already decoded ONCE (streaming path),
        avoiding a second base64 decode. When omitted, decodes via the shared,
        size-capped helper. Enforces the per-call frame cap; never persists raw audio.
        """
        if stream.media_frames_count >= self._max_frames_per_call:
            return 0
        if decoded is None:
            media = event.get("media") if isinstance(event.get("media"), dict) else {}
            decoded = decode_media_payload(media.get("payload"), self._max_frame_bytes)
        n_bytes = min(len(decoded), self._max_frame_bytes)

        stream.media_frames_count += 1
        stream.media_bytes_count += n_bytes
        seq = _int_or_none(event.get("sequenceNumber"))
        if seq is not None:
            stream.last_sequence_number = seq
        # Persist periodically so a dropped socket still leaves a recent count.
        if stream.media_frames_count % 50 == 0:
            await self._session.commit()
        return n_bytes

    async def ensure_live(self, stream: TelephonyStream) -> TelephonyStream:
        """Return a usable stream instance.

        A failed streaming turn rolls back the shared session, which EXPIRES this
        ORM instance; touching its attributes would then do implicit async IO and
        raise MissingGreenlet. When the instance is expired/detached we re-load it
        by primary key. In the normal path the instance is live, so this is a no-op
        (no extra query, no loss of pending frame-count increments)."""
        state = sa_inspect(stream)
        if (state.expired or state.detached) and state.identity is not None:
            fresh = await self._session.get(TelephonyStream, state.identity[0])
            if fresh is not None:
                return fresh
        return stream

    async def stop_stream(self, stream: TelephonyStream) -> TelephonyStream:
        stream = await self.ensure_live(stream)
        if stream.status != "stopped":
            stream.status = "stopped"
            stream.stopped_at = datetime.now(timezone.utc)
            await self._session.commit()
            await self._session.refresh(stream)
        return stream

    async def attach_streaming_summary(
        self, stream: TelephonyStream, summary: dict
    ) -> TelephonyStream:
        """Merge a streaming-STT summary (counts + final transcript text, NO raw
        audio) into stream_metadata so it shows in the admin detail endpoint."""
        stream = await self.ensure_live(stream)  # safe after a turn rollback expired it
        meta = dict(stream.stream_metadata or {})
        meta["streaming_stt"] = summary
        stream.stream_metadata = meta
        await self._session.commit()
        await self._session.refresh(stream)
        return stream

    async def attach_latency_summary(
        self, stream: TelephonyStream, latency: dict
    ) -> TelephonyStream:
        """Merge the streaming latency metrics (numeric ms offsets/durations only,
        NO raw audio/base64/secrets) into stream_metadata.latency for admin view."""
        stream = await self.ensure_live(stream)
        meta = dict(stream.stream_metadata or {})
        meta["latency"] = latency
        stream.stream_metadata = meta
        await self._session.commit()
        await self._session.refresh(stream)
        return stream

    async def resolve_call_session_id(self, stream: TelephonyStream) -> Optional[int]:
        """Resolve the linked text-simulation Call id (CallSession) for a stream.

        Routes through the stream's TelephonyCall (set at /twilio/voice). Returns
        None when the stream has no linked call session yet — the streaming AI turn
        is then simply not run (no crash). No audio is involved here."""
        tel = None
        if stream.telephony_call_id is not None:
            tel = await self._session.get(TelephonyCall, stream.telephony_call_id)
        if tel is None and stream.provider_call_id:
            tel = await self._find_call(stream.provider_call_id)
        return tel.call_session_id if tel is not None else None

    # --- admin reads --------------------------------------------------------
    async def list(
        self,
        *,
        call_sid: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TelephonyStream]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        stmt = select(TelephonyStream)
        if call_sid:
            stmt = stmt.where(TelephonyStream.provider_call_id == call_sid)
        if status:
            stmt = stmt.where(TelephonyStream.status == status)
        stmt = stmt.order_by(TelephonyStream.id.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, stream_id: int) -> Optional[TelephonyStream]:
        stmt = select(TelephonyStream).where(TelephonyStream.id == stream_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # --- helpers ------------------------------------------------------------
    async def _find_call(self, sid: str) -> Optional[TelephonyCall]:
        stmt = (
            select(TelephonyCall)
            .where(TelephonyCall.provider_call_id == sid)
            .order_by(TelephonyCall.id.desc())
        )
        return (await self._session.execute(stmt)).scalars().first()

    @staticmethod
    def _safe_start_metadata(start: dict) -> dict:
        meta: dict = {}
        tracks = start.get("tracks")
        if isinstance(tracks, list):
            meta["tracks"] = [str(t) for t in tracks]
        media_format = start.get("mediaFormat")
        if isinstance(media_format, dict):
            meta["media_format"] = {
                k: media_format[k] for k in _MEDIA_FORMAT_KEYS if k in media_format
            }
        return meta
