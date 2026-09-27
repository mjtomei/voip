# Voice acceptance results — September 19, 2026

Follow-up: [real handset testing](MANUAL-ACCEPTANCE-2026-09-19.md) has since
verified several of these paths with the primary user. The automated results below are
preserved; remaining phone-test coverage is listed in the follow-up report.

The TTS/STT suite is implemented, but voice acceptance is **not all passing**.
The latest completed attempts show **8 passed and 7 failed** across 15 scenarios.
These results combine selected runs made while developing the harness; they are
not one complete run of an identical revision or a measured reliability rate.
Original failures and recordings are preserved. The
machine-readable index (private historical artifact `state/e2e/latest-acceptance.json`) links each case to
its report, diagnostics, audio and available source hashes.

Caller requests went through eSpeak, dedicated virtual audio devices and the
production voice frontend into real native Codex sessions. Independent local
Whisper `small.en` transcribed captured speaker output. Assertions also checked
real files, mailbox records, request states, thread identities or backend turns.
The Twilio API and SIP observer events were simulated: **no real calls were
placed**. See [coverage and commands](VOICE-ACCEPTANCE.md) for the boundary and
joint telephony checklist.

## Latest results

| Scenario | Result | Evidence / limit |
| --- | --- | --- |
| Original voice/tool smoke test | Pass | Fresh six-digit marker written through a real shell tool; spoken confirmation independently transcribed. Run (private historical artifact `state/e2e/20260919-162728-64f916/report.json`). |
| Named profiles | Pass | Two simultaneous real audio sessions answered with their own configured startup phrases and distinct threads. Run (private historical artifact `state/e2e/20260919-162055-097fd9/report.json`). |
| Mailbox and configured incoming greeting | Fail | Latest attempt ignored the configured greeting and stopped before mailbox actions. An earlier attempt demonstrated the action, outcome note and pending decision; see qualifications below. Run (private historical artifact `state/e2e/20260919-164821-b4ef74/report.json`). |
| Prepared outgoing call | Pass | Preparation before simulated dial; configured server caller number; prepared topic spoken after connection, without another backend turn during the opening. Run (private historical artifact `state/e2e/20260919-165716-fea0c3/report.json`). |
| Hangup review | Fail | Delivered notification acknowledged, then hangup notice submitted; no required CLI review, outcome note or disconnection audit marker. Run (private historical artifact `state/e2e/20260919-162056-01acc2/report.json`). |
| Ordinary and priority requests | Pass | Ordinary context waited during microphone activity; priority context reached the operator. Matching recipient reused the call; a different recipient waited. Run (private historical artifact `state/e2e/20260919-162055-097fd9/report.json`). |
| Requests during backend work | Pass | A real waiting tool continued while priority context steered its turn; ordinary context waited; the spoken answer reported both agendas and completion. Run (private historical artifact `state/e2e/20260919-162055-097fd9/report.json`). |
| Scheduled calls and reuse | Fail | Initial occurrence, idempotency and prepared opening worked. A second occurrence reused the call but its actual reference was not spoken. Run (private historical artifact `state/e2e/20260919-163538-5bba98/report.json`). |
| Offline scheduled-call count | Pass | Four missed occurrences skipped with no catch-up calls; count reached the matching connection once and was absent on the next one. Run (private historical artifact `state/e2e/20260919-164056-081668/report.json`). |
| Redial, queued cancellation and expiry | Pass | Configured retry delay/cap, prepared retry opening after connection, completion and no dialing for cancelled/expired queued requests. Run (private historical artifact `state/e2e/20260919-165716-fea0c3/report.json`). |
| Concurrent sessions, same destination | Fail | Two separate attempts and threads existed; the first captured opening omitted its required topic. Run (private historical artifact `state/e2e/20260919-162055-097fd9/report.json`). |
| Concurrent sessions, different destinations | Pass | Separate destinations and threads; each output contained its own topic after its connection. Run (private historical artifact `state/e2e/20260919-165716-fea0c3/report.json`). |
| Frontend reset | Fail | Reset preserved the UUID and restored voice, but the phrase spoken and echoed before reset was not recalled. Run (private historical artifact `state/e2e/20260919-163319-4d278c/report.json`). |
| Same-conversation session reset | Fail | Reload preserved the UUID and restored voice; spoken-only phrase recall failed. Run (private historical artifact `state/e2e/20260919-163319-4d278c/report.json`). |
| Fresh session on outgoing call | Fail | Fresh UUID, prepared opening and lack of old phrase worked; repeated attempt/connected reuse did not reset again. The later reused-call agenda was acknowledged without its topic being spoken. Run (private historical artifact `state/e2e/20260919-162936-498730/report.json`). |

