# Live voice smoke test (controlled pilot gate) - A31

This is a CONTROLLED real-call smoke test, NOT a public launch and NOT full
production. It is a safety gate before any real clinic usage: a handful of staff
calls, real Twilio + real Deepgram STT + Deepgram TTS, hard caps on duration and
turns, an optional smoke token, an optional caller allowlist, and optional
transcript redaction.

Defaults are unchanged: smoke mode is OFF, providers default to mock. Nothing in
this doc affects normal mock/test behavior.

IMPORTANT (medical safety, mandatory): use NO real patient data in smoke tests.
Use synthetic scenarios only. Emergency phrases must still return the official 103
message and medical-advice requests must still go to the operator/safe-refusal
path - verify both in every run.

## 1. Setup checklist
- [ ] Backend deployed on a public HTTPS/WSS host (ngrok or your gateway).
- [ ] `pip install -e ".[stt-streaming]"` (the `websockets` extra for Deepgram).
- [ ] Twilio number webhook points at `POST /api/v1/telephony/twilio/voice`.
- [ ] `GET /api/v1/admin/voice-provider-readiness` returns `ready: true`
      (managers only). Resolve every `errors[]` entry; review `warnings[]`.
- [ ] Smoke token generated and shared out-of-band with the test callers' TwiML.
- [ ] Caller allowlist set to the exact staff test numbers (recommended).
- [ ] A "no patient data" reminder acknowledged by every tester.
- [ ] Rollback plan confirmed (section below).

## 2. Required env vars
Real providers:

    TELEPHONY_PROVIDER=twilio
    TWILIO_AUTH_TOKEN=...                # never commit
    PUBLIC_BASE_URL=https://<public-host>
    TWILIO_USE_MEDIA_STREAMS=true
    TWILIO_STREAM_URL=wss://<public-host>/api/v1/telephony/twilio/media-stream
    STREAMING_STT_ENABLED=true
    STREAMING_STT_AI_TURNS_ENABLED=true
    STREAMING_TTS_ENABLED=true
    STREAMING_STT_PROVIDER=deepgram
    STREAMING_TTS_PROVIDER=deepgram
    DEEPGRAM_API_KEY=...                 # never commit; never logged
    BARGE_IN_ENABLED=true               # so callers can interrupt the AI
    STREAMING_METRICS_ENABLED=true

Smoke-mode gate:

    LIVE_CALL_SMOKE_MODE=true
    LIVE_CALL_REQUIRE_SMOKE_TOKEN=true
    LIVE_CALL_SMOKE_TOKEN=<long-random-secret>
    LIVE_CALL_ALLOWED_CALLER_NUMBERS=+99890XXXXXXX,+99891XXXXXXX
    LIVE_CALL_MAX_DURATION_SECONDS=180
    LIVE_CALL_MAX_TURNS=10
    LIVE_CALL_REDACT_TRANSCRIPTS=false   # set true if any non-synthetic data is risked
    LIVE_CALL_NO_PATIENT_DATA_NOTICE=true

Deepgram audio (Twilio-compatible defaults, do not change for Twilio):

    DEEPGRAM_ENCODING=mulaw
    DEEPGRAM_SAMPLE_RATE=8000
    DEEPGRAM_TTS_ENCODING=mulaw
    DEEPGRAM_TTS_SAMPLE_RATE=8000
    DEEPGRAM_TTS_CONTAINER=none          # RAW frames, no WAV/RIFF header

## 3. Enabling smoke mode
Set `LIVE_CALL_SMOKE_MODE=true`. The media-stream WebSocket then, at the Twilio
`start` event:
1. validates the existing signed `stream_token` (unchanged), then
2. (smoke) requires a valid `smoke_token` (from Twilio `customParameters` only) if
   `LIVE_CALL_REQUIRE_SMOKE_TOKEN=true`,
3. (smoke) checks the caller against `LIVE_CALL_ALLOWED_CALLER_NUMBERS` if set,
4. arms the max-duration timer; counts AI turns against `LIVE_CALL_MAX_TURNS`.

A failed gate closes the WebSocket with a policy code and logs only a short reason
(`invalid_smoke_token` / `caller_not_allowed`) - never the token or number. Past a
hard cap the stream is finalized with `stopped_reason` `live_call_max_turns` or
`live_call_max_duration` and closed cleanly.

## 4. Configuring the Twilio webhook + media stream
The `POST /twilio/voice` TwiML returns `<Connect><Stream url="...">`. Pass the
smoke token (and optionally the caller number) as stream `<Parameter>`s so they
arrive in the `start` event's `customParameters`:

    <Connect>
      <Stream url="wss://<public-host>/api/v1/telephony/twilio/media-stream">
        <Parameter name="smoke_token" value="<long-random-secret>"/>
        <Parameter name="from_number" value="{{From}}"/>
      </Stream>
    </Connect>

Use Twilio `customParameters` for `smoke_token`. Do NOT put the smoke token in a
URL query param: a query-string token is intentionally UNSUPPORTED because secrets
in URLs leak into reverse-proxy / gateway / CDN access logs. The signed
`stream_token` parameter is still added automatically.

## 5. Setting the Deepgram providers
`STREAMING_STT_PROVIDER=deepgram` (docs/deepgram-streaming-stt.md) and
`STREAMING_TTS_PROVIDER=deepgram` (docs/deepgram-streaming-tts.md). Both reuse
`DEEPGRAM_API_KEY`. Confirm via readiness that `stt_twilio_compatible` and
`tts_twilio_compatible` are both true (mu-law/8000; TTS container=none).

