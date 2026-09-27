# Handset acceptance — September 19, 2026

the primary user and the text operator tested the deployed `default` session and a temporary
`phone_test` session using two phones authorized by the primary user. These tests used real Twilio calls, PIN entry, SIP connectivity,
handset-originated keypad commands, and independent local STT where spoken output
was expected. the primary user confirmed what he actually heard. Entered PIN digits were not
recorded in results. The generated second-phone PIN was kept in a private local
file and removed after restoring the original routing.

Results and evidence (private historical artifact `state/manual-tests/20260919-170938/results.json`) include
the individual recordings, provider outcomes, call identities and reset records.
These observations supplement the earlier
[automated acceptance results](VOICE-ACCEPTANCE-RESULTS.md); successful manual
cases do not erase previously observed failures or establish a reliability rate.

| Check | Result | Observations |
| --- | --- | --- |
| Prepared outgoing opening after correct PIN | Pass | the primary user heard the prepared “blue lantern” topic promptly and clearly. Independent STT verified the opening after connection. |
| Priority request for the current session/recipient | Pass | “Green notebook” was delivered and acknowledged on the same call and conversation, with no new phone attempt. |
| Ordinary queued decision | Pass | The operator asked about “morning review” versus “evening review.” the primary user explicitly deferred the decision; its mailbox record remained unaddressed. This did not separately test waiting during continuous speech or a busy backend. |
| Automatic hangup review | Fail | The ended notice was submitted. After a 90-second observation window, both acknowledged notifications still had no addressed status or outcome note. The deferred decision correctly remained open. |
| Configured one-retry call | Incomplete | Provider records show a first no-answer attempt, one retry, and enforcement of the two-attempt cap. The second attempt ended without a SIP connection. the primary user reported seeing only one call and was still waiting for a retry. Neither attempt produced an authenticated spoken opening. Provider retry behavior was observed; successful handset redial acceptance was not established. |
| Incoming call with correct PIN | Pass | The call connected; an injected TTS request produced an independently transcribed “recovery test ready” response. |
| Handset `*1#` | Pass | Command recognized, same call and conversation ID preserved, and “frontend recovery verified” heard by the primary user and captured by STT. See the assertion correction below. |
| Handset `*2#` | Pass for recovery | Command recognized, same call and conversation ID preserved, and “session recovery verified” heard and independently transcribed. This test does not prove recall of all earlier spoken content. |
| Scheduled request during an existing call | Pass | A temporary real cron entry created a request at its due minute. The dispatcher reused the same call and conversation without dialing; “orange harbor” was heard and acknowledged. The schedule was removed immediately after its request appeared. |
| Handset `*3#` | Reset passed; speech check failed | the primary user explicitly selected a fresh conversation. The ID changed, the old rollout backup exists, and the same call survived. However, the TTS probe did not produce “fresh conversation verified” within its 120-second response window. Captured output discussed reviewing the mailbox instead; the primary user also reported no verified response. |
| Incoming wrong PIN | Pass | the primary user confirmed rejection. The corresponding provider call completed without a child SIP leg; no new local connected event appeared. |

## Second phone, concurrency and retry

The temporary profile had its own persistent native thread, audio devices, SIP
credential, mailbox and configured greeting. The second recipient had a different
PIN and routed incoming calls to that profile. the primary user's primary recipient was also
temporarily allowed to call that profile so same-number concurrency could be
tested. Shared server caller ID remained configured separately.