## What the failures establish

**Submitting a notice does not establish that the operator acted on it.** In the
hangup test, diagnostics show both connection and ended notices submitted to the
same native thread, but only the preparation backend turn. The acknowledged
notification remained unaddressed with no resolution, and the configured
disconnection audit file was absent. This is a failed behavior check; the evidence
does not identify whether the cause is prompt handling, delegation or another
native realtime/backend limitation.

**Acknowledgment is insufficient for agenda delivery.** The scheduled reuse
output said it had a new scheduled reference and asked for a moment, but did not
give the reference. Fresh-session call reuse similarly produced an acknowledgment
without the topic. Their state assertions passed while speech assertions failed.
The tests retain those failures rather than treating request acceptance as
successful delivery. Fresh outgoing reset itself did work in its test.

**Preserving a thread UUID did not guarantee recall of a spoken exchange.** Both
recovery tests first received an audible echo of “Golden umbrella.” After reset,
the operator gave its startup identification phrase or said it had no conversation
password. This does not prove that all saved history disappeared, or that backend
tool history was lost; it establishes that this spoken-only recall check failed.

**Opening behavior was inconsistent.** The latest incoming test spoke connection
status instead of its configured greeting. The same-destination concurrency test
captured the tail of an opening without its subject. Signal was present from the
start of that recording, before the simulated answer transitions; this suggests
speech had already begun, but does not establish why or whether a handset would
have missed it. The current recorder explicitly excludes preconnection audio.
Prepared, redial and different-destination concurrency were rerun successfully
with that stricter check. The failing same-destination case has not been rerun
with the final recorder revision and remains unverified.

## Harness corrections and limits

Development uncovered test issues as well as system failures. Initial `base.en`
recognition misheard distinctive phrases; the current `small.en` model improved
recognition of the same recorded audio. Expectations allow explicit spelling and
word-spacing variants without passing expected words to STT. Short, continuous
caller utterances avoid a pause causing delegation before the full request has
been spoken. The fixture now uses the normal daemon socket setting so real
session resets can run; earlier custom-socket setup errors are not counted as
product reset failures.

In an earlier mailbox attempt, the action and addressing checks passed, and STT
reported “Deployments still pending” while the decision actually remained
unaddressed. Its speech assertion was too narrow and has been corrected. That
partial evidence is preserved in the
earlier run (private historical artifact `state/e2e/20260919-164056-081668/report.json`); it does not turn the
latest whole mailbox scenario into a pass.

Recordings, transcripts and failure logs should be inspected before attributing
any failure. These are model acceptance tests with bounded waits, not proofs that
the model can never perform the behavior. They have no automatic retry-until-pass
policy. Original failed evidence is retained across explicit reruns.

The phase marker and first/last signal offsets refer to PCM received at the local
speaker monitor. They do not establish handset playback time. Tool execution,
backend turns, result handoff/request acceptance and saved-speech timestamps are
separate observations. **No audible latency is inferred from transcript-save
timing.** This report does not diagnose network latency or supply a latency
breakdown.

## Deterministic checks and cleanup

- Python: **161 tests run, 160 passed, one existing optional skip**, zero failures.
- Twilio JavaScript: **38 passed**, including access/PIN and outgoing handlers.
- No remaining acceptance frontend processes or virtual audio modules.
- The original production thread remained selected, voice was connected, and
  production services were active. No real calls were connected at cleanup.

Only a provider-factory injection point was added to the production runtime; its
default still uses the real `Calls` implementation. This work adds tests and
evidence, not fixes for the voice behavior failures above.

## Ready for joint phone testing

The existing mailbox and keypad phone checks now include output STT. The new
`scripts/check-phone-audio.py` can capture an arranged outgoing opening or inject
a TTS request during an arranged call and verify the local spoken output. It
requires `--run-telephony`; handset playback still needs the primary user's confirmation.

The remaining checks are real PIN/number routing, prepared opening at the handset,
carrier busy/no-answer retry, scheduled/priority topics during a call, concurrent
SIP legs, keypad recovery and hangup/cancellation. An additional authorized number
and deployed second profile are needed for multi-number testing. Fresh keypad
reset must be arranged because it replaces the real operator conversation.
No telephony test was started automatically.
