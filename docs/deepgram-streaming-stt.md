# Real streaming STT provider: Deepgram (A29)

This adds a REAL streaming speech-to-text adapter for Deepgram behind the existing
`StreamingSTTProvider` interface. It is a drop-in: select it with
`STREAMING_STT_PROVIDER=deepgram`. The mock remains the default and nothing else
in the pipeline (AI turns, TTS playback, barge-in, latency metrics) changes - the
adapter just produces real `TranscriptEvent`s instead of mock ones.

Tests NEVER call Deepgram or the network: a fake connection/connector is injected.

## How it fits the pipeline
Twilio media frame (decoded mu-law bytes) -> `DeepgramStreamingSession.accept_audio_frame`
sends the bytes to Deepgram and drains any available transcript messages:
- interim Deepgram result -> partial `TranscriptEvent` -> drives BARGE-IN
  (docs/barge-in.md): an interim during active playback sends a Twilio `clear`.
- final Deepgram result -> final `TranscriptEvent` -> drives the AI TURN
  (docs/streaming-stt.md) and TTS playback (docs/streaming-tts-playback.md).
- the A28 latency tracker marks `first_partial_transcript_at` /
  `first_final_transcript_at` from these events; `ai_turn_duration`, playback
  durations, `mark_round_trip`, and `barge_in_clear_latency` are unchanged.

Empty transcripts are ignored. Finals get a monotonic local `event_id`
(`<stream_sid>:dg:<n>`) so the turn layer dedups a re-delivered SAME event without
conflating two separate utterances. Transcript length and provider metadata are
bounded. No raw audio or base64 is ever logged or persisted.

## Connection design (testable, no network in tests)
- `DeepgramConnection` (send_audio / recv / finish / close) and `DeepgramConnector`
  (connect) are small injectable protocols.
- Production: `WebsocketsDeepgramConnector` lazy-imports `websockets` (opt-in
  extra: `pip install -e ".[stt-streaming]"`). If the package is missing while
  `provider=deepgram`, connect fails with a clear error (the session degrades; the
  WebSocket does not crash).
- Tests inject a fake connection with scripted messages - no `websockets`, no key,
  no network.

The session connects LAZILY on the first audio frame. The API key travels ONLY in
the `Authorization: Token <key>` connect header - never in a URL, log, or metadata.

## Error handling (never crashes the Twilio WebSocket)
- connect failure -> session degraded (the session service marks it; no crash).
- send failure -> degraded; the connection is best-effort closed.
- receive/parse failure -> degraded.
- malformed / non-transcript messages -> ignored (not degraded).
- close is best-effort. Error strings in metadata are short and carry no secret or
  payload.

## Config (.env)
- `STREAMING_STT_PROVIDER=deepgram` (default `mock`).
- `DEEPGRAM_API_KEY` - REQUIRED when provider=deepgram; empty -> fail fast at
  startup with a clear configuration error. Never logged/persisted.
- `DEEPGRAM_MODEL` (default `nova-2`).
- `DEEPGRAM_LANGUAGE` - optional (e.g. `ru`, `multi`); empty -> provider default.
- `DEEPGRAM_ENCODING` (default `mulaw`) and `DEEPGRAM_SAMPLE_RATE` (default `8000`)
  match Twilio Media Streams audio.
- `DEEPGRAM_INTERIM_RESULTS` (default true) - emit partials that drive barge-in.
- `DEEPGRAM_ENDPOINTING` - optional (`false` or ms, e.g. `300`).
- `DEEPGRAM_CONNECT_TIMEOUT_SECONDS`, `DEEPGRAM_RECEIVE_TIMEOUT_SECONDS`,
  `DEEPGRAM_MAX_MESSAGE_BYTES`, `DEEPGRAM_MAX_TRANSCRIPT_CHARS`.

## Sample mapping (fake Deepgram message -> TranscriptEvent)
Interim:

    {"type":"Results","is_final":false,
     "channel":{"alternatives":[{"transcript":"ish","confidence":0.9}]}}
    -> TranscriptEvent(text="ish", is_final=false, provider="deepgram",
                       confidence=0.9, event_id=None)

Final:

    {"type":"Results","is_final":true,
     "channel":{"alternatives":[{"transcript":"ish vaqtingiz qanday","confidence":0.95}]}}
    -> TranscriptEvent(text="ish vaqtingiz qanday", is_final=true,
                       provider="deepgram", confidence=0.95, event_id="MZ:dg:0")

## What is implemented vs not
Implemented: Deepgram adapter behind `StreamingSTTProvider`, injectable
connection/connector, interim->partial / final->final parsing, lazy connect,
fail-fast on missing key, degrade-on-failure, bounded text/metadata, key-in-header
only, full fake-connection test coverage, and end-to-end WS tests (latency +
barge-in + AI turn driven by fake Deepgram events).

NOT implemented: real streaming TTS (still mock), OpenAI Realtime / Azure STT,
Deepgram `speech_final`/utterance-end tuning (each `is_final` is treated as a turn
segment), reconnect/backpressure, language auto-detect from Deepgram metadata
(language is detected from the transcript text, as with the mock).

## Next step
Real streaming TTS provider integration, so the AI reply is synthesized by a real
provider and streamed back (the playback path + latency hooks already exist).
