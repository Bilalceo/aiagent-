# Twilio Media Streams (WebSocket spike)

This is a SPIKE: a WebSocket endpoint that accepts Twilio Media Streams events,
tracks the stream lifecycle, and counts media frames/bytes. It is NOT real-time
voice AI yet. There is:
- no streaming STT (we do not transcribe frames),
- no streaming TTS (we do not synthesize audio back),
- no barge-in, no turn endpointing, no outbound dialing.

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

A FINAL transcript now creates an AI TEXT turn: when
`STREAMING_STT_AI_TURNS_ENABLED=true` and a CallSession is linked to the stream,
the final transcript is routed once through the full AI/safety pipeline
(`CallSessionService.handle_message`) and the safe turn (ai_text, action,
reason_code, transferred, sources, ...) is persisted under `streaming_stt.turns`.
Partials never call the AI. See docs/streaming-stt.md for the turn structure,
limits, and safety guarantees.

## Streaming TTS playback (mock-first) - now available behind flags
A FINAL transcript's AI reply can now be streamed BACK to Twilio. With the STT +
AI-turn flags on AND `STREAMING_TTS_ENABLED=true`, the turn's `ai_text` is
synthesized (mock) and sent over the SAME socket as `media` frames (base64 audio)
followed by a `mark` event. Emergency / operator-transfer turns voice the official
SAFE reply (103 / operator message). A safe playback summary (provider, chunks,
bytes, mark name, degraded) is stored under each turn's `playback` block - never
raw audio or base64. It is mock-only (the bytes are not real playable audio), and
there is NO barge-in. Default is OFF (AI turn persisted, no outbound media). See
docs/streaming-tts-playback.md.

## Next steps toward real-time voice
1. Real streaming STT provider (Azure/Deepgram/OpenAI realtime) behind
   `StreamingSTTProvider`: feed mu-law frames to a streaming recognizer; emit
   partial + final transcripts (the mock provider already defines this contract).
2. Real turn endpointing: trigger the AI turn from a provider end-of-utterance /
   silence signal instead of the mock's frame-count heuristic (the final ->
   AI-turn wiring already exists).
3. Real streaming TTS provider behind `StreamingTTSProvider`, emitting mu-law/8k
   frames Twilio can actually play (the media/mark playback path already exists).
4. Barge-in: stop outbound TTS when inbound speech is detected (`clear` event
   builder already exists, not wired).
5. Latency + audio-quality metrics and a live voice eval on top of the text evals.
6. Optionally persist a placeholder inbound `AudioRecording`
   (kind=user_audio, content_type=audio/x-mulaw) once buffering is designed
   safely (NOT done in this spike).
