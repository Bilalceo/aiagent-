"""Telephony intake webhook (spike) — NOT full production telephony.

Thin routes: validation/orchestration lives in TelephonyIntakeService. The
webhook is public but the provider authenticates the request (mock: shared
secret; twilio: signature - skeleton only). Secrets are never logged or returned.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    build_streaming_playback_service,
    build_streaming_stt_session_service,
    build_streaming_turn_service,
    build_telephony_intake_service,
    build_telephony_stream_service,
    build_twilio_telephony_service,
    get_session,
    get_telephony_provider,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.services.telephony.mock import MockTelephonyProvider
from app.services.telephony.provider import (
    TelephonyParseError,
    TelephonySignatureError,
)
from app.services.telephony.stream import TelephonyStreamError, decode_media_payload
from app.services.telephony.twilio import TwilioTelephonyProvider
from app.services.voice.streaming_stt import StreamingAudioFrame
from app.services.voice.streaming_turn import StreamingTurnManager

router = APIRouter()
log = get_logger("telephony")

_TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"
_XML = "application/xml"
_WS_UNSUPPORTED = 1003  # close code: unsupported data (malformed event)
_WS_POLICY = 1008  # close code: policy violation (auth failure)


async def _process(request: Request, session: AsyncSession, *, provider=None) -> Response:
    body = await request.body()
    if len(body) > settings.telephony_max_payload_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Payload exceeds TELEPHONY_MAX_PAYLOAD_BYTES ({settings.telephony_max_payload_bytes})",
        )
    headers = {k.lower(): v for k, v in request.headers.items()}
    svc = build_telephony_intake_service(session, provider=provider)
    try:
        resp = await svc.handle_inbound(headers=headers, body=body)
    except TelephonySignatureError:
        raise HTTPException(status_code=403, detail="Invalid webhook signature/secret")
    except TelephonyParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Telephony provider not implemented")
    except Exception:  # never leak a traceback to the caller
        raise HTTPException(status_code=500, detail="Telephony processing failed")
    return JSONResponse(content=resp.payload, media_type=resp.content_type)


@router.post("/webhook")
async def telephony_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Provider-configured inbound webhook (TELEPHONY_PROVIDER)."""
    return await _process(request, session)


@router.post("/mock/inbound")
async def telephony_mock_inbound(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Local-testing entrypoint that always uses the mock provider."""
    provider = MockTelephonyProvider(webhook_secret=settings.telephony_webhook_secret)
    return await _process(request, session, provider=provider)


# --- Twilio Voice webhook (non-streaming Gather/SpeechResult) ----------------
async def _twilio_form(request: Request) -> tuple[dict, str]:
    """Read the form body (size-guarded) and the signature header."""
    body = await request.body()
    if len(body) > settings.telephony_max_payload_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Payload exceeds TELEPHONY_MAX_PAYLOAD_BYTES ({settings.telephony_max_payload_bytes})",
        )
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get(_TWILIO_SIGNATURE_HEADER, "")
    return params, signature


@router.post("/twilio/voice")
async def twilio_voice(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Inbound Twilio Voice webhook: greet + Gather speech. Returns TwiML."""
    params, signature = await _twilio_form(request)
    svc = build_twilio_telephony_service(session)
    try:
        xml = await svc.handle_voice(form=params, signature=signature)
    except TelephonySignatureError:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    except TelephonyParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=xml, media_type=_XML)


@router.post("/twilio/gather")
async def twilio_gather(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Twilio Gather callback: run the pipeline on SpeechResult. Returns TwiML."""
    params, signature = await _twilio_form(request)
    svc = build_twilio_telephony_service(session)
    try:
        xml = await svc.handle_gather(form=params, signature=signature)
    except TelephonySignatureError:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    except TelephonyParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=xml, media_type=_XML)


@router.post("/twilio/status")
async def twilio_status(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Optional Twilio status callback: update telephony call status."""
    params, signature = await _twilio_form(request)
    svc = build_twilio_telephony_service(session)
    try:
        xml = await svc.handle_status(form=params, signature=signature)
    except TelephonySignatureError:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    except TelephonyParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=xml, media_type=_XML)


