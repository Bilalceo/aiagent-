# Streaming voice latency metrics (instrumentation only)

This adds latency instrumentation to the (still mock-first) streaming voice
pipeline so future REAL STT/TTS providers can be debugged and evaluated. It does
NOT change STT, AI-turn, TTS, or barge-in behavior - it only records numbers.

Safety: the persisted summary holds ONLY event names + numeric ms values (and
optional wall-clock ISO timestamps when explicitly enabled). Never raw audio,
base64, secrets, or provider payloads.

## Component
- `StreamingLatencyTracker` (pure, DB-free) with an INJECTABLE monotonic clock so
  tests are deterministic. `mark(name)` records an event (first-occurrence wins;
  `once=False` keeps the latest, e.g. last_media_frame). `set_duration(name, ms)`
  injects a precomputed duration (e.g. TTS first-chunk time). `summary()` returns
  the safe metadata block.

Durations use a monotonic clock; wall-clock ISO timestamps are added only when
`STREAMING_METRICS_INCLUDE_TIMESTAMPS=true` (default false).

## Tracked events (stream_metadata.latency.events_at_ms)
Each is an integer ms OFFSET from `websocket_connected_at` (= 0):
- `websocket_connected_at` - socket accepted
- `stream_started_at` - Twilio `start` processed
- `first_media_frame_at`, `last_media_frame_at`
- `first_partial_transcript_at`, `first_final_transcript_at`
- `ai_turn_started_at`, `ai_turn_completed_at`
- `tts_playback_started_at`, `first_tts_chunk_sent_at`, `tts_playback_completed_at`
- `mark_received_at` - Twilio echoed the playback `mark`
- `clear_sent_at` - a barge-in `clear` was sent
- `stream_stopped_at` - finalize (stop/disconnect/over-limit/superseded)

(The first AI turn / first playback set the global events; per-turn detail is on
each turn, below.)

## Durations (stream_metadata.latency.durations_ms)
- `time_to_first_media_ms` = first_media_frame - connected
- `time_to_first_partial_ms`, `time_to_first_final_ms`
- `ai_turn_duration_ms` = ai_turn_completed - ai_turn_started
- `tts_time_to_first_chunk_ms` - synth+send to the first media frame
- `tts_playback_duration_ms` - first media to the trailing mark sent
- `mark_round_trip_ms` = mark_received - tts_playback_completed (we send the mark
  at playback end; this is the echo round trip)
- `barge_in_clear_latency_ms` - caller-speech event to the `clear` send
- `total_stream_duration_ms` = stream_stopped - connected

Only durations that could be computed are included.

## Per-turn metrics
Each AI turn in `streaming_stt.turns[i]` gets a `metrics` block (ms offsets +
durations): `ai_started_at_ms`, `ai_completed_at_ms`, `ai_duration_ms`,
`playback_started_at_ms`, `first_chunk_sent_at_ms`, `playback_completed_at_ms`,
`playback_duration_ms`. The per-turn `mark_received_at_ms` / `clear_sent_at_ms`
land on that turn's `playback` block (set by the barge/mark handling). Metadata is
bounded (turns are already capped by `STREAMING_STT_MAX_TURNS`).

## Inspecting metrics (admin)
`GET /api/v1/admin/telephony-streams/{id}` (super_admin/admin) returns the full
`stream_metadata`, including `latency` and per-turn `metrics`/`playback`. No
frontend change is required.

## Config (.env)
- `STREAMING_METRICS_ENABLED` (default true) - record latency metrics.
- `STREAMING_METRICS_INCLUDE_TIMESTAMPS` (default false) - also add wall-clock ISO
  times under `latency.timestamps` (safe but more verbose).

When disabled, no `latency` block is attached and turns carry no `metrics`.

Metrics are best-effort: a metrics-collection or persist failure is swallowed and
never crashes the WebSocket or loses the streaming summary.

## What is implemented vs not
Implemented: latency tracker (injectable clock), pipeline event + duration
recording, per-turn metrics, safe persistence to `stream_metadata.latency`, admin
visibility, full test coverage.

NOT implemented: real STT/TTS provider timings (the mock has near-zero synth time),
network/RTT to Twilio, audio-quality metrics, percentile aggregation across calls,
a metrics dashboard.

## Next step
Real streaming STT/TTS provider integration - the same event/duration hooks then
measure REAL recognition and synthesis latency, which is the point of this
instrumentation.
