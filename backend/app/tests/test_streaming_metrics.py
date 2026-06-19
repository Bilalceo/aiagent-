"""Streaming voice latency metrics (instrumentation only).

Unit tests use an injected fake clock for deterministic durations. WebSocket tests
assert the latency summary is attached (structure + numeric, no raw audio) and that
per-turn metrics land in the turn dict.
"""
from __future__ import annotations

import base64

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app
from app.models.audit_log import AuditLog
from app.models.call import Call
from app.models.callback_task import CallbackTask
from app.models.knowledge_item import KnowledgeItem
from app.models.telephony_call import TelephonyCall
from app.models.telephony_stream import TelephonyStream
from app.models.transcript import Transcript
from app.services.ai.provider import MockAIProvider
from app.services.ai.service import AIService
from app.services.audit.log import AuditLogService
from app.services.call.session import CallSessionService
from app.services.knowledge.service import KBMatch
from app.services.operator.availability import MockOperatorAvailability, OperatorState
from app.services.operator.transfer import OperatorTransferDecisionService
from app.services.telephony.stream import TelephonyStreamService
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.streaming_metrics import StreamingLatencyTracker
from app.services.voice.streaming_stt import (
    StreamingSTTProvider,
    StreamingSTTSession,
    TranscriptEvent,
)

API = "/api/v1"
WS_URL = f"{API}/telephony/twilio/media-stream"
_WS_PROVIDER = TwilioTelephonyProvider(
    auth_token="ws-secret", public_base_url="https://x", validate_signature=False
)
_TABLES = [
    Call.__table__, Transcript.__table__, AuditLog.__table__, CallbackTask.__table__,
    KnowledgeItem.__table__, TelephonyCall.__table__, TelephonyStream.__table__,
]


class _FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


# --- unit: StreamingLatencyTracker ------------------------------------------
def test_tracker_durations_are_deterministic() -> None:
    clk = _FakeClock()
    tr = StreamingLatencyTracker(clock=clk)
    tr.mark("websocket_connected_at")          # t=0
    clk.advance(0.030)
    tr.mark("first_media_frame_at")            # +30ms
    clk.advance(0.020)
    tr.mark("first_final_transcript_at")       # +50ms
    tr.mark("ai_turn_started_at")
    clk.advance(0.100)
    tr.mark("ai_turn_completed_at")            # +100ms
    clk.advance(0.010)
    tr.mark("stream_stopped_at")               # total 160ms
    s = tr.summary()
    assert s["enabled"] is True
    assert s["events_at_ms"]["websocket_connected_at"] == 0
    assert s["events_at_ms"]["first_media_frame_at"] == 30
    assert s["durations_ms"]["time_to_first_media_ms"] == 30
    assert s["durations_ms"]["time_to_first_final_ms"] == 50
    assert s["durations_ms"]["ai_turn_duration_ms"] == 100
    assert s["durations_ms"]["total_stream_duration_ms"] == 160


def test_first_event_is_once_wins_last_overwrites() -> None:
    clk = _FakeClock()
    tr = StreamingLatencyTracker(clock=clk)
    tr.mark("websocket_connected_at")
    clk.advance(0.01)
    tr.mark("first_media_frame_at")
    clk.advance(0.05)
    tr.mark("first_media_frame_at")            # once -> ignored
    tr.mark("last_media_frame_at", once=False)
    clk.advance(0.04)
    tr.mark("last_media_frame_at", once=False)  # overwrite
    s = tr.summary()
    assert s["events_at_ms"]["first_media_frame_at"] == 10
    assert s["events_at_ms"]["last_media_frame_at"] == 100


def test_set_duration_and_offset() -> None:
    clk = _FakeClock()
    tr = StreamingLatencyTracker(clock=clk)
    tr.mark("websocket_connected_at")
    clk.advance(0.2)
    assert tr.offset_now() == 200
    tr.set_duration("tts_playback_duration_ms", 42)
    tr.set_duration("tts_playback_duration_ms", 99)  # once -> kept first
    assert tr.summary()["durations_ms"]["tts_playback_duration_ms"] == 42


