# Twilio Media Streams (WebSocket spike)

This is a SPIKE: a WebSocket endpoint that accepts Twilio Media Streams events,
tracks the stream lifecycle, and counts media frames/bytes. It is NOT real-time
voice AI yet. The base spike does NOT transcribe/synthesize/interrupt; those are
layered on top behind flags (all mock-first, default OFF), documented below:
- streaming STT + AI turns (docs/streaming-stt.md),
- streaming TTS playback (docs/streaming-tts-playback.md),
- barge-in + clear/mark handling (docs/barge-in.md).
Still no real VAD/endpointing and no outbound dialing.

The existing Twilio Gather/SpeechResult flow, the mock telephony flow, and
`/voice/simulate` are all unchanged.

## Enabling it
Set in `.env`:
- `TWILIO_USE_MEDIA_STREAMS=true`
- `TWILIO_STREAM_URL=wss://<public-host>/api/v1/telephony/twilio/media-stream`

When enabled, `POST /api/v1/telephony/twilio/voice` returns TwiML that connects
the call to the WebSocket instead of the Gather flow:

    <?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice" language="ru-RU">[greeting]</Say><Connect><Stream url="wss://example.test/api/v1/telephony/twilio/media-stream"/></Connect></Response>

When `TWILIO_USE_MEDIA_STREAMS=false` (default), `/twilio/voice` returns the
unchanged Gather/SpeechResult TwiML.

## WebSocket endpoint
`WebSocket /api/v1/telephony/twilio/media-stream`. Twilio sends newline-delimited
JSON text frames:
- `connected` - protocol handshake (ignored).
- `start` - carries `streamSid`, `start.callSid`, `start.tracks`,
  `start.mediaFormat`. A `TelephonyStream` row is created (linked to the
  `TelephonyCall` by `callSid` when one exists). Only a SAFE subset of the start
  payload is stored (tracks + media format); `customParameters` are NOT stored.
- `media` - `media.payload` is base64 (mu-law). It is base64-decoded ONLY to
  measure size and validate; the raw payload is never logged or persisted. Frame
  and byte counters are updated.
- `stop` - marks the stream stopped and closes the socket.
- `mark` and unknown events are ignored.

### Safety / limits
- Raw audio payloads are never logged or stored (only counts + safe metadata).
- Malformed JSON or a non-object event closes the socket with code 1003.
- Invalid base64 in a media frame is counted as a zero-byte frame (no crash).
- `TWILIO_STREAM_MAX_FRAME_BYTES` caps the bytes counted per frame.
- `TWILIO_STREAM_MAX_FRAMES_PER_CALL` caps how many frames are processed; beyond
  it, frames are ignored. No audio is buffered to disk or DB.
- If the socket drops mid-stream, the stream is still marked stopped.

## Data model
`TelephonyStream` (migration `0011_telephony_streams`): id, provider,
provider_call_id, stream_sid, telephony_call_id (nullable), status
(active|stopped), media_frames_count, media_bytes_count, last_sequence_number,
stream_metadata (safe JSON), started_at, stopped_at, created_at, updated_at.

`TelephonyStreamService`: start_stream / record_media_frame / stop_stream /
list / get.

## Admin read endpoints (super_admin/admin only)
- `GET /api/v1/admin/telephony-streams?call_sid=&status=&limit=&offset=`
- `GET /api/v1/admin/telephony-streams/{id}`
Operators get 403; unauthenticated gets 401. No audio is ever exposed.

## Local public wss setup (ngrok)
    cd backend && uvicorn app.main:app --port 8000
    ngrok http 8000
    # .env:
    #   TELEPHONY_PROVIDER=twilio
    #   TWILIO_AUTH_TOKEN=...
    #   PUBLIC_BASE_URL=https://<your-ngrok>.ngrok.io
    #   TWILIO_USE_MEDIA_STREAMS=true
    #   TWILIO_STREAM_URL=wss://<your-ngrok>.ngrok.io/api/v1/telephony/twilio/media-stream
The Twilio console "A call comes in" webhook still points to
`https://<your-ngrok>.ngrok.io/api/v1/telephony/twilio/voice` (HTTP POST); the
TwiML it returns tells Twilio to open the media-stream WebSocket.

## Test strategy
- WebSocket protocol is tested with Starlette's `TestClient.websocket_connect`
  against a dedicated in-memory SQLite engine: connected/start/media/stop is
  accepted; malformed JSON and invalid base64 close/continue safely.
- DB-backed counting (frame/byte counters, caps, stop, call linking) is tested
  directly against `TelephonyStreamService` with the async session fixture.
- No real Twilio account, token, or network is used; no audio is asserted by
  value.

## Streaming STT + AI turns (mock-first) - now available behind flags
A streaming STT architecture is wired on top of this spike. With
`TWILIO_USE_MEDIA_STREAMS=true` AND `STREAMING_STT_ENABLED=true`, media frames are
fed to a (mock) `StreamingSTTSessionService` and a safe transcript summary is
attached to `TelephonyStream.stream_metadata.streaming_stt` on stop/disconnect.
The STT itself is mock-only (no real recognition).

