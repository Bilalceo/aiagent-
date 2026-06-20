"""Live-call smoke mode gate + redaction helpers (A31).

A controlled real-call PILOT gate for the Twilio media-stream WebSocket. It is
pure (DB-free) and OFF by default: when `LIVE_CALL_SMOKE_MODE=false` every method
is a no-op so non-smoke behavior is unchanged.

When smoke mode is ON it can:
- require a valid shared smoke token (from Twilio `customParameters` ONLY - never
  the URL query string, which would leak the secret into proxy/access logs),
- restrict callers to a configured allowlist,
- enforce a max call duration and a max number of AI turns.

Safety: the smoke token is compared in constant time and is NEVER logged,
persisted, or returned. Rejection reasons are short safe codes (no token, no
number). Caller numbers are only redacted for logging via `redact_number`.
"""
from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from typing import Callable, Optional

_REDACTED_NUMBER = "[redacted]"


def redact_number(num: Optional[str]) -> str:
    """Mask a phone number for safe logging: keep a short prefix/suffix only."""
    s = (num or "").strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def _redact_text(text: Optional[str]) -> str:
    """Replace transcript content with a length-only marker (keeps no content)."""
    n = len(text or "")
    return f"[redacted:{n}]" if n else ""


def redact_streaming_summary(summary: dict) -> dict:
    """Redact caller transcript TEXT in a STREAMING METADATA summary in place
    (counts, languages, metrics, and AI actions are kept). Never raises on an
    unexpected shape.

    SCOPE: this redacts only the stream_metadata summary. It does NOT redact the
    CallSession `transcripts` table (role="user" rows persisted by the AI pipeline),
    so smoke tests must still use NO real patient data."""
    if not isinstance(summary, dict):
        return summary
    for f in summary.get("final_transcripts") or []:
        if isinstance(f, dict) and "text" in f:
            f["text"] = _redact_text(f.get("text"))
    for t in summary.get("turns") or []:
        if isinstance(t, dict) and "transcript_text" in t:
            t["transcript_text"] = _redact_text(t.get("transcript_text"))
    return summary


@dataclass(frozen=True)
class LiveCallDecision:
    allowed: bool
    reason: str  # safe code only (e.g. ok | invalid_smoke_token | caller_not_allowed)


class LiveCallGate:
    """Smoke-mode gate for one media stream. Pure logic; injectable clock."""

    def __init__(
        self,
        *,
        smoke_mode: bool = False,
        require_token: bool = True,
        smoke_token: str = "",
        allowed_numbers: Optional[list[str]] = None,
        max_duration_seconds: int = 180,
        max_turns: int = 10,
        redact_transcripts: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._smoke_mode = smoke_mode
        self._require_token = require_token
        self._smoke_token = smoke_token or ""
        self._allowed = list(allowed_numbers or [])
        self._max_duration = max(0, int(max_duration_seconds))
        self._max_turns = max(0, int(max_turns))
        self._redact = redact_transcripts
        self._clock = clock
        self._t0: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self._smoke_mode

    @property
    def redact_transcripts(self) -> bool:
        return self._redact

    def authorize_start(self, *, params: Optional[dict] = None) -> LiveCallDecision:
        """Authorize a stream start. Always allowed when smoke mode is OFF.

        The smoke token is read ONLY from Twilio customParameters (`params`). URL
        query-string tokens are intentionally unsupported: secrets in URLs leak into
        reverse-proxy/gateway access logs."""
        if not self._smoke_mode:
            return LiveCallDecision(True, "smoke_mode_off")
        params = params if isinstance(params, dict) else {}
        if self._require_token:
            token = params.get("smoke_token")
            if not self._smoke_token or not token or not hmac.compare_digest(
                str(token), self._smoke_token
            ):
                return LiveCallDecision(False, "invalid_smoke_token")
        if self._allowed:
            caller = str(params.get("from_number") or params.get("caller") or "").strip()
            if caller not in self._allowed:
                return LiveCallDecision(False, "caller_not_allowed")
        return LiveCallDecision(True, "ok")

    def start_clock(self) -> None:
        """Stamp the start time for the duration guard (call once at stream start)."""
        self._t0 = self._clock()

    def over_limit(self, *, turns: int = 0) -> Optional[str]:
        """Return a safe stop reason if a smoke-mode limit is exceeded, else None.
        Always None when smoke mode is OFF."""
        if not self._smoke_mode:
            return None
        if self._max_turns and turns >= self._max_turns:
            return "live_call_max_turns"
        if self._max_duration and self._t0 is not None:
            if (self._clock() - self._t0) >= self._max_duration:
                return "live_call_max_duration"
        return None
