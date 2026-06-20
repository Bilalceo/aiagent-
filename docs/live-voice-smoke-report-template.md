# Live voice smoke test report

Copy this file per run (e.g. `live-voice-smoke-report-2026-06-20.md`) and fill it
in. Do NOT paste any secret (Deepgram key, smoke token, Twilio token) or any real
patient data into this report.

## Run metadata
- Date / time (with timezone): ____________________
- Commit hash (git rev-parse HEAD): ____________________
- Environment (host / region / ngrok or gateway): ____________________
- Operator(s) running the test: ____________________

## Providers
- Telephony: twilio
- STT provider: deepgram (model: __________)
- TTS provider: deepgram (model: __________, container: none)
- Barge-in enabled: yes / no
- Metrics enabled: yes / no
- Readiness `ready`: true / false (attach `errors[]` / `warnings[]` summary)

## Smoke-mode config
- LIVE_CALL_SMOKE_MODE: true
- Require smoke token: yes / no
- Caller allowlist used: yes / no (count: ___; do NOT list raw numbers)
- Max duration (s): ___   Max turns: ___
- Redact transcripts: yes / no

## Call count
- Total calls placed: ___
- Calls completed cleanly: ___
- Calls rejected by gate (and why - reason codes only): ___

## Scenario results
For each, mark PASS / FAIL and a short note (no patient data).

1. Uzbek working hours:        PASS / FAIL  - ____________________
2. Russian working hours:      PASS / FAIL  - ____________________
3. Service price question:     PASS / FAIL  - ____________________
4. Doctor schedule question:   PASS / FAIL  - ____________________
5. Appointment intent:         PASS / FAIL  - ____________________
6. Operator transfer request:  PASS / FAIL  - ____________________
7. Complaint / angry caller:   PASS / FAIL  - ____________________
8. Emergency / 103:            PASS / FAIL  - ____________________
9. Barge-in while AI speaks:   PASS / FAIL  - ____________________
10. Unclear / noisy / empty:   PASS / FAIL  - ____________________

## Latency summary (ms; min / median / max across calls)
- time_to_first_partial_ms:   ___ / ___ / ___
- time_to_first_final_ms:     ___ / ___ / ___
- ai_turn_duration_ms:        ___ / ___ / ___
- tts_time_to_first_chunk_ms: ___ / ___ / ___
- tts_playback_duration_ms:   ___ / ___ / ___
- mark_round_trip_ms:         ___ / ___ / ___
- barge_in_clear_latency_ms:  ___ / ___ / ___
- total_stream_duration_ms:   ___ / ___ / ___

## Audio quality notes (avg 1-5)
- Intelligibility: ___   Naturalness: ___   Volume/clipping: ___
- Latency felt: ___   Language/voice correct: ___
- Free-form notes: ____________________

## Safety notes
- Emergency scenario spoke the official 103 message: yes / no
- Medical-advice / unknown-price routed to operator or safe refusal: yes / no
- Any unsafe content observed (diagnosis/medicine/dosage): yes / no - detail: ____
- Any secret/token/phone/audio found in logs or metadata: yes / no - detail: ____

## Operator transfer notes
- Transfers triggered (count): ___
- Hand-off message correct + safe: yes / no
- Any missed transfer (should have transferred but did not): ____________________

## Bugs found
1. ____________________
2. ____________________

## Decision
- [ ] PASS - proceed to next pilot stage
- [ ] RETRY - fix listed issues and re-run smoke test
- [ ] BLOCK - do not pilot; blocking issues: ____________________

Signed off by: ____________________   Date: ____________________
