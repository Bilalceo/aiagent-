# Real streaming TTS provider: Deepgram (A30)

This adds a REAL streaming text-to-speech adapter for Deepgram behind the existing
`StreamingTTSProvider` interface. It is a drop-in: select it with
`STREAMING_TTS_PROVIDER=deepgram`. The mock remains the default and nothing else
in the pipeline (STT, AI turns, barge-in, latency metrics) changes - the adapter
just produces REAL mu-law/8k audio bytes instead of the mock `b"MOCK-TTS:..."`.

Tests NEVER call Deepgram or the network: a fake connection/connector is injected.

## How it fits the pipeline
A FINAL transcript -> AI text turn -> `TwilioPlaybackService.play(...)`:
1. `play()` calls `provider.synthesize(ai_text, language=..., voice=...)`.
2. The Deepgram provider opens a TTS WebSocket, sends the text + a flush, and
   drains the binary audio frames into RAW bytes (no WAV/container header).
3. `play()` (UNCHANGED) chunks those bytes, base64-encodes each chunk ONCE, and
   sends Twilio `media` frames + a trailing `mark`.

The provider returns RAW bytes only. `TwilioPlaybackService` is the single owner of
base64 encoding and of the Twilio `media`/`mark` events, so there is no
double-base64 and the provider introduces no header bytes. The playback summary
shows `provider="deepgram"`; it still holds only safe counts + the mark name (never
raw audio, base64, or the key).

## Why mu-law / 8000 / container=none
Twilio Media Streams carry RAW 8 kHz mu-law audio in each `media` frame's base64
payload. Requesting `encoding=mulaw&sample_rate=8000&container=none` makes Deepgram
emit exactly those raw frames - no WAV/RIFF container header to strip, so the bytes
can be chunked and base64-encoded straight onto the wire and played by Twilio.

## Connection design (testable, no network in tests)
- `DeepgramTTSConnection` (send_text / flush / recv / close) and
  `DeepgramTTSConnector` (connect) are small injectable protocols.
- Production: `WebsocketsDeepgramTTSConnector` lazy-imports `websockets` (opt-in
  extra: `pip install -e ".[stt-streaming]"`, shared with the STT adapter). If the
  package is missing while `provider=deepgram`, connect fails with a clear error
  (the playback degrades; the WebSocket does not crash). It reuses the STT adapter's
  `_header_kwarg_name` shim so it works across `websockets` versions
  (`additional_headers` vs `extra_headers`).
- Tests inject a fake connection with scripted audio frames - no `websockets`, no
  key, no network.

One connection per `synthesize()` call: connect -> `Speak(text)` -> `Flush` ->
drain binary audio until a `Flushed`/`Close` control message (or the recv timeout)
-> best-effort close. The API key travels ONLY in the `Authorization: Token <key>`
connect header - never in a URL, log, metadata, or exception.

## Error handling (never crashes the Twilio WebSocket)
Every failure RAISES out of `synthesize()`; `TwilioPlaybackService.play()` catches
it and returns a degraded summary (`degraded=true`, `error="tts_error"`), so the
WebSocket never crashes:
- connect failure -> playback degraded.
- send-text failure -> degraded; connection best-effort closed.
- flush failure -> degraded; connection best-effort closed.
- receive failure (any non-close, non-timeout error) -> degraded.
- empty text -> no provider call, returns no audio (playback already guards empty
  text as `error="empty_text"` before calling the provider).
- JSON control messages (`Metadata`/`Warning`/unknown) are ignored; only the
  message TYPE is ever parsed, never any payload.
- close is best-effort. Nothing in metadata or logs carries a key or raw payload.

## Config (.env)
- `STREAMING_TTS_PROVIDER=deepgram` (default `mock`).
- `DEEPGRAM_API_KEY` - REQUIRED when provider=deepgram; empty -> fail fast with a
  clear configuration error. Reused from the STT adapter. Never logged/persisted.
- `DEEPGRAM_TTS_MODEL` (default `aura-asteria-en`) - Deepgram voice/model id.
- `DEEPGRAM_TTS_ENCODING` (default `mulaw`), `DEEPGRAM_TTS_SAMPLE_RATE` (default
  `8000`), `DEEPGRAM_TTS_CONTAINER` (default `none`) - RAW Twilio-playable frames.
- `DEEPGRAM_TTS_SPEED` - optional (e.g. `1.1`); empty -> provider default.
- `DEEPGRAM_TTS_CONNECT_TIMEOUT_SECONDS`, `DEEPGRAM_TTS_RECEIVE_TIMEOUT_SECONDS`,
  `DEEPGRAM_TTS_MAX_MESSAGE_BYTES`.