def test_disabled_tracker_is_minimal_noop() -> None:
    tr = StreamingLatencyTracker(enabled=False)
    tr.mark("websocket_connected_at")
    tr.set_duration("x_ms", 5)
    assert tr.summary() == {"enabled": False}
    assert tr.offset_now() is None


def test_include_timestamps_adds_iso() -> None:
    clk = _FakeClock()
    tr = StreamingLatencyTracker(clock=clk, include_timestamps=True)
    tr.mark("websocket_connected_at")
    s = tr.summary()
    assert "timestamps" in s and "websocket_connected_at" in s["timestamps"]
    assert "T" in s["timestamps"]["websocket_connected_at"]  # ISO


def test_summary_has_no_raw_audio_keys() -> None:
    tr = StreamingLatencyTracker(clock=_FakeClock())
    tr.mark("websocket_connected_at")
    s = tr.summary()
    assert not any(bad in str(s) for bad in ("payload", "MOCK-TTS", "base64"))


# --- WS harness --------------------------------------------------------------
class _StubKnowledge:
    def __init__(self, chunks):
        self._matches = [KBMatch(id=i + 1, title=f"i{i}", content=c, category="faq")
                         for i, c in enumerate(chunks)]

    async def search(self, query, language, intent=None):
        return list(self._matches)


async def _new_call(session, chunks=None):
    css = CallSessionService(
        session,
        AIService(provider=MockAIProvider(), knowledge=_StubKnowledge(chunks or [])),
        AuditLogService(session),
        OperatorTransferDecisionService(
            session, MockOperatorAvailability(OperatorState.AVAILABLE), AuditLogService(session)
        ),
    )
    return (await css.start_call(from_number="+998901112233", to_number="clinic")).call


class _ScriptedSession(StreamingSTTSession):
    def __init__(self, script):
        self._script, self._i = script, 0

    async def accept_audio_frame(self, frame):
        out = self._script[self._i] if self._i < len(self._script) else []
        self._i += 1
        return list(out)

    async def finish_stream(self):
        return []

    async def close(self):
        return None


class _ScriptedProvider(StreamingSTTProvider):
    name = "mock"

    def __init__(self, script):
        self._script = script

    def start_stream(self, context):
        return _ScriptedSession(self._script)


def _final(text, eid):
    return TranscriptEvent(text=text, language="uz-UZ", is_final=True, provider="mock",
                           confidence=0.9, event_id=eid)


def _partial(text):
    return TranscriptEvent(text=text, language="uz-UZ", is_final=False, provider="mock")


def _ws_start(stream_sid, call_sid, phrase=None):
    cp = {"call_sid": call_sid, "stream_token": _WS_PROVIDER.make_stream_token(call_sid)}
    if phrase is not None:
        cp["test_phrase"] = phrase
    return {
        "event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
        "start": {
            "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": cp,
        },
    }


def _media(seq):
    return {"event": "media", "sequenceNumber": str(seq),
            "media": {"track": "inbound", "payload": base64.b64encode(b"\x00" * 160).decode()}}


@pytest.fixture
def latency_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_latency_summary

    async def _spy(self, stream, latency):
        calls.append(latency)
        return await orig(self, stream, latency)

    monkeypatch.setattr(TelephonyStreamService, "attach_latency_summary", _spy)
    return calls


@pytest.fixture
def stt_spy(monkeypatch):
    calls: list[dict] = []
    orig = TelephonyStreamService.attach_streaming_summary

    async def _spy(self, stream, summary):
        calls.append(summary)
        return await orig(self, stream, summary)

    monkeypatch.setattr(TelephonyStreamService, "attach_streaming_summary", _spy)
    return calls


