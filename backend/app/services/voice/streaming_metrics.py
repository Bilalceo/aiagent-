"""Streaming voice latency metrics (instrumentation only).

Pure and DB-free, with an INJECTABLE monotonic clock so tests are deterministic.
Records pipeline EVENTS (as integer ms offsets from the websocket connect) and
computes DURATIONS in ms. The persisted summary holds ONLY event names + numbers
(and optional wall-clock ISO timestamps when explicitly enabled): never raw audio,
base64, secrets, or provider payloads.

This is debugging/eval instrumentation for the (still mock-first) streaming voice
pipeline; it does not change STT/TTS/barge-in behavior.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional


class StreamingLatencyTracker:
    def __init__(
        self,
        *,
        enabled: bool = True,
        include_timestamps: bool = False,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.enabled = enabled
        self._include_ts = include_timestamps
        self._clock = clock  # monotonic seconds (for durations)
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self._t0: Optional[float] = None  # reference: first event (connect)
        self._events: dict[str, float] = {}  # event -> monotonic time
        self._wall_events: dict[str, str] = {}  # event -> ISO (only if include_ts)
        self._durations: dict[str, int] = {}  # externally-supplied durations (ms)

    def now(self) -> float:
        return self._clock()

    def mark(self, name: str, *, once: bool = True) -> None:
        """Record an event at the current clock time. `once` keeps the FIRST time
        (for first_* events); pass once=False to keep the LATEST (for last_*)."""
        self.mark_at(name, self._clock(), once=once)

    def mark_at(self, name: str, t: float, *, once: bool = True) -> None:
        if not self.enabled:
            return
        try:
            if self._t0 is None:
                self._t0 = t
            if once and name in self._events:
                return
            self._events[name] = t
            if self._include_ts and not (once and name in self._wall_events):
                self._wall_events[name] = self._wall().isoformat()
        except Exception:  # metrics must never crash the pipeline
            pass

    def set_duration(self, name: str, ms: Optional[int], *, once: bool = True) -> None:
        """Inject a precomputed duration (e.g. TTS first-chunk time from the player)."""
        if not self.enabled or ms is None:
            return
        if once and name in self._durations:
            return
        try:
            self._durations[name] = int(ms)
        except Exception:
            pass

    def offset(self, t: Optional[float]) -> Optional[int]:
        """ms offset of a monotonic time from t0 (the connect event)."""
        if not self.enabled or t is None or self._t0 is None:
            return None
        return int(round((t - self._t0) * 1000))

    def offset_now(self) -> Optional[int]:
        return self.offset(self._clock()) if self.enabled else None

    def _dur(self, a: str, b: str) -> Optional[int]:
        ta, tb = self._events.get(a), self._events.get(b)
        if ta is None or tb is None:
            return None
        return int(round((tb - ta) * 1000))

    def summary(self) -> dict:
        """Safe latency summary for stream_metadata.latency (numbers only)."""
        if not self.enabled:
            return {"enabled": False}
        events_at = {name: self.offset(t) for name, t in self._events.items()}
        durations = {
            "time_to_first_media_ms": self._dur("websocket_connected_at", "first_media_frame_at"),
            "time_to_first_partial_ms": self._dur("websocket_connected_at", "first_partial_transcript_at"),
            "time_to_first_final_ms": self._dur("websocket_connected_at", "first_final_transcript_at"),
            "ai_turn_duration_ms": self._dur("ai_turn_started_at", "ai_turn_completed_at"),
            "tts_time_to_first_chunk_ms": self._durations.get("tts_time_to_first_chunk_ms"),
            "tts_playback_duration_ms": self._durations.get("tts_playback_duration_ms"),
            "mark_round_trip_ms": self._dur("tts_playback_completed_at", "mark_received_at"),
            "barge_in_clear_latency_ms": self._durations.get("barge_in_clear_latency_ms"),
            "total_stream_duration_ms": self._dur("websocket_connected_at", "stream_stopped_at"),
        }
        out: dict = {
            "enabled": True,
            "events_at_ms": events_at,
            "durations_ms": {k: v for k, v in durations.items() if v is not None},
        }
        if self._include_ts:
            out["timestamps"] = dict(self._wall_events)
        return out
