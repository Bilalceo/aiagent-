"""TwilioTelephonyProvider — real inbound Voice webhook (NON-streaming).

Implements:
- X-Twilio-Signature validation (HMAC-SHA1 over URL + sorted POST params),
- application/x-www-form-urlencoded Voice webhook parsing,
- valid TwiML generation for a basic Gather/SpeechResult conversation loop.

It intentionally does NOT implement Twilio Media Streams, WebSocket audio frames,
barge-in, or outbound dialing. Speech text comes in via Twilio's speech-to-text
(`SpeechResult`); the AI reply goes back out as TwiML `<Say>` text.

No paid Twilio API call is ever made here. The auth token is never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Callable, Optional
from xml.sax.saxutils import escape, quoteattr

from app.services.telephony.provider import (
    InboundCallEvent,
    TelephonyParseError,
    TelephonyProvider,
    ValidationResult,
    VoiceResponse,
)

_SIGNATURE_HEADER = "x-twilio-signature"
# Keys copied (sanitized) into raw_metadata. NEVER includes the signature/token.
_SAFE_FORM_KEYS = (
    "CallSid",
    "AccountSid",
    "From",
    "To",
    "CallStatus",
    "Direction",
    "SpeechResult",
    "Confidence",
    "RecordingUrl",
    "CallerName",
)

# Signature is computed by the module-level function so tests can inject a fake
# validator without monkeypatching hashlib.
SignatureValidator = Callable[[str, dict, str], bool]


def compute_twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted k+v))."""
    data = url
    for key in sorted(params):
        data += key + str(params[key])
    digest = hmac.new(
        auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    expected = compute_twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature or "")