def _seeded_ws(monkeypatch, *, call_sid, script=None, metrics=True, barge=False):
    import app.api.deps as depsmod
    import app.api.v1.telephony as tele

    monkeypatch.setattr(tele, "get_telephony_provider", lambda: _WS_PROVIDER)
    if script is not None:
        monkeypatch.setattr(depsmod, "get_streaming_stt_provider", lambda: _ScriptedProvider(script))
    monkeypatch.setattr(settings, "twilio_use_media_streams", True)
    monkeypatch.setattr(settings, "streaming_stt_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_ai_turns_enabled", True)
    monkeypatch.setattr(settings, "streaming_stt_final_after_frames", 2)
    monkeypatch.setattr(settings, "streaming_tts_enabled", True)
    monkeypatch.setattr(settings, "streaming_metrics_enabled", metrics)
    monkeypatch.setattr(settings, "barge_in_enabled", barge)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    state = {"init": False}

    async def _override():
        if not state["init"]:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_TABLES))
            factory0 = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory0() as s0:
                call = await _new_call(s0, chunks=["9:00-18:00"])
                s0.add(TelephonyCall(
                    provider="twilio", provider_call_id=call_sid, call_session_id=call.id,
                    from_number="+998901112233", to_number="clinic", status="in_progress",
                    direction="inbound",
                ))
                await s0.commit()
            state["init"] = True
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    return TestClient(app)


def _recv_until(ws, event_type):
    out = []
    while True:
        m = ws.receive_json()
        out.append(m)
        if m["event"] == event_type:
            return out


def _drain(ws):
    with pytest.raises(WebSocketDisconnect):
        while True:
            ws.receive_json()


