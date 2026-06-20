# Streaming STT (mock-first architecture)

This adds a STREAMING speech-to-text architecture on top of the Twilio Media
Streams WebSocket spike. The default is a deterministic MOCK provider; a REAL
provider (Deepgram) is now available opt-in (see below):
- the mock does not decode audio (deterministic tests, no external calls),
- a real streaming STT provider (Deepgram) is implemented behind this same
  interface - `STREAMING_STT_PROVIDER=deepgram`, docs/deepgram-streaming-stt.md,
- no streaming TTS back to the caller and no audio is sent back, no barge-in.

A FINAL transcript IS now routed once through the full AI/safety pipeline and the
safe AI turn is persisted (see "Streaming AI turns" below). Partial transcripts
never call the AI. This produces a TEXT turn only - there is still NO streaming
TTS / audio playback.

The non-streaming Twilio Gather/SpeechResult flow, mock telephony, and
`/voice/simulate` are all unchanged.

## Components
- `StreamingAudioFrame` (dataclass): stream_sid, call_sid, sequence_number,
  timestamp_ms, payload_bytes (decoded audio, transient - never logged/persisted),
  codec (default `audio/x-mulaw`), track.
- `TranscriptEvent` (dataclass): text, language, is_final, provider, confidence
  (nullable), timestamp_ms (nullable), metadata (safe dict), `event_id` (nullable;
  a stable, unique id per FINAL event used for dedup - see the provider contract).
- `StreamingSTTProvider` (interface): `start_stream(context) -> StreamingSTTSession`.
- `StreamingSTTSession` (interface): `accept_audio_frame(frame) -> [TranscriptEvent]`,
  `finish_stream() -> [TranscriptEvent]`, `close()`.
- `MockStreamingSTTProvider`: deterministic. Emits one partial near the middle and
  one final at `STREAMING_STT_FINAL_AFTER_FRAMES` frames. The phrase comes from
  the start event's `customParameters["test_phrase"]` (default `Salom`), so tests
  are fully deterministic without any real audio.
- `StreamingSTTSessionService`: owns one session per stream, counts frames/bytes,
  enforces memory limits, tracks partial/final transcripts, handles provider
  errors safely (marks the session `degraded`, never crashes), and builds a SAFE
  `summary()`.

## Streaming AI turns (final transcript -> AI text turn)
When `STREAMING_STT_AI_TURNS_ENABLED` is on and a CallSession is linked to the
stream (via the TelephonyCall created at `/twilio/voice`), each FINAL transcript
is routed once through `CallSessionService.handle_message` - the SAME path the
text simulation and the Twilio Gather flow use. So every safety guarantee is
preserved end to end:
- pre-LLM medical guard (diagnosis/medicine/dosage/treatment/emergency),
- KB grounding + anti-hallucination (factual question with no KB data -> operator),
- post-LLM output reviewer (defense-in-depth),
- operator-transfer decision (priority, callback).

Rules and limits:
- partials NEVER call the AI; only a FINAL transcript does;
- a RE-DELIVERY of the same final event (same `event_id`) does NOT double-call the
  AI; but two SEPARATE utterances that share the same text each create their own
  turn (dedup is by `event_id`, never by text);
- transcript text is capped to `STREAMING_STT_MAX_TRANSCRIPT_CHARS` per turn;
- at most `STREAMING_STT_MAX_TURNS` turns per stream (then further finals are
  ignored and `turns_over_limit` is set), bounding metadata growth;
- if the AI/pipeline errors, the turn is marked `degraded` and the WebSocket does
  NOT crash;
- a safety transfer/emergency persists the correct `action`/`reason_code` and the
  official 103 message - never unsafe medical advice.

