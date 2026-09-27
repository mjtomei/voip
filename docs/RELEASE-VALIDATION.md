# Container release validation

Validation date: September 26, 2026. This pass tests the consolidated source and
an actual container deployment connected to a host Codex daemon. Arranged tests
with the available handset are complete; a second-recipient test was not rerun.
Reboot validation and release review remain outstanding, so this is not a
completed release sign-off.
No license is included.

## Clean container

The current clean run passed every required step on an internal Docker network,
with no host daemon, account credentials, host audio or real telephone calls:

| Check | Result |
| --- | --- |
| Python contracts/integration | 308 tests, one optional integration skipped, no failures |
| Hosted-handler Node tests | 79 passed against the actual handlers and simulated provider |
| Separate input/output audio | Passed on container-local PipeWire |
| Synthetic PIN TTS/STT | Passed, including complete-code confirmation and opt-out |
| SIP TLS keepalive | Passed with patched Baresip against a local TLS peer |

The image uses Ubuntu 24.04, Python 3.12, Node 22, checksum-verified Baresip/re
4.11.0 and whisper.cpp v1.8.3 with `small.en`. The optional tmux-resume exclusion
test needs a compatible external tool; its explicit skip is not a pass. That
tool is not required by the container's private tmux supervisor.

## Native container-to-host voice

These tests use real Codex voice, real tool execution, synthetic speech into the
virtual microphone and independent STT of captured output. Only the provider and
SIP call observer are simulated. Model usage is real; no phone calls are placed.
The host Docker CLI requires explicitly selected `danger-full-access`; the
public default remains read-only.

The full sixteen-case run completed with six passes and ten failures. A failure
below is not overwritten by a later repeat. All cases completed owned-resource
cleanup without errors.

| Case | Observed result |
| --- | --- |
| Smoke | Passed: TTS request, actual marker-file tool action and independent spoken verification |
| Profiles | Failed because STT rendered the required phrase differently; an isolated repeat with unchanged assertions passed |
| Mailbox | Full run failed its exact custom greeting; a repeat verified the actual marker action, addressed outcome and pending decision, but failed the required spoken confirmation |
| Prepared outgoing | Passed, including preparation before simulated dial and captured opening |
| Hangup review | Full run stopped at the opening speech check; focused repeats reproduced a missing-acknowledgment review failure; after adding call-scoped transcript guidance, a fresh native repeat passed |
| Requests | Passed: nonpreemptive waiting, priority delivery and same-call reuse |
| Backend requests | Actual waiting tool completed and priority context arrived; the spoken result omitted part of the required phrase, so the case failed |
| Schedules | Failed the scheduled opening speech check; simulated scheduling state had progressed |
| Offline skipped calls | Count and notice state were correct; STT rendered the spoken number as a homophone, so the strict speech check failed |
| Redial | Passed: simulated failed attempt, retry timing, prepared opening and completion |
| Concurrent sessions, same recipient | Passed with separate native threads/audio and independent speech checks |
| Different recipients | Both separate openings passed; a later acknowledgment was paraphrased and failed its exact speech check |
| Frontend reset | Passed: same thread and recall of the spoken test phrase |
| Same-conversation reload | Thread identity was preserved, but a speech-only phrase could not be recalled after reload |
| Fresh reset before outgoing | New conversation was created; the older harness incorrectly required connected voice before warmup, preventing dispatch. With standby-aware readiness, dispatch succeeded; the repeated case then failed its opening speech assertion |
| Directory workspace | Speech recognition interpreted a shell abbreviation as another command; exact file-content assertion correctly failed. A repeat using a natural-language request passed the unchanged file-content and independent speech assertions |

Separate lifecycle testing passed two on-demand cycles: standby, startup,
configured greeting, TTS/STT exchange, ordinary text hangup review and return to
standby with the same UUID. A separate transfer-delegation test passed: the spoken
home request reached the audited CLI and an unavailable route was reported
honestly. Neither is a carrier test.

The acknowledgment problem is more than an STT mismatch: the saved realtime
transcript contained the acknowledgment, but the subsequent backend review said
it was unconfirmed and left the message open. Native voice records and backend
working context are distinct. Similarly, preserving the conversation UUID does
not establish recall of every speech-only exchange after a full reload. An attempted prompt change requested delegation of fulfilled acknowledgments
during the call; its focused repeat still failed and the change was replaced.
The ended notice now identifies only the native threads associated with that
call and directs the backend to inspect the saved realtime caller words before
declaring confirmation missing. The call-scoping regression passes. A fresh native TTS/STT repeat then passed:
the backend read the saved caller transcript, addressed the notification with an
outcome note, and completed the actual hangup review. The updated runtime is deployed. A follow-up review of the original handset
call recovered the missed acknowledgment from its saved transcript, addressed
that notification and preserved both the previous outcome and the undecided
question. The original failed test remains recorded. A fresh automatic handset
hangup test then passed: the caller acknowledged a new notification through
speech, ended the call using the handset, and the backend inspected that call's
saved transcript and addressed the message with an outcome note, without manual
repair. Independent STT verified the notification's local output; the caller
confirmed hearing it. The same-conversation speech-only recall limitation remains.