# --- WS: latency attached with per-turn metrics ------------------------------
def test_ws_latency_attached_on_stop(monkeypatch, latency_spy, stt_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-lat")
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-lat", "CA-lat", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(2))  # partial
            ws.send_json(_media(3))  # final -> turn + playback
            _recv_until(ws, "mark")  # drain playback
            ws.send_json({"event": "stop", "streamSid": "MZ-lat", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    assert len(latency_spy) == 1
    lat = latency_spy[-1]
    assert lat["enabled"] is True
    ev = lat["events_at_ms"]
    for key in ("websocket_connected_at", "stream_started_at", "first_media_frame_at",
                "first_final_transcript_at", "ai_turn_started_at", "ai_turn_completed_at",
                "tts_playback_started_at", "tts_playback_completed_at", "stream_stopped_at"):
        assert key in ev and ev[key] is not None and ev[key] >= 0
    dur = lat["durations_ms"]
    for key in ("time_to_first_media_ms", "ai_turn_duration_ms",
                "tts_playback_duration_ms", "total_stream_duration_ms"):
        assert key in dur and dur[key] >= 0
    # Per-turn metrics live on the turn dict.
    m = stt_spy[-1]["turns"][0]["metrics"]
    assert m["ai_duration_ms"] >= 0 and m["playback_duration_ms"] >= 0
    assert m["playback_started_at_ms"] is not None
    # No raw audio / base64 / secrets in metrics.
    assert not any(bad in str(lat) for bad in ("MOCK-TTS", "payload", "base64", "stream_token"))


def test_ws_metrics_disabled_mark_writes_no_timing(monkeypatch, latency_spy, stt_spy) -> None:
    # Metrics OFF + an echoed mark: the playback still completes, but NO timing
    # keys (mark_received_at_ms) are written, and no latency/metrics are attached.
    client = _seeded_ws(monkeypatch, call_sid="CA-nom", metrics=False)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-nom", "CA-nom", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(2))
            ws.send_json(_media(3))  # final -> turn + playback (mark MZ-nom:turn:0)
            _recv_until(ws, "mark")
            ws.send_json({"event": "mark", "streamSid": "MZ-nom", "mark": {"name": "MZ-nom:turn:0"}})
            ws.send_json({"event": "stop", "streamSid": "MZ-nom", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    assert latency_spy == []  # disabled -> no latency attached
    turn = stt_spy[-1]["turns"][0]
    assert "metrics" not in turn  # no per-turn metrics
    pb = turn["playback"]
    assert "mark_received_at_ms" not in pb and "clear_sent_at_ms" not in pb  # no timing keys
    # Normal lifecycle fields are still present/correct.
    assert pb["mark_received"] is True and pb["status"] == "completed"


def test_ws_metrics_disabled_clear_writes_no_timing(monkeypatch, latency_spy, stt_spy) -> None:
    # Metrics OFF + barge-in clear: the playback is interrupted, but NO timing keys
    # (clear_sent_at_ms) are written, and no latency/metrics are attached.
    script = [[_final("Ish vaqtingiz qanday", "f0")], [_partial("yana")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-noc", script=script, metrics=False, barge=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-noc", "CA-noc"))
            ws.send_json(_media(2))  # final -> turn0 + playback
            _recv_until(ws, "mark")
            ws.send_json(_media(3))  # partial -> barge-in clear
            _recv_until(ws, "clear")
            ws.send_json({"event": "stop", "streamSid": "MZ-noc", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    assert latency_spy == []  # disabled -> no latency attached
    turn = stt_spy[-1]["turns"][0]
    assert "metrics" not in turn
    pb = turn["playback"]
    assert "clear_sent_at_ms" not in pb and "mark_received_at_ms" not in pb  # no timing keys
    # Normal lifecycle fields are still present/correct.
    assert pb["clear_sent"] is True and pb["interrupted"] is True and pb["status"] == "interrupted"


# --- WS: mark round trip (echoed mark completes playback) ---------------------
def test_ws_metrics_mark_round_trip(monkeypatch, latency_spy, stt_spy) -> None:
    client = _seeded_ws(monkeypatch, call_sid="CA-mrt")  # barge off
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-mrt", "CA-mrt", phrase="Ish vaqtingiz qanday"))
            ws.send_json(_media(2))  # partial
            ws.send_json(_media(3))  # final -> turn + playback (server mark MZ-mrt:turn:0)
            _recv_until(ws, "mark")  # drain playback
            ws.send_json({"event": "mark", "streamSid": "MZ-mrt", "mark": {"name": "MZ-mrt:turn:0"}})
            ws.send_json({"event": "stop", "streamSid": "MZ-mrt", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    lat = latency_spy[-1]
    assert "mark_received_at" in lat["events_at_ms"]
    assert lat["durations_ms"]["mark_round_trip_ms"] >= 0
    pb = stt_spy[-1]["turns"][0]["playback"]
    assert pb["mark_received_at_ms"] is not None and pb["status"] == "completed"


# --- WS: barge-in clear latency ----------------------------------------------
def test_ws_metrics_barge_clear_latency(monkeypatch, latency_spy, stt_spy) -> None:
    script = [[_final("Ish vaqtingiz qanday", "f0")], [_partial("yana")]]
    client = _seeded_ws(monkeypatch, call_sid="CA-bcl", script=script, barge=True)
    try:
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_ws_start("MZ-bcl", "CA-bcl"))
            ws.send_json(_media(2))  # final -> turn0 + playback
            _recv_until(ws, "mark")  # drain playback
            ws.send_json(_media(3))  # partial -> barge-in clear (playback still active)
            _recv_until(ws, "clear")
            ws.send_json({"event": "stop", "streamSid": "MZ-bcl", "stop": {}})
            _drain(ws)
    finally:
        app.dependency_overrides.clear()
    lat = latency_spy[-1]
    assert "clear_sent_at" in lat["events_at_ms"]
    assert lat["durations_ms"]["barge_in_clear_latency_ms"] >= 0
    pb = stt_spy[-1]["turns"][0]["playback"]
    assert pb["clear_sent_at_ms"] is not None and pb["status"] == "interrupted"
