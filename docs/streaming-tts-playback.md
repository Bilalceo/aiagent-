# Streaming TTS playback (mock-first)

This is the FIRST outbound-playback milestone on top of the Twilio Media Streams
WebSocket. When a streaming FINAL transcript produces an AI text turn (see
docs/streaming-stt.md), the reply is synthesized (MOCK by default) and streamed
back to Twilio over the SAME socket as `media` + `mark` events.

It is ARCHITECTURE + a deterministic MOCK only:
- no real speech synthesis (the mock emits `b"MOCK-TTS:" + text`),
- no real/paid streaming TTS provider,
- no barge-in (the `clear` event helper exists but is NOT used),
- no real hangup control (emergency/transfer playback is documented below).

The non-streaming Twilio Gather/SpeechResult flow, `/voice/simulate`, streaming
STT, and the AI-turn metadata are all unchanged. Streaming TTS is OFF by default.

## Components
- `StreamingTTSProvider` (interface): `synthesize(text, *, language, voice) -> bytes`.
  `MockStreamingTTSProvider` returns deterministic fake audio, no external calls.
- Twilio outbound builders (safe JSON, never carry raw bytes in logs/metadata):
  - `build_media_message(stream_sid, payload_b64)` -> `{"event":"media", ...}`
  - `build_mark_message(stream_sid, name)` -> `{"event":"mark", ...}`
  - `build_clear_message(stream_sid)` -> `{"event":"clear", ...}` (for a FUTURE
    barge-in; not used yet)
  - `chunk_bytes(data, size)` -> split audio into `<= size` frames
- `TwilioPlaybackService.play(send, *, stream_sid, ai_text, language, turn_order)`:
  resolves the voice, caps text, synthesizes, chunks, base64-encodes each chunk
  ONCE, sends N `media` frames then one `mark`, and returns a SAFE playback
  summary. Never raises - a synth/send failure becomes a degraded summary so the
  WebSocket cannot crash.

## When it runs
On the media stream, only when ALL of these hold:
- `TWILIO_USE_MEDIA_STREAMS=true`
- `STREAMING_STT_ENABLED=true`
- `STREAMING_STT_AI_TURNS_ENABLED=true` and a CallSession is linked to the stream
- `STREAMING_TTS_ENABLED=true`

A FINAL transcript -> AI turn -> `play(...)` sends media + mark. Partials never
produce playback (they never produce a turn). Emergency / operator-transfer turns
carry the official SAFE reply text (103 message / operator message) as `ai_text`,
so playing `ai_text` voices the safe message - never unsafe medical advice. Real
hangup after an emergency message is NOT implemented here (the call stays on the
media stream until Twilio/stop); a `clear`/close strategy can follow later.

If `STREAMING_TTS_ENABLED=false` (default) behavior is exactly the A25 one: the
AI turn is persisted, no outbound media is sent.

## Outbound events (samples)
media frame (one per audio chunk; payload is base64 of the chunk):

    {"event": "media", "streamSid": "MZ-on", "media": {"payload": "TU9DSy1UVFM6..."}}

mark after the last chunk of a turn (Twilio echoes it back at playback time):

    {"event": "mark", "streamSid": "MZ-on", "mark": {"name": "MZ-on:turn:0"}}

## Metadata (per turn, under stream_metadata.streaming_stt.turns[i].playback)
Safe counts + the mark name ONLY - never raw audio, base64, or secrets:

    "playback": {
      "provider": "mock", "enabled": true, "voice": "uz-UZ-MadinaNeural",
      "chunks_sent": 10, "bytes_sent": 158, "mark_name": "MZ-on:turn:0",
      "truncated": false, "degraded": false, "error": null
    }

`degraded=true` with `error` in {empty_text, tts_error, send_error} marks a failed
playback; `truncated=true` means the reply text or chunk count hit its cap.

## Env flags
- `STREAMING_TTS_ENABLED` (default false) - enable outbound playback.
- `STREAMING_TTS_PROVIDER=mock` - only `mock` implemented.
- `STREAMING_TTS_CHUNK_BYTES` (default 400) - audio bytes per media frame (pre-base64).
- `STREAMING_TTS_MAX_TEXT_CHARS` (default 2000) - cap reply chars synthesized per turn.
- `STREAMING_TTS_MAX_CHUNKS_PER_TURN` (default 200) - cap media frames per turn.
- `STREAMING_TTS_VOICE_UZ` / `STREAMING_TTS_VOICE_RU` - resolved by language.

## What is implemented vs not
Implemented: playback provider interface, mock provider, Twilio media/mark/clear
builders, chunking + once-per-chunk base64, TwilioPlaybackService with caps and
safe degraded handling, WebSocket wiring (final turn -> media + mark), per-turn
playback summary in stream metadata, full test coverage.

NOT implemented: real streaming TTS provider, real mu-law/8k audio encoding (the
mock bytes are not playable audio), barge-in (`clear` on inbound speech), hangup
after emergency, playback-latency metrics, `mark`-acknowledgement handling.

## Next steps toward a real-time voice pilot
1. Real streaming TTS provider behind `StreamingTTSProvider`, emitting mu-law/8k
   frames Twilio can actually play; reuse the same chunk/media/mark path.
2. Barge-in: on inbound speech resuming mid-playback, send `clear` and stop the
   outbound stream (the `clear` builder already exists).
3. Handle Twilio's echoed `mark` events to track playback progress / completion.
4. Hangup/handoff strategy after an emergency or operator-transfer message.
5. Latency + audio-quality metrics and a live voice eval on top of the text evals.