A first native invocation used `workspace-write` and encountered the host Docker
socket restriction. The corrected invocation explicitly selected the needed
permissions. A later repeat used an evidence path too long for a Unix tmux
socket and failed before audio; its artifacts are retained, and the rerun uses a
shorter path. Neither invocation failure is counted as a voice feature pass.

## Actual deployment and arranged handset checks

The host phone services were stopped before the container took ownership. The
container uses its own audio and tmux processes; the host Codex daemon and
ordinary worker sessions remain shared host capabilities. Credentials, recipients,
verifiers, histories and recordings stay in private mounted state. The existing
project's daily schedule was retained. No second dispatcher was enabled.

| Boundary | Observed result |
| --- | --- |
| Prepared outgoing call | User confirmed prompt, clear opening after keypad PIN; independent local-output STT passed |
| Frontend keypad recovery | Same phone call and spoken response confirmed; an initial reset-record race was fixed and the repeat completed with unchanged call/thread |
| Same-conversation keypad reload | Same phone call and thread, saved history backup, user confirmation and independent STT passed |
| Fresh keypad reset | New UUID, saved old history, same phone call, user confirmation and independent STT passed |
| Transfer to another directory | Same phone call, no additional PIN, arrival announcement; user and STT passed |
| Return transfer to home | First attempt used an old host CLI from a saved conversation and failed before redirecting. Fresh conversations corrected it; user confirmed same-call arrival at home |
| Priority mailbox topic | User confirmed delivery and acknowledgment on the same call without another ring |
| Pending decision | User left it undecided; independent STT verified the spoken choices |
| Scheduled same-call topic | Real cron occurrence submitted into the connected call; user and STT passed; temporary schedule removed immediately |
| Hangup outcome review | Initial partial failure retained; corrected follow-up recovered the missed acknowledgment and preserved the pending decision. A fresh automatic handset test passed without manual repair |
| Wrong PIN | User confirmed rejection before connecting to an operator |
| First incoming callback | Caller and independent STT confirmed automatic greeting by the original calling session; provider history confirmed consumption of its callback preference |
| Subsequent incoming route | Next plain redial returned home with an automatic greeting; caller and independent STT passed |
| Explicit directory extension | Selected another active directory before the personal PIN; caller and independent STT verified its automatic greeting |
| Actual retry | Caller ended the first attempt before PIN entry, then answered the retry and heard its prepared topic; caller and independent STT passed. Eligibility was 60 seconds after observed failure, dispatch followed 2.5 seconds later, and provider submission followed 11 seconds after eligibility |
| Same phone, separate concurrent sessions | Passed: separate parent calls and native threads, second prepared opening verified by caller and STT; ending only the second call preserved the responding first call |
| Two-phone isolation | Not rerun on the container network; the user chose to finish handset testing with the available phone |
| Idle container restart | Passed: all twelve configured endpoints returned to healthy standby and retained their saved identities |
| Host reboot recovery | Startup services configured; actual machine reboot not yet tested |

The first frontend reset raced with an ordinary supervisor check. Reset now waits
for that lock, with a contention regression test. Same/fresh handset resets and
the later frontend repeat completed normally after the fix. The running service
received the targeted reset fix while the phone call remained connected; the
rebuilt runtime image was then deployed while idle; all twelve endpoint transports
were healthy afterward and retained their saved conversation IDs.

After migration, fresh conversations are used instead of forwarding historical
host CLI commands. The old histories are backed up. The source `default` profile
still identifies the provider project; the transfer destination `default` is the
home alias when directory routing is enabled. The home transfer was verified
by actual arrival, not by a spoken promise or a submitted redirect alone.

Not every handset pass has a corresponding STT pass. Some observation captures
contended for the same audio lock or expired before the caller repeated a step.
Those failed captures are retained. In particular, the successful return to home
has handset and call-state evidence, without an independently verified capture
of its arrival announcement. No failed capture is relabeled successful.

During the concurrent-call test, the caller reported a different voice in the
second session. Neither profile nor the host configuration explicitly selected
a named voice, and the frontend leaves that choice to Codex. The backend's
selected voice and the reason for the audible difference were not established.

## Evidence and reproduction

Run [TESTING.md](../TESTING.md) and the arranged
[handset runbook](MANUAL-TESTS.md). Clean runs record each step's exit code,
duration and logs. Native cases save source hashes, inputs, captured audio,
independent STT, exact state assertions, failures and cleanup. Handset evidence
also records the caller's observation, call identity, reset/transfer records and
remaining work. All raw artifacts remain private. Public summaries exclude
recipients, PIN material, account identifiers and native rollouts.

Assertions were not relaxed to hide native failures. Different test inputs or
fixture corrections are identified above. A native pass on an earlier source
revision, or a historical host-only handset pass, does not certify the final
container image. Actual reboot recovery,
review of known native limitations, exact-index/build privacy audit and publication
are still outstanding.

Tool execution time, backend turn time, result handoff, local audio offsets and
saved speech timestamps remain separate evidence. Transcript-save timing is
never treated as audible latency; local output capture is not handset playback.