| Check | Result | Observations |
| --- | --- | --- |
| Another recipient's PIN | Pass | Calling from the second phone with the primary phone's PIN was rejected. Provider records show no child SIP leg, and neither local session received a connection. |
| Second recipient's own PIN and incoming route | Routing/audio passed; greeting behavior imperfect | The new PIN reached `phone_test`, and its session-specific greeting was recorded and eventually heard. the primary user initially reported silence. When asked for its identification phrase, the operator repeated its greeting instead; immediate greeting and correct first identification answer are not claimed. |
| Both phones dialed at once | Incomplete first attempt | Both phones rang, but the first ended before the primary user could enter its PIN while he was handling the second. The abandoned attempt and recordings are retained. |
| Overlapping calls to different numbers | Pass | We connected the first phone and completed its PIN entry before dialing the second. Both authenticated calls remained connected to distinct native threads. The first delivered “silver meadow,” the second “copper forest”; the primary user confirmed both. |
| Simultaneous audio on both sessions | Pass | Concurrent TTS requests through the two virtual microphones produced “first channel ready” and “second channel ready” on their respective speaker monitors. Independent STT required the correct phrase and forbade the other channel's phrase. Both calls stayed connected, and the primary user confirmed both handset responses. |
| Cancel only one active outgoing call | Pass | Cancelling the second request reached `cancel_settled: true`, ended its phone call, and preserved the first call identity. the primary user confirmed only the second phone disconnected. |
| Two sessions calling the same number | Pass | With the first session's call still active, the temporary session placed another call to the same primary number. the primary user used call waiting, held the original call, entered the primary recipient's PIN and heard “amber river.” Both sessions retained separate call identities and native threads; neither call was merged or transferred. |
| Completed configured retry | Pass for the selected policy | On the second phone, the primary user answered the first attempt and hung up before PIN entry. An explicit per-request policy allowed one retry after 20 seconds for `ended_without_sip_connection`. the primary user answered the retry, entered its PIN and heard “violet garden,” also verified by STT. This policy is an opt-in override; it does not change the production defaults. |

The initial simultaneous-dial recorder continued waiting on its other verification
task after the first call failed admission. Once both calls had ended, we stopped
only that test recorder and its children, retained its final PCM as WAV, and ran
the new, explicitly arranged sequence with one PIN-entry step at a time. The
initial failed attempt was not overwritten or counted as a pass.

## Interpretation and test corrections

The hangup failure is a failure to perform the required mailbox review, even
though the service's native prompt submission succeeded. No test driver marked
the notifications addressed to manufacture a pass.

The fresh-reset controller succeeded, but restored transport did not imply a
successful response to the test request. That distinction also applies to the
successful frontend/session recovery checks: preserving the UUID does not prove
spoken-only recall, which failed in earlier isolated tests.

For `*1#`, independent STT rendered “frontend” as “front-end.” The initial exact
spacing assertion rejected correct speech. The observer was stopped, its original
result retained, and the same captured transcript was rechecked with an explicit
spacing alternative. The correction record (private historical artifact `state/manual-tests/20260919-170938/keypad-frontend/verification-correction.json`)
includes the independent speech, matching reset identity, surviving call and
the primary user's confirmation. No second reset or newly generated response was used to
replace that evidence.

Recordings prove output at the local audio monitor. Handset claims come from
the primary user's separate confirmations. Provider state, native request acceptance, backend
execution, result handoff, PCM offsets and saved-speech timestamps remain distinct;
no audible latency is inferred from transcript-save timing.

## State at the end of these checks

The fresh conversation selected by the primary user remains the default operator, with voice
transport connected. The old conversation was saved by the reset controller.
No test phone call, unfinished manual request or temporary schedule remained
active. Original recipient routing was restored and deployed. The temporary
profile's services were disabled, its native thread archived, its audio devices
unloaded, and its provider SIP credential and local temporary PIN removed. The
default conversation ID was preserved throughout that cleanup.

Test mailboxes and failed evidence were retained, including the deferred
choice; these checks did not manually repair the failed hangup review.

Number/PIN routing, both concurrency modes, active cancellation and a completed
retry now have handset evidence. The earlier no-answer attempt did trigger a
provider retry, but that particular retry was not authenticated at the handset;
the fully completed retry used the explicitly configured pre-PIN-hangup policy.
Separate carrier-specific busy outcomes and network/audio-server outage recovery
were not tested in this manual run.
