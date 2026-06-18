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
from app.services.telephony.stream import TelephonyStreamError
from app.services.telephony.twilio import TwilioTelephonyProvider

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


@router.websocket("/twilio/media-stream")
async def twilio_media_stream(
    websocket: WebSocket, session: AsyncSession = Depends(get_session)
) -> None:
    """Accept a Twilio Media Streams WebSocket and track stream lifecycle.

    Parses connected/start/media/stop JSON events and counts frames/bytes. Raw
    audio payloads are NEVER logged or stored. This is a spike: no streaming
    STT/TTS, no barge-in. Malformed events close the socket safely.
    """
    await websocket.accept()
    svc = build_telephony_stream_service(session)
    stream = None
    frames = 0
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
                # Do NOT log payloads or the token; only safe identifiers.
                log.info("twilio_stream_started", stream_sid=stream.stream_sid)
            elif kind == "media":
                if stream is not None:
                    await svc.record_media_frame(stream, event)
                    frames += 1
            elif kind == "stop":
                if stream is not None:
                    await svc.stop_stream(stream)
                await websocket.close(code=1000)
                return
            # "mark" and unknown events are ignored.
    finally:
        # If the socket dropped mid-stream, still mark it stopped.
        if stream is not None and stream.status != "stopped":
            await svc.stop_stream(stream)
        log.info("twilio_stream_closed", frames=frames)