# --- Twilio Media Streams WebSocket (spike: parse/lifecycle only) ------------
def _authorize_stream(event: dict) -> bool:
    """Validate the signed stream_token from the start event's customParameters.

    Returns False (caller closes) when the provider is not Twilio, the token is
    missing/invalid/expired, or it does not match the start callSid.
    """
    try:
        provider = get_telephony_provider()
    except RuntimeError:
        return False
    if not isinstance(provider, TwilioTelephonyProvider):
        return False
    start = event.get("start") or {}
    params = start.get("customParameters") or {}
    if not isinstance(params, dict):
        return False
    token = params.get("stream_token")
    call_sid = start.get("callSid") or params.get("call_sid")
    return provider.validate_stream_token(token, call_sid=call_sid)


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _build_audio_frame(event: dict, stream, decoded: bytes) -> StreamingAudioFrame:
    """Build a StreamingAudioFrame from already-decoded bytes (decoded ONCE by the
    caller). The raw payload / base64 string is NEVER logged."""
    media = event.get("media")
    if not isinstance(media, dict):
        media = {}
    track = media.get("track")
    return StreamingAudioFrame(
        stream_sid=stream.stream_sid,
        call_sid=stream.provider_call_id,
        sequence_number=_to_int(event.get("sequenceNumber")),
        timestamp_ms=_to_int(media.get("timestamp")),
        payload_bytes=decoded,
        codec="audio/x-mulaw",
        track=track if isinstance(track, str) else None,
    )


