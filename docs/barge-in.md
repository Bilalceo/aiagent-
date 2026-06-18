# Barge-in + Twilio clear/mark handling (mock-first)

This adds barge-in on top of the streaming TTS playback (docs/streaming-tts-playback.md):
when the AI is playing audio and the caller starts speaking again, the server
sends a Twilio `clear` event to flush the queued playback and marks that playback
as interrupted. It also handles Twilio `mark` echoes to mark a playback completed.

It is mock-first ARCHITECTURE, not production barge-in:
- there is NO real Voice-Activity-Detection (VAD) and no real provider-based
  end-of-utterance endpointing - the streaming STT TRANSCRIPT (a partial/final)
  IS the "caller is speaking" signal,
- the STT and TTS are still the deterministic mocks,
- there are no playback-timing/latency metrics yet.

Barge-in is OFF by default. With it off, behavior is exactly the A26 one.

## The speech signal (why transcripts, not raw media)
Raw inbound `media` frames are continuous and do NOT mean the caller is speaking
(silence still produces frames). So barge-in is NOT triggered by media frames.
It is triggered when the streaming STT emits a partial or final TRANSCRIPT while
playback is active - that is the closest "speech detected" signal we have without
a real VAD. A real VAD / provider endpointing replaces this later, behind the
same trigger point.

## Trigger rules
On each media frame, after streaming STT returns transcript events and BEFORE the
new AI turn is processed:
- if `BARGE_IN_ENABLED=true` AND a playback is currently active AND an event
  qualifies as caller speech (a partial when `BARGE_IN_ON_PARTIAL=true`, a final
  when `BARGE_IN_ON_FINAL=true`, with `len(text) >= BARGE_IN_MIN_TRANSCRIPT_CHARS`):
  - send ONE Twilio `clear` event,
  - mark the active playback `interrupted`, `clear_sent=true`,
    `interruption_reason="caller_speech"`, `status="interrupted"`,
  - drop the active-playback reference so NO duplicate `clear` is sent for it.
- a final transcript may both interrupt the prior playback AND then go through the
  AI-turn pipeline as usual (which may start a new playback).
- if the `clear` send fails (socket broken), the playback is marked `degraded`
  with `interruption_error="clear_send_error"`; the WebSocket never crashes.

## Mark handling
Twilio echoes a `mark` event back when playback actually reaches that mark. The
server:
- on a `mark` whose name matches the active playback's `mark_name`: sets
  `mark_received=true`, `status="completed"` (unless already interrupted), and
  clears the active-playback reference,
- ignores unknown mark names and duplicate/late marks (idempotent, never crash).

## Events
Outbound `clear` (flush queued playback; carries only the streamSid):

    {"event": "clear", "streamSid": "MZ-bp"}

Inbound `mark` echo handled by the server (Twilio -> us):

    {"event": "mark", "streamSid": "MZ-mk", "mark": {"name": "MZ-mk:turn:0"}}

## Metadata (per turn, stream_metadata.streaming_stt.turns[i].playback)
The barge-in/mark state lives on the SAME playback summary that A26 persists -
safe counts + lifecycle flags only, never raw audio/base64:

    "playback": {
      "provider": "mock", "enabled": true, "voice": "uz-UZ-MadinaNeural",
      "chunks_sent": 10, "bytes_sent": 158, "mark_name": "MZ-bp:turn:0",
      "truncated": false, "degraded": false, "error": null,
      "status": "interrupted",          // playing | completed | interrupted | degraded
      "mark_received": false,
      "clear_sent": true,
      "interrupted": true,
      "interruption_reason": "caller_speech"
    }

## Config (.env)
- `BARGE_IN_ENABLED` (default false) - enable barge-in (send `clear` on speech).
- `BARGE_IN_ON_PARTIAL` (default true) - a partial transcript triggers barge-in.
- `BARGE_IN_ON_FINAL` (default true) - a final transcript triggers barge-in.
- `BARGE_IN_MIN_TRANSCRIPT_CHARS` (default 1) - ignore shorter (noise) transcripts.

Barge-in only does anything when streaming STT + AI turns + streaming TTS are also
enabled (there must be active playback to interrupt).

## What is implemented vs not
Implemented: `BargeInController` (active-playback tracking, clear-on-speech with
single-clear guard, mark-completes-playback, idempotent/unknown marks, safe
degraded on clear-send failure), the `clear` builder, WebSocket wiring (barge-in
before the new turn; `mark` event handling), playback lifecycle in metadata, full
test coverage.

NOT implemented: real VAD / provider end-of-utterance endpointing, real streaming
STT/TTS, partial-playback position tracking (we mark whole-turn interrupted, not
the exact offset), playback-latency metrics, resuming/replaying interrupted text.

## Next steps toward a real-time voice pilot
1. Real streaming STT/TTS providers (the trigger/clear/mark wiring already exists).
2. Real VAD / endpointing as the barge-in signal instead of transcript events.
3. Playback-timing + latency metrics (time-to-first-frame, mark round-trip,
   interruption latency) and a live voice eval on top of the text evals.
4. Track the exact interrupted offset (from the echoed `mark`) for precise resume.