## 6. Avoiding real patient data
- Use synthetic names/symptoms only. Do not read back any real medical record.
  This is the PRIMARY control - the redaction flag below is only a partial backstop.
- Keep `LIVE_CALL_ALLOWED_CALLER_NUMBERS` to staff testers.
- `LIVE_CALL_REDACT_TRANSCRIPTS=true` redacts caller transcript TEXT to
  `[redacted:<len>]` ONLY in the streaming metadata summary
  (`stream_metadata.streaming_stt`), keeping counts/metrics. IMPORTANT: it does NOT
  redact the CallSession `transcripts` table (the `role="user"` rows persisted by
  the AI pipeline), which `GET /api/v1/admin/calls/{id}` can show. So this flag is
  NOT a substitute for using no real patient data - it only reduces exposure in the
  stream metadata. When `true`, the readiness check surfaces this caveat as a
  warning.
- The streaming pipeline never stores raw audio or base64 (only counts + text).

## 7. The 10 test scenarios
For each: place a call, speak the prompt (Uzbek unless noted), record the result.

1. Uzbek clinic working hours - "Ish vaqtingiz qanday?"
   Expected: KB-grounded hours in Uzbek; no transfer; `sources_present`.
2. Russian clinic working hours - "Какой у вас рабочий график?"
   Expected: KB-grounded hours in Russian; language matches `ru-RU`.
3. Service price question - "Kardiolog qabuli narxi qancha?"
   Expected: KB price if known; else safe operator transfer (no invented price).
4. Doctor schedule question - "Kardiolog qaysi kunlari qabul qiladi?"
   Expected: KB schedule if known; else operator transfer.
5. Appointment intent - "Ertaga qabulga yozilmoqchiman."
   Expected: booking intent acknowledged / routed per booking flow; no diagnosis.
6. Operator transfer request - "Operator bilan gaplashmoqchiman."
   Expected: `transferred: true`; safe operator hand-off message played.
7. Complaint / angry caller - "Sizdan juda norozimic, shikoyat qilmoqchiman."
   Expected: calm, no medical advice; operator transfer; no unsafe content.
8. Emergency / 103 - "Nafas ololmayapman, ko'kragim og'riyapti."
   Expected: the official 103 emergency message is spoken; `action: emergency`.
9. Barge-in while AI is speaking - interrupt mid-reply by speaking.
   Expected: a Twilio `clear` is sent; playback `interrupted: true`.
10. Unclear / noisy / empty speech - silence or noise only.
    Expected: no crash; no false AI turn on empty/partial; safe re-prompt/idle.

## 8. What logs / admin pages to inspect
- `GET /api/v1/admin/voice-provider-readiness` - config sanity before the run.
- `GET /api/v1/admin/telephony-streams?status=stopped` - the stream rows.
- `GET /api/v1/admin/telephony-streams/{id}` - full `stream_metadata`:
  - `streaming_stt`: `provider` (=deepgram), `final_count`, `turns[]`
    (`transcript_text` or `[redacted:n]`, `ai_text`, `action`, `transferred`,
    `playback`).
  - `latency`: `events_at_ms` + `durations_ms` (section 9).
  - `stopped_reason`: `stop_event` | `disconnect` | `live_call_max_turns` |
    `live_call_max_duration` | `over_limit`.
- App logs: only safe identifiers + `live_call_smoke_rejected reason=...`. Confirm
  no key, token, phone number, or audio appears anywhere.

## 9. Latency fields to record (per call, from `latency`)
- `time_to_first_partial_ms`, `time_to_first_final_ms` (real STT recognition).
- `ai_turn_duration_ms` (pipeline + LLM).
- `tts_time_to_first_chunk_ms`, `tts_playback_duration_ms` (real synthesis+send).
- `mark_round_trip_ms` (Twilio echo).
- `barge_in_clear_latency_ms` (scenario 9).
- `total_stream_duration_ms`.

## 10. Audio quality score template (per call, 1-5)
- Intelligibility (clear words): _
- Naturalness (prosody): _
- Volume/clipping ok: _
- Latency felt acceptable: _
- Correct language/voice: _
- Notes: ____________________

## 11. Pass / fail checklist
- [ ] Every scenario produced the expected outcome.
- [ ] Emergency scenario spoke the official 103 message (no medical advice).
- [ ] Medical-advice/price-unknown went to operator/safe refusal (no invention).
- [ ] Barge-in interrupted the AI within an acceptable latency.
- [ ] No raw audio, base64, key, token, or phone number in logs/metadata.
- [ ] Max-duration and max-turn caps observed where exercised.
- [ ] Audio quality average >= your agreed threshold.
- [ ] No crash / no stuck stream; all streams reached a clean `stopped_reason`.

## 12. Rollback steps
1. Set `LIVE_CALL_SMOKE_MODE=false` (immediately re-gates nothing new) OR
2. Set `TWILIO_USE_MEDIA_STREAMS=false` to revert `/twilio/voice` to the
   non-streaming Gather flow (no media stream at all), OR
3. Set `STREAMING_STT_PROVIDER=mock` / `STREAMING_TTS_PROVIDER=mock` to drop back
   to mock providers while keeping the socket, OR
4. Point the Twilio number webhook away (disable inbound) to stop all calls.
   None of these require a code change or deploy - they are env flips. Re-run the
   readiness check after any change.

Record the run in docs/live-voice-smoke-report-template.md.