@router.websocket("/twilio/media-stream")
async def twilio_media_stream(
    websocket: WebSocket, session: AsyncSession = Depends(get_session)
) -> None:
    """Accept a Twilio Media Streams WebSocket and track stream lifecycle.

    Parses connected/start/media/stop JSON events and counts frames/bytes. When
    TWILIO_USE_MEDIA_STREAMS and STREAMING_STT_ENABLED are both on, media frames
    are also fed to a (mock) StreamingSTTSessionService. A FINAL transcript is
    routed once through the full AI/safety pipeline (CallSessionService) and the
    safe turn result is attached to the stream; partials never call the AI. When
    STREAMING_TTS_ENABLED is also on, the turn's reply is synthesized (mock) and
    streamed back as `media` + `mark` events over this same socket. No barge-in.
    Raw audio payloads are NEVER logged or stored. Malformed events close safely.
    """
    await websocket.accept()
    svc = build_telephony_stream_service(session)
    streaming_on = settings.streaming_stt_enabled and settings.twilio_use_media_streams
    turns_on = streaming_on and settings.streaming_stt_ai_turns_enabled
    stream = None
    stt = None  # StreamingSTTSessionService, only when streaming_on
    turns = None  # StreamingTurnManager, only when turns_on + a call session is linked
    playback = None  # TwilioPlaybackService, only when streaming_tts_enabled + turns active
    frames = 0
    stopped = False  # local guard (avoids reading possibly-expired stream.status)

    async def _play_turn(turn: dict) -> None:
        # Outbound mock playback over the SAME socket: media frames + a mark. Only
        # when streaming TTS is enabled and the turn produced safe AI text. The
        # summary (safe counts + mark name, NO raw audio/base64) is stored on the
        # turn so it persists with the stream metadata. Never crashes the WS.
        if playback is None or not turn or not turn.get("ai_text"):
            return
        turn["playback"] = await playback.play(
            websocket.send_json,
            stream_sid=stream.stream_sid if stream is not None else None,
            ai_text=turn["ai_text"],
            language=turn.get("language"),
            turn_order=turn.get("order", 0),
        )

    async def _run_finals(events) -> bool:
        # Only FINAL transcripts trigger an AI turn; partials are ignored here.
        # Returns True if at least one final was routed (a turn may have rolled the
        # session back, so the caller re-binds its stream reference afterwards).
        if turns is None:
            return False
        ran = False
        for ev in events:
            if ev.is_final:
                turn = await turns.on_final(ev)
                ran = True
                await _play_turn(turn)
        return ran

    async def _finalize(reason: str) -> None:
        if stt is None or stream is None:
            return
        final_events = await stt.finish()
        await _run_finals(final_events)
        # Summary holds counts + recognized text (+ AI turns); never raw audio/base64.
        summary = stt.summary(stopped_reason=reason)
        if turns is not None:
            summary.update(turns.summary())
        await svc.attach_streaming_summary(stream, summary)

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                await websocket.close(code=_WS_UNSUPPORTED)
                return
            if not isinstance(event, dict):
                await websocket.close(code=_WS_UNSUPPORTED)
                return

            kind = event.get("event")
            if kind == "connected":
                continue
            if kind == "start":
                # Twilio sends exactly one start per stream. A duplicate start on the
                # same socket is anomalous: finalize + stop the existing stream/session
                # (no orphan, session closed once) and reject with a policy violation.
                if stream is not None:
                    await _finalize("superseded")
                    await svc.stop_stream(stream)
                    stopped = True
                    await websocket.close(code=_WS_POLICY)
                    return
                # Authenticate BEFORE creating any TelephonyStream row: validate the
                # signed stream_token passed via <Parameter> in the Connect/Stream TwiML.
                if not _authorize_stream(event):
                    await websocket.close(code=_WS_POLICY)
                    return
                try:
                    stream = await svc.start_stream(event)
                except TelephonyStreamError:
                    await websocket.close(code=_WS_UNSUPPORTED)
                    return
                if streaming_on:
                    start_obj = event.get("start") or {}
                    params = start_obj.get("customParameters")
                    stt = build_streaming_stt_session_service()
                    await stt.start(
                        stream_sid=stream.stream_sid,
                        call_sid=stream.provider_call_id,
                        params=params if isinstance(params, dict) else {},
                    )
                    # Wire AI turns only when a CallSession is linked to this stream.
                    if turns_on:
                        call_session_id = await svc.resolve_call_session_id(stream)
                        if call_session_id is not None:
                            turns = StreamingTurnManager(
                                build_streaming_turn_service(session),
                                call_session_id=call_session_id,
                                stream_id=stream.id,
                                max_turns=settings.streaming_stt_max_turns,
                            )
                            # Outbound mock playback only when AI turns are active.
                            if settings.streaming_tts_enabled:
                                playback = build_streaming_playback_service()
                # Do NOT log payloads or the token; only safe identifiers.
                log.info("twilio_stream_started", stream_sid=stream.stream_sid)
            elif kind == "media":
                if stream is not None:
                    # Decode the payload at most ONCE; reuse it for counting + frame.
                    decoded = None
                    if stt is not None:
                        media = event.get("media") if isinstance(event.get("media"), dict) else {}
                        decoded = decode_media_payload(
                            media.get("payload"), settings.twilio_stream_max_frame_bytes
                        )
                    await svc.record_media_frame(stream, event, decoded=decoded)
                    frames += 1
                    if stt is not None:
                        # A FINAL transcript routes through the AI/safety pipeline;
                        # partials do not. The push returns the new partial/final events.
                        events = await stt.push_frame(
                            _build_audio_frame(event, stream, decoded or b"")
                        )
                        if await _run_finals(events):
                            # A turn may have rolled the session back (expiring the
                            # stream); re-bind so later frames/finalize stay valid.
                            stream = await svc.ensure_live(stream)
                        if stt.over_limit:
                            await _finalize("over_limit")
                            await svc.stop_stream(stream)
                            stopped = True
                            await websocket.close(code=1000)
                            return
            elif kind == "stop":
                if stream is not None:
                    await _finalize("stop_event")
                    await svc.stop_stream(stream)
                    stopped = True
                await websocket.close(code=1000)
                return
            # "mark" and unknown events are ignored.
    finally:
        # If the socket dropped mid-stream, finalize streaming + mark it stopped.
        # Use the local `stopped` flag (never read a possibly-expired stream.status).
        if stream is not None and not stopped:
            await _finalize("disconnect")
            await svc.stop_stream(stream)
        log.info("twilio_stream_closed", frames=frames)
