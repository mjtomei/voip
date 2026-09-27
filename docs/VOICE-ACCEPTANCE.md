# Audio acceptance tests

See the [September 19 results](VOICE-ACCEPTANCE-RESULTS.md): the suite is
implemented, with eight latest scenarios passing and seven failing. Those runs
simulated the phone boundary; test coverage is not a claim that every behavior works.
The subsequent [handset test results](MANUAL-ACCEPTANCE-2026-09-19.md) cover two
recipients, separate PINs, both concurrency modes, scheduled delivery, targeted
cancellation, a completed retry and keypad recovery, plus reproduced hangup and
post-fresh-reset response failures. The temporary second profile was removed afterward.

`python3 scripts/test-features.py --run` runs real Codex voice sessions with
synthesized caller input and independent speech recognition of the recorded
operator output. It consumes the existing Codex account's model usage. It never
places telephone calls. Run `python3 scripts/test-features.py` to list cases;
`--case NAME` is repeatable and `--timeout 180` allows slower backend work.

Install test dependencies locally, without root or new accounts:

```bash
python3 scripts/install-tts.py
python3 scripts/install-stt.py
```

STT uses [whisper.cpp v1.8.3](https://github.com/ggml-org/whisper.cpp/tree/v1.8.3)
and its checksum-verified `small.en` model, following the upstream
[build](https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/README.md) and
[model](https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/models/README.md)
instructions. Both dependencies live under `.tools/`. Speech recognition runs
locally; recordings are not sent to an STT API. Recognition uses four CPU threads
and is serialized across test processes to leave headroom for live audio. Expected phrases are never supplied
to the recognizer as hints. Phrase matching ignores capitalization and punctuation,
and uses word boundaries, with explicit alternatives for recognized spelling/word-spacing variants. Each case also checks the state change it claims.

## What runs end to end

The input path is eSpeak → dedicated PipeWire/Pulse sink → actual virtual mic →
production WebRTC frontend → native Codex realtime/backend/tools. The output path
is native speech → production frontend → dedicated output sink → recorded PCM/WAV
→ independent Whisper transcription → assertions. Service notices and worker
agendas enter through the normal text control paths; they are not substitutes for
spoken caller requests. No native transcript is used as proof of spoken output. Outgoing-call captures
mark the established-call transition and only recognize audio received after
that marker; preparation/preload speech cannot satisfy the connected opening.

Each feature gets its own temporary project, persistent native conversation(s),
mailboxes, requests, fictional recipient config, private tmux socket and audio
devices. The fixture stops its own frontends, archives its own native threads and
unloads only its own audio modules. It leaves the real operator and workers alone.
Saved recordings, transcripts, configuration and failed evidence remain in
`state/e2e/RUN/`. Keep these private: they contain test conversations. Cleanup
errors fail the run and are recorded for inspection.

Only the telephone boundary is simulated: an in-memory Twilio API and Baresip
observer events. The real `Calls`, preparation, dispatcher, scheduler, prompt
rendering, mailbox and reset implementations run unchanged apart from injecting
the provider factory into `Runtime`. The simulator cannot make network requests.
An established simulated call represents *already authenticated* connectivity;
this does **not** validate a PIN, carrier behavior, SIP/RTP or handset audio.

| Case | Behavior and evidence |
| --- | --- |
| `smoke` | Original TTS request, fresh six-digit shell marker, independently verified spoken confirmation. |
| `profiles` | Simultaneous input/output on two real voice sessions; distinct threads, profile startup phrases and no cross-session phrase leakage. |
| `mailbox` | Custom incoming greeting; audio-triggered mailbox read, tool action and outcome note; unanswered decision remains pending after hangup and an audio status query. |
| `prepared` | Real backend preparation precedes simulated dial; server caller number is used; no premature addressing; output contains the prepared topic without another backend turn during the opening; persistent thread remains unchanged and the ended request completes. |
| `hangup` | Prepared topic is delivered and acknowledged through audio; hangup must trigger actual mailbox review and completion with an outcome note. A custom disconnection prompt also requires an audit marker for that call. |
| `requests` | Actual microphone activity defers normal context; priority context enters the connected realtime session; request prompt configuration; same-recipient reuse does not dial; another recipient waits. |
| `backend_requests` | Audio starts a real waiting tool; ordinary request waits while priority request steers the busy native turn; original tool completes; speech reports both agendas and completion. |
| `schedules` | Cron creates one ordinary request; duplicate tick is idempotent; prepared scheduled opening; another scheduled occurrence preempts by reusing the existing call. |
| `missed` | Controlled scheduler downtime skips four occurrences, queues no catch-up, exposes the count to the operator once; audio diagnostic queries verify count and no repetition on a later connection. |
| `redial` | Provider busy outcome, configured wait and second attempt; prepared retry opening; ended connected request completes; cancelled/expired queued requests never dial and their status is queried through audio. |
| `concurrency`, `different_recipients` | Two sessions dial independently, first testing the same configured number and separately testing different configured numbers; two simultaneous actual audio outputs carry their own agendas; each caller acknowledges its own topic. Mailbox completion is asserted separately in `hangup`. |
| `frontend_reset` | Reconnect audio to the same UUID and recall a phrase spoken before recovery. |
| `session_reset` | Reload the same UUID/history and recall a phrase spoken before recovery. |
| `outgoing_reset` | Configured outgoing reset creates a fresh UUID without old conversational memory; preparation/opening work afterward; same-attempt retry and connected-call reuse do not reset again. |

These are model acceptance tests, so real recognition, delegation and speech
failures are failures, not retries silently counted as passes. A report includes
`passed`, `failed`, and `not_run` separately. Inspect each `result.json`,
`response.wav`, `response.txt` and `failure.log` before attributing a failure.
A later rerun has its own report and does not overwrite the original evidence.
The dedicated `hangup` test owns positive post-call mailbox completion assertions;
that failure must not suppress otherwise independent scheduling or reset coverage.

Tests for numeric cron/DST rules, configuration validation, per-recipient PIN
routing, ambiguity/crash handling and detailed retry/cancellation states remain
in the deterministic Python/JavaScript suites. A TTS phrase alone would not prove
those internal invariants.

## Transfer delegation regression

Run `python3 scripts/test-transfer-delegation.py --run` to reproduce a request to
return home immediately after a transfer-arrival opening. A fresh isolated native
session receives a simulated arrival, then the complete request through TTS.
The test audits the backend's actual `call transfer --to-home` CLI invocation and
correct local call identity. Home routing is deliberately disabled, so independent
STT must verify an honest failure response, with no claim that the transfer is done.
This proves delegation and refusal behavior only; no provider redirect, SIP transfer,
PIN validation, or handset audio is exercised. Evidence and cleanup follow the
same fixture rules as the other native cases; `--output` retains a separate run.

## Existing tests now verify speech

- `scripts/test-voice.py run`: shell marker **and** transcribed confirmation;
  supports `--root`, `--session`, a test-owned `--marker-file`, and a bounded
  `--response-seconds` timeout. The isolated suite uses a simple root-level
  filename to avoid turning the tool-access check into a spoken-path spelling test.
- `scripts/check-mailbox-calls.py --run-telephony`: original two real SIP calls
  plus a recorded/STT-verified mailbox action response.
- `scripts/check-keypad-call.py --run-telephony --scope session`: original real
  SIP DTMF test plus TTS/STT after recovery. `frontend` and `fresh` also supported.
  `fresh` replaces the operator conversation and must be arranged accordingly.
- Transport/signal-only tests (`check-audio`, `test_voice_transport`,
  `check-twilio-sip`) intentionally have no language assertion: they carry tones,
  echo or signaling, not a model's spoken answer.

## Joint telephony checks

No real telephony tests run as part of the automated feature suite. Arrange these
with the primary user; an additional authorized number and deployed second session are needed
for recipient routing and concurrent phone tests. Do not enable fictional fixture
recipients or schedules in production.

| Check | the primary user's part | Evidence to collect |
| --- | --- | --- |
| Prepared outgoing opening | Answer and enter the configured PIN; confirm the first substantive opening is heard. | Preparation before dial, established call identity, recorded/STT topic, handset confirmation; no inference from saved speech timestamps. |
| Number/PIN routing | Call from each configured number; test its own and the other recipient's PIN. | Correct profile and prompt after authorization; wrong PIN never reaches the operator. Never put PINs in logs. |
| Scheduled and priority requests during a call | Keep a call open while we submit ordinary and priority agendas and a due schedule. | Correct waiting/reuse behavior, spoken topics, same established call and native thread. |
| Busy/no-answer redial | Decline or leave one agreed call unanswered, then answer its retry. | Real carrier disposition, configured delay/attempt cap and prepared opening. |
| Concurrent calls | Use two numbers, then two sessions calling the same number if the handset/carrier supports it. | Independent SIP legs/audio/conversations; any carrier call-waiting limitations recorded. |
| Recovery keypad | Enter `*1#`, `*2#`, then an explicitly arranged `*3#`. | Correct reset scope, restored speech, UUID/history behavior, call remains up. |
| Hangup and cancellation | Acknowledge one notification, leave a second unanswered, then hang up. | Correct call-ended notice and outcome note; unanswered message stays open; cancellation settles both provider legs. |

To verify local audio on an arranged incoming/already-connected call:

```bash
python3 scripts/check-phone-audio.py --run-telephony --session default \
  --tts 'Please say telephone audio verified.' --expect 'telephone audio verified'
```

To record a prepared outgoing opening, create a notification-only agenda file
containing a distinctive reference phrase, then use a stable key:

```bash
python3 scripts/check-phone-audio.py --run-telephony --session default \
  --recipient alice --request-key joint-test-YYYYMMDD-01 --agenda /path/to/agenda.txt \
  --expect 'distinctive reference phrase'
```

This starts recording before enqueueing and matches the resulting call identity.
A successful test leaves the call connected for the conversation. A failed test
requests cancellation of its own queued call; inspect `request status KEY` for
settlement before retrying. Reuse the same key; do not bypass uncertain attempts.

## Timing and limits

`first_output_signal_seconds` and `last_output_signal_seconds` are PCM positions
at the local speaker monitor. STT omits leading/trailing quiet with one second
of padding and records the analyzed offset/duration; the full WAV is retained.
These measurements do not establish when a telephone played the
sound. Backend execution, preparation completion, request acceptance and call
signaling have their own state timestamps. Native saved-speech timestamps are
neither a playback clock nor a measure of audible latency. Phase markers describe locally observed call transitions, and `verification_after_seconds`
excludes earlier audio. A passing spoken phrase does not establish that a task completed: the corresponding marker,
mailbox, queue or conversation assertion must pass independently.
