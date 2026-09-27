# Outgoing calls

`voip.py call start` enqueues a call request and returns its durable status. The
request daemon prepares the selected Codex session before dialing a configured
recipient from the server's Twilio number. Each recipient uses their own PIN;
only successful PIN entry bridges the selected SIP endpoint. No project agenda
is read to voicemail. Phone recording remains disabled.

```bash
python3 voip.py mailbox post --from worker --subject 'Review decision' \
  --key review-42 --file /tmp/agenda.txt --notify passive
python3 voip.py call start --session default --recipient alice \
  --request-key review-42 --reason 'Discuss the review' --message review-42
python3 voip.py call status review-42
python3 voip.py call cancel review-42
```

Use calls when requested or covered by an authorized arrangement. Normal Twilio
phone/SIP charges apply. Enqueueing needs no API credentials in the worker's
shell: `voip-requests.service` loads them from `~/.profile` for dispatch.

See [configuration](CONFIGURATION.md) for phone numbers, session prompts,
reset-on-outgoing-call, and redial settings. [Requests and schedules](REQUESTS-AND-SCHEDULES.md)
defines busy-call behavior, priority, expiry, retries, cancellation, and recovery.
A matching person already connected to the requested session receives the topic
on that existing call, without reset or another dial. Different named sessions
can call concurrently, including separate calls to the same number.

## Preparation and opening

The selected operator reads its mailbox agenda and prepares a JSON brief with
`ready`, `opening`, and `context` before dialing. Preparation uses its normal
backend tools and model settings. The first attempt's key is the request key.
Optional `call prepare --request-key KEY --reason REASON --message ID` performs
preparation without enqueuing a phone call. The daemon waits if the backend is
busy and resumes a known preparation turn instead of duplicating it.

An operator initiating its own call can read the agenda and supply a private
JSON file with `call prepare ... --brief-file FILE`, then enqueue `call start`.
Workers must not supply briefs on the operator's behalf. Enqueueing itself no
longer waits on another turn, so an operator can also enqueue and finish its
current turn, allowing ordinary preparation afterward.

Only a ready, current brief can reach the dial step. It is preloaded into voice
context. The PIN-verified connection notice carries the matching opening and
instructs the operator to discuss it immediately without fetching the mailbox
again or announcing connection/count metadata. Native voice generation still
takes time; this eliminates a required post-connection lookup, not every source
of latency. Preparation and speaking do not address mailbox messages.

Signed admission tickets bind the request UUID, destination, server number,
recipient ID, session ID, and expiry. No agenda or API credential is in the
ticket. Protected Functions also validate the account, call direction, and PIN.
The SIP-only `voip-REQUEST_UUID` caller identity correlates the connection to the
prepared opening; the handset sees the configured Twilio number. Incoming calls
carry a recipient identity and use their configured incoming session.

Hangup sends the identified session a notice asking it to review messages
handled on that call and mark only fulfilled ones addressed, with an outcome.
It does not stop workers, authorize a callback, or count as mailbox completion.
A voice reconnect retains the conversation and avoids intentionally repeating
an opening already submitted. Experimental native delivery is not an exactly-once
speech guarantee; saved speech timestamps do not measure audible latency.

## Validation

Automated Python and JavaScript tests cover per-recipient authentication,
session routing, queue admission, phone ambiguity/cancellation, scheduled
occurrences, reset-once behavior, and preserving the prepared-opening protocol.
The tests below record earlier actual handset acceptance; multiple recipients
and simultaneous handset calls still need a live acceptance test after those
recipients are configured.

### Prepare-before-dial validation: September 18, 2026

The preparation update passed 92 Python tests and 33 JavaScript tests, including
no dial before readiness, preload failure, retrying the same preparation turn,
stale context, exact SIP agenda matching, and preserving unaddressed messages.
The live saved operator produced its structured test opening in 15.394 seconds;
its preparation completed before any phone request. The targeted final-result
lookup and voice-context preload also succeeded. The update was deployed and
the frontend reconnected to the same operator UUID.

the primary user then authorized a handset test using request `prepared-opening-20260918-01`.
The expired brief was refreshed before dialing. After PIN verification, the SIP
identity matched that exact request and its connection notice supplied the
prepared opening. The saved voice transcript contains the opening and the primary user's
confirmation, “Yes, I heard harbor lantern.” No backend tool calls were recorded
after dialing during this call. Both phone/SIP legs ended normally; the same
operator remained connected. The test message was addressed after verification.

This confirms handset receipt of the prepared opening without a post-connection
backend lookup. Audible latency was not measured. Two identical opening texts
were saved; those records alone do not establish repeated playback or its timing.
Private evidence is in `state/prepared-opening-acceptance.json`.

### Handset acceptance: September 18, 2026

The outgoing handset test passed: the configured mobile rang, the primary user entered the
existing PIN, and the SIP leg connected to the same persistent operator. The
connection notice included one unaddressed mailbox message. The operator read
the worker's message and spoke its test phrase, which the primary user confirmed hearing.
The phone leg lasted 67 seconds; handset hangup closed both phone and SIP legs
and produced a submitted call-ended notice.

Repeating the original call request key reused the same Call SID and did not
redial. Cancelling that already-ended request returned the ended state safely.
Active-call cancellation remains covered by automated tests; this handset test
ended through ordinary hangup.

The mailbox deliberately remained pending after reading and hangup. Following
the primary user's text confirmation, a local TTS request exercised the completion step:
the operator retrieved a fresh proof value from the message, wrote and verified
the proof file through its CLI tools, and marked the message addressed. This
completion step was tested through local audio, not a second handset command.
The mailbox returned to zero, and the saved operator identity remained unchanged
and connected. Posting the identical message again did not reopen it.

All **74 Python tests and 32 JavaScript tests passed**, including the daemon and
WebRTC transport tests under `.tools/voice-venv/bin/python`. Hosted routing and
PIN/SIP configuration were also reverified. Private live evidence is in
`state/outgoing-mailbox-acceptance.json`. Notice submission establishes RPC
acceptance; actual mailbox use was separately demonstrated by the readback and
proof-file check.

### Earlier deployment checks

On this installation, the hosted build and three protected routes were verified
after deployment. Thirteen outgoing controller tests, 32 incoming/outgoing
JavaScript tests, and 16 existing provisioning/supervisor checks passed. The
saved operator remains connected. Acceptance evidence is stored privately in
`state/outgoing-acceptance.json`; handset acceptance was completed subsequently
as described above.
Three voice transport checks also passed. A fresh TTS request through the actual
virtual microphone produced the expected shell result and audible response
(RMS 802); the exact spoken response words were not independently verified.
An unrelated stale Codex runtime discovered during preflight was recovered using
native archive/unarchive and resume of the same UUID; see
[restart handoff](OPERATIONS.md). The transcript was preserved.

Provider references: [Calls API](https://www.twilio.com/docs/voice/api/call-resource),
[call request events](https://www.twilio.com/docs/voice/api/call-event-resource),
[keypad gathering](https://www.twilio.com/docs/voice/twiml/gather).