- Text length is capped by `STREAMING_TTS_MAX_TEXT_CHARS`; outbound frames are
  capped by `STREAMING_TTS_CHUNK_BYTES` / `STREAMING_TTS_MAX_CHUNKS_PER_TURN` (the
  existing playback caps still apply).

The Azure-style `voice` passed by playback (e.g. `uz-UZ-MadinaNeural`) is ignored
because it is not a Deepgram model; the configured `DEEPGRAM_TTS_MODEL` is used. A
`voice` that looks like a Deepgram model (prefix `aura`) overrides the model.

## Samples (fake connection)
Client -> Deepgram text + flush messages:

    {"type": "Speak", "text": "Ish vaqtimiz 9:00 dan 18:00 gacha."}
    {"type": "Flush"}

Deepgram -> client: binary audio frames (RAW mu-law), then a control message:

    b"\xaa\xbb\xcc"   (audio frame)
    b"\xdd\xee"       (audio frame)
    {"type": "Flushed"}   (ends this flush's audio)

Outbound Twilio media payload decodes back to the exact raw bytes (no double-base64,
no header):

    {"event":"media","streamSid":"MZ","media":{"payload":"qrvM3e4="}}
    base64.b64decode("qrvM3e4=") == b"\xaa\xbb\xcc\xdd\xee"

Playback summary persisted per turn (safe counts only):

    "playback": {
      "provider": "deepgram", "enabled": true, "voice": "uz-UZ-MadinaNeural",
      "chunks_sent": 4, "bytes_sent": 7, "mark_name": "MZ:turn:0",
      "truncated": false, "degraded": false, "error": null,
      "status": "playing", "mark_received": false, "clear_sent": false,
      "interrupted": false, "interruption_reason": null
    }

## Latency metrics (A28 unchanged)
The same playback hooks fire: `tts_playback_started_at` is marked BEFORE
`synthesize()`, so `playback_started_at_ms..playback_completed_at_ms` (per turn) now
wrap REAL synthesis + receive time. `first_tts_chunk_sent_at` /
`tts_time_to_first_chunk_ms` / `tts_playback_duration_ms` are computed exactly as
before. See docs/streaming-latency-metrics.md.

## Barge-in (A27 unchanged)
Twilio `clear` remains the source of playback interruption: when caller speech (an
STT partial/final) arrives during active playback, the server sends a Twilio
`clear` and marks the playback interrupted - identical to the mock path. No
provider-side Deepgram clear is implemented in this milestone. See docs/barge-in.md.

## What is implemented vs not
Implemented: Deepgram TTS adapter behind `StreamingTTSProvider`, injectable
connection/connector, `Speak`/`Flush` protocol, raw mu-law/8k/container=none
output, RAW-bytes return (no header, no double-base64), fail-fast on missing key,
degrade-on-failure via the playback layer, bounded text/audio, key-in-header only,
full fake-connection test coverage + an end-to-end WS test where the Twilio media
decodes back to the exact fake audio.

NOT implemented: persistent/multiplexed TTS connections (one per turn), real
audio-quality tuning, provider-side barge-in/clear, OpenAI Realtime / Azure TTS,
hangup/handoff after an emergency or operator-transfer message.

## Next step: live voice pilot checklist / provider smoke test
Before a real call:
1. Set `STREAMING_STT_PROVIDER=deepgram` + `STREAMING_TTS_PROVIDER=deepgram`,
   `DEEPGRAM_API_KEY`, and install the extra: `pip install -e ".[stt-streaming]"`.
2. Enable the media stream + streaming flags: `TWILIO_USE_MEDIA_STREAMS=true`,
   `STREAMING_STT_ENABLED=true`, `STREAMING_STT_AI_TURNS_ENABLED=true`,
   `STREAMING_TTS_ENABLED=true` (and `BARGE_IN_ENABLED=true` if desired).
3. Point `TWILIO_STREAM_URL` at a public `wss://` (ngrok/your gateway) and run one
   real call: confirm you hear synthesized audio, that interim transcripts barge-in,
   and that the admin stream metadata shows `streaming_stt.provider=deepgram`,
   `turns[].playback.provider=deepgram`, and the `latency` block.
4. Verify no key appears in logs and that emergency/operator turns voice the SAFE
   reply text (103 message / operator message), not medical advice.
5. Remaining gaps: hangup/handoff after the emergency or operator message, audio
   quality/latency tuning, and a live voice eval on top of the text evals.