Provider contract: a real `StreamingSTTProvider` MUST set a stable, unique
`event_id` on every FINAL `TranscriptEvent` (a monotonic per-stream index or the
recognizer's final id). The mock does this as `"{stream_sid}:final:{n}"`. If a
provider omits `event_id`, the turn layer falls back conservatively to object
identity - it only suppresses the exact same event object delivered twice and
will otherwise treat each final as a new turn (it never dedups by text).

Each turn (stored under `streaming_stt.turns`) holds ONLY safe text:
`order`, `transcript_text`, `transcript_language`, `transcript_confidence`,
`transcript_truncated`, `ai_text`, `action`, `reason_code`, `transferred`,
`transfer_reason`, `priority`, `callback_required`, `language`, `sources`
([{id,title}]), `created_at`, `degraded`, `error`. No raw audio/base64, no secrets
or provider payloads. The AI text is produced but NOT synthesized or sent back.

Sample `stream_metadata.streaming_stt` after one emergency final:

    {
      "provider": "mock", "frames_processed": 2, "bytes_processed": 320,
      "partial_count": 1, "final_count": 1,
      "final_transcripts": [{"text": "Nafas ololmayapman", "language": "uz-UZ", "confidence": 0.9}],
      "stopped_reason": "stop_event", "errors": 0, "degraded": false, "over_limit": false,
      "turn_count": 1, "turns_over_limit": false,
      "turns": [{
        "order": 0, "transcript_text": "Nafas ololmayapman", "transcript_language": "uz-UZ",
        "transcript_confidence": 0.9, "transcript_truncated": false,
        "ai_text": "Bu holat shoshilinch tibbiy yordam talab qilishi mumkin. Iltimos, darhol 103 ...",
        "action": "emergency", "reason_code": "emergency", "transferred": true,
        "transfer_reason": "emergency", "priority": "urgent", "callback_required": false,
        "language": "uz-UZ", "sources": [], "created_at": "2026-06-18T...Z",
        "degraded": false, "error": null
      }]
    }

## Summary (attached to TelephonyStream.stream_metadata.streaming_stt)
Counts + recognized transcript TEXT only - NEVER raw audio or any base64 payload:
- `provider`, `frames_processed`, `bytes_processed`, `partial_count`,
  `final_count`, `final_transcripts` (text/language/confidence), `stopped_reason`
  (stop_event | disconnect | over_limit | finished | degraded), `errors`,
  `degraded`, `over_limit`.
It is visible in `GET /api/v1/admin/telephony-streams/{id}` (super_admin/admin).

## WebSocket integration
In `/api/v1/telephony/twilio/media-stream` (after stream-token auth):
- `start` -> create the TelephonyStream as before; if streaming is enabled, open a
  `StreamingSTTSessionService` (reading `customParameters`).
- `media` -> base64-decode the payload to transient bytes (malformed -> empty,
  never logged), build a `StreamingAudioFrame`, push it to the session, and
  collect partial/final events. The AI is NOT called here (even on a final).
- over limit -> finalize + attach summary + stop + close (1000).
- `stop` / WebSocket disconnect -> finalize the session and attach the summary to
  `stream_metadata` (`stopped_reason` = `stop_event` / `disconnect`).
- A duplicate `start` on the same socket (Twilio sends exactly one) is REJECTED:
  the existing stream/session is finalized (`stopped_reason=superseded`, session
  closed once, no orphan left active) and the socket is closed with a policy
  violation. The media payload is base64-decoded at most ONCE per frame (shared
  helper `decode_media_payload`, with a pre-decode size cap that rejects oversized
  frames before allocating); the decoded bytes are reused for counting and the
  streaming frame.

Streaming runs ONLY when both flags are on (otherwise the WS behaves exactly as
the earlier counting-only spike):
- `TWILIO_USE_MEDIA_STREAMS=true`
- `STREAMING_STT_ENABLED=true`

## Env flags
- `TWILIO_USE_MEDIA_STREAMS` - when true, `/twilio/voice` returns `<Connect><Stream>`.
- `STREAMING_STT_ENABLED` - default false; enable streaming STT on the media stream.
- `STREAMING_STT_PROVIDER=mock` (default) `| deepgram` (real, opt-in -
  docs/deepgram-streaming-stt.md; `DEEPGRAM_*` config; fails fast without a key).
- `STREAMING_STT_MAX_FRAMES` (default 10000) - per-stream frame cap; over it the
  socket finalizes + closes safely.
- `STREAMING_STT_MAX_BYTES` (default 8000000) - per-stream byte cap.
- `STREAMING_STT_FINAL_AFTER_FRAMES` (default 25) - mock: emit the final after N
  frames (set small for local tests).
- `STREAMING_STT_AI_TURNS_ENABLED` (default true) - run an AI text turn on each
  final transcript (text only; no streaming TTS).
- `STREAMING_STT_MAX_TURNS` (default 50) - per-stream AI-turn cap.
- `STREAMING_STT_MAX_TRANSCRIPT_CHARS` (default 2000) - per-turn transcript cap.

## Testing locally with mock frames
Enable media streams + streaming STT (see docs/twilio-media-streams.md for the
ngrok/wss setup), set `STREAMING_STT_FINAL_AFTER_FRAMES=2`, then connect a
WebSocket client to `/api/v1/telephony/twilio/media-stream` and send:

    {"event":"connected","protocol":"Call","version":"1.0.0"}
    {"event":"start","streamSid":"MZ1","start":{"streamSid":"MZ1","callSid":"CA1",
      "tracks":["inbound"],"mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1},
      "customParameters":{"call_sid":"CA1","stream_token":"<signed>","test_phrase":"Klinika manzili qayerda"}}}
    {"event":"media","sequenceNumber":"2","media":{"track":"inbound","payload":"<base64 mu-law>"}}
    {"event":"media","sequenceNumber":"3","media":{"track":"inbound","payload":"<base64 mu-law>"}}
    {"event":"stop","streamSid":"MZ1","stop":{}}

After stop, `GET /api/v1/admin/telephony-streams/{id}` shows
`stream_metadata.streaming_stt` with `final_transcripts: [{"text":"Klinika manzili qayerda", ...}]`.
The `stream_token` is required (signed, expiring) and is never stored/logged.

## What is implemented vs not
Implemented: streaming provider/session interfaces, mock provider, session
service with limits + safe error handling, WebSocket wiring, safe summary,
persistence to stream metadata, admin visibility, AI text turn on a FINAL
transcript through the full safety pipeline (deduped + capped + degraded-safe),
turn persistence under `streaming_stt.turns`, full test coverage.

NOT implemented: real streaming STT provider, streaming TTS / audio playback back
to Twilio, barge-in, real end-of-utterance endpointing (the mock decides finals),
latency metrics.

## Next steps toward a real-time voice pilot
1. Real streaming STT provider: Deepgram is implemented
   (docs/deepgram-streaming-stt.md); Azure / OpenAI realtime can follow behind the
   same `StreamingSTTProvider` interface.
2. Real turn endpointing: detect end-of-utterance from the provider (silence /
   provider final) to decide turns, instead of the mock's frame-count heuristic.
3. Streaming TTS / playback: synthesize the `ai_text` from each turn and send
   mu-law `media` frames back over the same socket, with `mark` events for
   playback tracking. (This is the immediate next milestone; the AI text turn is
   already produced and persisted.)
4. Barge-in: stop outbound TTS when inbound speech resumes (`clear` event).
5. Latency + audio-quality metrics and a live voice eval on top of the text evals.