class TwilioTelephonyProvider(TelephonyProvider):
    name = "twilio"

    def __init__(
        self,
        *,
        auth_token: str,
        account_sid: str = "",
        public_base_url: str = "",
        validate_signature: bool = True,
        voice: str = "alice",
        gather_language: str = "ru-RU",
        gather_timeout: int = 5,
        use_media_streams: bool = False,
        stream_url: str = "",
        validator: Optional[SignatureValidator] = None,
    ) -> None:
        if not auth_token:
            raise ValueError("TwilioTelephonyProvider requires an auth token")
        if validate_signature and not public_base_url:
            raise ValueError(
                "Twilio signature validation requires PUBLIC_BASE_URL to be set"
            )
        self._auth_token = auth_token
        self._account_sid = account_sid
        self._public_base_url = public_base_url.rstrip("/")
        self._validate_signature = validate_signature
        self._voice = voice
        self._gather_language = gather_language
        self._gather_timeout = gather_timeout
        self.use_media_streams = use_media_streams
        self.stream_url = stream_url
        # Injectable for tests; defaults to the real HMAC check.
        self._validator: SignatureValidator = validator or (
            lambda url, params, signature: validate_twilio_signature(
                self._auth_token, url, params, signature
            )
        )

    # --- URL + signature ----------------------------------------------------
    def public_url_for(self, path: str) -> str:
        return f"{self._public_base_url}{path}"

    def authenticate(self, *, url: str, params: dict, signature: str) -> ValidationResult:
        if not self._validate_signature:
            return ValidationResult(ok=True, reason="signature_validation_disabled")
        if not signature:
            return ValidationResult(ok=False, reason="missing_twilio_signature")
        if self._validator(url, params, signature):
            return ValidationResult(ok=True)
        # Never echo the signature or token.
        return ValidationResult(ok=False, reason="invalid_twilio_signature")

    # --- media-stream token (WebSocket auth) --------------------------------
    # Twilio's WS upgrade has no X-Twilio-Signature, so we pass a short-lived HMAC
    # token via <Parameter> in the <Stream> TwiML and verify it in the start event.
    def make_stream_token(self, call_sid: str, *, ttl_seconds: int = 3600) -> str:
        exp = int(time.time()) + ttl_seconds
        payload = f"{call_sid}.{exp}"
        sig = hmac.new(
            self._auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{sig}"

    def validate_stream_token(
        self,
        token: Optional[str],
        *,
        call_sid: Optional[str] = None,
        now: Optional[int] = None,
    ) -> bool:
        if not token:
            return False
        parts = token.rsplit(".", 2)  # call_sid may contain '.', exp/sig do not
        if len(parts) != 3:
            return False
        tok_call_sid, exp_s, sig = parts
        expected = hmac.new(
            self._auth_token.encode("utf-8"),
            f"{tok_call_sid}.{exp_s}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        current = now if now is not None else int(time.time())
        if exp < current:
            return False
        if call_sid is not None and tok_call_sid != call_sid:
            return False
        return True

    # --- form parsing -------------------------------------------------------
    def parse_form(self, form: dict) -> InboundCallEvent:
        call_sid = _s(form.get("CallSid"))
        if not call_sid:
            raise TelephonyParseError("missing CallSid")
        raw_metadata = {k: form[k] for k in _SAFE_FORM_KEYS if _s(form.get(k))}
        return InboundCallEvent(
            provider=self.name,
            provider_call_id=call_sid,
            from_number=_s(form.get("From")),
            to_number=_s(form.get("To")),
            account_sid=_s(form.get("AccountSid")),
            call_status=_s(form.get("CallStatus")),
            direction=_s(form.get("Direction")) or "inbound",
            speech_result=_s(form.get("SpeechResult")),
            confidence=_f(form.get("Confidence")),
            recording_url=_s(form.get("RecordingUrl")),
            raw_metadata=raw_metadata,
        )

    # --- TwiML builders -----------------------------------------------------
    def build_greeting_twiml(self, *, greeting: str, gather_action: str, language: str = "") -> str:
        lang = language or self._gather_language
        gather = self._gather_block(greeting, gather_action, lang)
        no_input = "Javob eshitilmadi. Iltimos keyinroq qayta qo'ng'iroq qiling."
        body = (
            f"{gather}"
            f"<Say{self._say_attrs(lang)}>{escape(no_input)}</Say>"
            f"<Hangup/>"
        )
        return self._response(body)

    def build_answer_twiml(self, *, ai_text: str, gather_action: str, language: str = "") -> str:
        lang = language or self._gather_language
        follow_up = "Yana savolingiz bo'lsa, ayting."
        gather = self._gather_block(follow_up, gather_action, lang)
        goodbye = "Qo'ng'iroq uchun rahmat. Sog' bo'ling."
        body = (
            f"<Say{self._say_attrs(lang)}>{escape(ai_text)}</Say>"
            f"{gather}"
            f"<Say{self._say_attrs(lang)}>{escape(goodbye)}</Say>"
            f"<Hangup/>"
        )
        return self._response(body)

    def build_repeat_twiml(self, *, gather_action: str, language: str = "") -> str:
        lang = language or self._gather_language
        prompt = "Kechirasiz, eshitolmadim. Iltimos savolingizni qaytaring."
        gather = self._gather_block(prompt, gather_action, lang)
        goodbye = "Javob eshitilmadi. Sizni operatorga ulaymiz yoki keyinroq bog'lanamiz."
        body = (
            f"{gather}"
            f"<Say{self._say_attrs(lang)}>{escape(goodbye)}</Say>"
            f"<Hangup/>"
        )
        return self._response(body)

    def build_media_stream_twiml(
        self, *, greeting: str, stream_url: str, call_sid: str = "", language: str = ""
    ) -> str:
        """Connect the call to a Media Streams WebSocket (spike).

        Twilio opens a WebSocket to `stream_url` and sends connected/start/media/
        stop JSON events. A signed `stream_token` is passed via <Parameter> so the
        WebSocket can authenticate the connection. This does not itself implement
        streaming STT/TTS.
        """
        lang = language or self._gather_language
        params = ""
        if call_sid:
            token = self.make_stream_token(call_sid)
            params = (
                f"<Parameter name=\"call_sid\" value={quoteattr(call_sid)}/>"
                f"<Parameter name=\"stream_token\" value={quoteattr(token)}/>"
            )
        body = (
            f"<Say{self._say_attrs(lang)}>{escape(greeting)}</Say>"
            f"<Connect><Stream url={quoteattr(stream_url)}>{params}</Stream></Connect>"
        )
        return self._response(body)

    def build_operator_twiml(self, *, message: str, language: str = "") -> str:
        lang = language or self._gather_language
        # message is the already-safety-checked AI text (operator/emergency).
        body = f"<Say{self._say_attrs(lang)}>{escape(message)}</Say><Hangup/>"
        return self._response(body)

    def build_error_twiml(self, language: str = "") -> str:
        lang = language or self._gather_language
        msg = "Texnik nosozlik yuz berdi. Iltimos keyinroq qayta qo'ng'iroq qiling."
        return self._response(f"<Say{self._say_attrs(lang)}>{escape(msg)}</Say><Hangup/>")

    # --- helpers ------------------------------------------------------------
    def _gather_block(self, say_text: str, action: str, lang: str) -> str:
        return (
            f"<Gather input=\"speech\" method=\"POST\" action={quoteattr(action)} "
            f"language={quoteattr(lang)} speechTimeout=\"auto\" "
            f"timeout=\"{int(self._gather_timeout)}\">"
            f"<Say{self._say_attrs(lang)}>{escape(say_text)}</Say>"
            f"</Gather>"
        )

    def _say_attrs(self, lang: str) -> str:
        return f" voice={quoteattr(self._voice)} language={quoteattr(lang)}"

    @staticmethod
    def _response(body: str) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'

    # --- generic interface (unused for Twilio; dedicated endpoints handle it) ---
    def validate_inbound_request(self, *, headers: dict, body: bytes) -> ValidationResult:
        # Signature needs the reconstructed public URL + form params; the Twilio
        # endpoints call authenticate() directly. The generic JSON webhook is not
        # used for Twilio.
        raise NotImplementedError("Twilio uses the dedicated /telephony/twilio endpoints")

    def parse_inbound_call(self, *, headers: dict, body: bytes) -> InboundCallEvent:
        raise NotImplementedError("Twilio uses the dedicated /telephony/twilio endpoints")

    def build_voice_response(self, outcome) -> VoiceResponse:
        raise NotImplementedError("Twilio returns TwiML via the dedicated endpoints")


def _s(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