A REAL streaming STT provider (Deepgram) is available opt-in behind the same
interface: `STREAMING_STT_PROVIDER=deepgram` (default `mock`). Interim Deepgram
results drive barge-in; finals drive the AI turn; tests use a fake connection (no
network). See docs/deepgram-streaming-stt.md.

A FINAL transcript now creates an AI TEXT turn: when
`STREAMING_STT_AI_TURNS_ENABLED=true` and a CallSession is linked to the stream,
the final transcript is routed once through the full AI/safety pipeline
(`CallSessionService.handle_message`) and the safe turn (ai_text, action,
reason_code, transferred, sources, ...) is persisted under `streaming_stt.turns`.
Partials never call the AI. See docs/streaming-stt.md for the turn structure,
limits, and safety guarantees.

## Streaming TTS playback - now available behind flags
A FINAL transcript's AI reply can now be streamed BACK to Twilio. With the STT +
AI-turn flags on AND `STREAMING_TTS_ENABLED=true`, the turn's `ai_text` is
synthesized and sent over the SAME socket as `media` frames (base64 audio)
followed by a `mark` event. Emergency / operator-transfer turns voice the official
SAFE reply (103 / operator message). A safe playback summary (provider, chunks,
bytes, mark name, degraded) is stored under each turn's `playback` block - never
raw audio or base64. The default provider is a deterministic MOCK; a REAL Deepgram
TTS provider is opt-in (`STREAMING_TTS_PROVIDER=deepgram`) emitting raw mu-law/8k
frames Twilio can play, with this same media/mark path doing the base64 +
chunking (docs/deepgram-streaming-tts.md). Default is OFF (AI turn persisted, no
outbound media). See docs/streaming-tts-playback.md.

## Barge-in (mock-first) - now available behind flags
With `BARGE_IN_ENABLED=true`, caller speech (a streaming partial/final transcript)
during active playback sends a Twilio `clear` event to flush the queued audio and
marks the playback `interrupted` in metadata. Incoming Twilio `mark` echoes
complete a playback (`status=completed`). There is no real VAD - the transcript IS
the speech signal. Default OFF. See docs/barge-in.md.

## Latency metrics (instrumentation only) - now available
Numeric latency instrumentation is recorded for the streaming pipeline (event
offsets + durations in ms) and attached to `stream_metadata.latency`, with
per-turn `metrics`. It is on by default (`STREAMING_METRICS_ENABLED=true`), holds
numbers only (no audio/payloads), and is visible via the admin stream-detail
endpoint. It measures the mock pipeline today; the same hooks measure REAL
provider latency once integrated. See docs/streaming-latency-metrics.md.

## Live-call smoke mode (controlled pilot gate) - now available behind flags
For a controlled REAL call test (real Twilio + Deepgram STT + Deepgram TTS) before
clinic usage, `LIVE_CALL_SMOKE_MODE=true` (default OFF) gates this WebSocket at the
`start` event: optional smoke token (Twilio customParameters ONLY - query-string
tokens are intentionally unsupported to avoid proxy/access-log leakage) +
optional caller allowlist, plus hard caps `LIVE_CALL_MAX_TURNS` /
`LIVE_CALL_MAX_DURATION_SECONDS` (clean stop with `stopped_reason`
`live_call_max_turns` / `live_call_max_duration`). The token/number are never
logged (only a safe reason code) and `LIVE_CALL_REDACT_TRANSCRIPTS` can redact
caller transcript text. Validate config first with
`GET /api/v1/admin/voice-provider-readiness` (config only, no key/token leak). Full
runbook + scenarios: docs/live-voice-smoke-test.md (report template:
docs/live-voice-smoke-report-template.md).

## Next steps toward real-time voice
1. Real streaming STT provider (Azure/Deepgram/OpenAI realtime) behind
   `StreamingSTTProvider`: feed mu-law frames to a streaming recognizer; emit
   partial + final transcripts (the mock provider already defines this contract).
2. Real turn endpointing: trigger the AI turn from a provider end-of-utterance /
   silence signal instead of the mock's frame-count heuristic (the final ->
   AI-turn wiring already exists).
3. Real streaming TTS provider behind `StreamingTTSProvider`, emitting mu-law/8k
   frames Twilio can actually play - DONE (A30, Deepgram opt-in;
   docs/deepgram-streaming-tts.md). The media/mark playback path is reused.
4. Real VAD / endpointing as the barge-in signal (barge-in itself is wired in
   A27 - docs/barge-in.md - using transcript events as the speech signal).
5. Audio-quality metrics and a live voice eval on top of the text evals (real STT
   + TTS providers are now wired; latency metrics already exist - A28).
6. Optionally persist a placeholder inbound `AudioRecording`
   (kind=user_audio, content_type=audio/x-mulaw) once buffering is designed
   safely (NOT done in this spike).
