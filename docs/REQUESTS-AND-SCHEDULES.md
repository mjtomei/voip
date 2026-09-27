# Requests and scheduled calls

Worker messages, explicit calls, and cron occurrences use one durable request
queue. The default is `non_preemptive`. `preemptive` requests deliver context
without waiting for the current turn to finish; they do not claim to cancel
existing work or hang up a call.

```bash
# Post an agenda and queue one call to the selected session/recipient.
python3 voip.py request call --session default --recipient alice \
  --request-key review-42 --reason 'Review the deployment decision' \
  --from claude:reviewer --file /tmp/agenda.txt

# Send context without requesting a telephone call.
python3 voip.py request message --session default --mode preemptive \
  --request-key incident-42 --reason 'New incident information' \
  --from worker --file /tmp/incident.txt

python3 voip.py request status review-42
python3 voip.py request cancel review-42
```

`call start` is an alias for queueing a call, preserving existing syntax. It
returns the saved request promptly; a queued response is not evidence that the
phone rang. The daemon prepares context and dials when admitted. `--message ID`
can be repeated. Without a file or explicit messages, enqueue snapshots this
selected recipient's currently unaddressed messages in this project. Both
`request message` and `request call` accept `--recipient` or `--to`; they never
select another person's messages. The first attempt uses the
request key, so `call prepare --request-key KEY --brief-file FILE` still works
for an operator preparing its own call. Subsequent redial attempts receive
separate durable keys listed in request status.

A normal `mailbox post` queues a nonpreemptive message notification. Use
`--notify preemptive` for immediate context or `--notify passive` to store an
agenda without a separate notification, as when a call request will deliver it.
Message storage and request creation are two durable, idempotent writes; if a
command stops between them, repeat it with the same key. Reading, prompt
submission, or a completed phone request never automatically addresses a message.

## Admission policy

| Situation | Result |
| --- | --- |
| Same recipient already connected to the requested session | Keep the call, submit context there; no dialing or reset |
| Another recipient occupies the requested session | Keep that call; wait without exposing the new agenda to that caller |
| Requested recipient is on another session | Keep their current call; allow a separate call from the requested session |
| Distinct ready sessions, with the same or different recipients | Allow independent concurrent calls |
| Nonpreemptive message or reused-call request | Wait for backend idle and, during a call, a short local audio pause |
| Preemptive message or reused-call request for the connected person | Submit context now through the existing voice connection |
| Preemptive outgoing request while its off-call backend is busy | Steer context into the active turn; dial only after the backend can prepare |
| Session unavailable or its call attempt still pending | Persist and wait until ready or until the request expires |

One Baresip call per profile prevents callers from sharing that profile's audio.
Admission checks local calls and Twilio state, but an incoming call can race an
outgoing attempt; the later SIP connection can receive busy. Automatic requests do not transfer calls or create a cloud reservation.
Explicit caller-requested transfers use the separate [call control](CALL-ROUTING.md) interface. Reservations apply to the session, not the phone
number: multiple sessions may call the same configured recipient concurrently.
Each call has its own request key, PIN check, preparation, audio, and cancellation;
the recipient must be allowed to access every requested session. An ambiguous
phone attempt continues to block its own session until reconciled.

Twilio applies [account concurrency limits](https://www.twilio.com/docs/api/errors/10004)
and [call-start rate limits](https://www.twilio.com/docs/voice/api/call-resource).
Whether a second call to the same number can be answered depends on the receiving
phone service; ordinary requests do not merge or transfer the calls; caller-requested transfers are explicit. If the
carrier reports busy or no-answer, the ordinary configured redial policy applies.
Same-number concurrent calls passed the September 19
[handset acceptance test](MANUAL-ACCEPTANCE-2026-09-19.md), using call waiting
to hold the first call while answering the second.

The daemon checks roughly every three seconds per request and considers
preemptive requests first. Nonpreemptive submissions are serialized per session.
During a connected call, readiness uses current backend status plus a fresh
frontend heartbeat and at least two seconds without input/output PCM above a
small signal threshold. This is a **local audio-pause heuristic**, not guaranteed
semantic turn completion. Background noise can keep a request waiting. Saved
speech/transcript timestamps are not used as evidence of silence or audible
latency. An accepted native prompt is not proof that a person heard or acted on it.

Default lifetime is one hour (`--ttl-seconds`, up to seven days). Waiting requests
expire instead of ringing indefinitely. An already-active phone call may finish
after that deadline. If all selected agenda messages were addressed while the
request waited, it completes without dialing. Partially handled agendas prepare
the remaining messages. A preparation already in progress keeps its original
snapshot and requires inspection if later message changes invalidate it.

## Redial and recovery

Session `[redial]` settings are defaults. Schedule `[schedules.redial]` and CLI
options override them. Requests snapshot their policy when enqueued.
`max_attempts` includes the first attempt, ranges from 1 to 10000, and defaults
to 1. Fixed/backoff delay remains `delay_seconds * backoff_multiplier^(n-1)`,
capped by `max_delay_seconds`.

For an arbitrary list of delays after successive failed attempts:

```toml
[redial]
delays_seconds = [60, 300, 480, 3600]
# Optional cap; defaults to one initial attempt plus the four delays.
max_attempts = 5
retry_on = ["busy", "no-answer"]
```

Or pass `--retry-delays 1m,5m,8m,60m` to `call start` or `request call`.
Durations accept seconds (bare or `s`), minutes (`m`), hours (`h`), and days (`d`).
Each interval starts when the corresponding failure is observed, **not** at the
original dial. List entries bypass the backoff cap and allow up to one year
per interval, with at most 1000 entries. A supplied list defaults to length+1
attempts unless `max_attempts` is supplied in that same override. An explicit
larger attempt limit repeats the final delay; a smaller limit truncates retries.
Use `delays_seconds = []` to return to fixed/backoff timing.

The request lifetime still bounds retries. Without an explicit TTL, list-based
requests allow the sum of the chosen intervals plus ten minutes per attempt
(at least an hour); other policies retain the one-hour default. `--ttl-seconds`
and schedule `ttl_seconds` can override this, up to one year. Allow more time
for long preparation or busy sessions. Active calls are not cut off at expiry.
Indefinite [PIN delivery](PIN-DELIVERY.md) deliberately has no attempt cap or TTL.

Eligible outcomes are `busy`, `no-answer`, optionally `failed`, and optionally
`ended_without_sip_connection`. That last option can ring again after a PIN
failure or voicemail answer, so it is not enabled by default. A connected call's
normal hangup never retries. Unknown provider submissions never retry.

Reuse the same request key for an intended request. Changed details under an
existing key are rejected. A legacy phone attempt's key cannot be reused to
create a new queued call. `request status` is local and includes attempts,
waiting reason, prompt acceptance state, expiry, and retry timing. `call status
ATTEMPT_KEY --local` reads legacy phone/preparation state; queue request keys
return queue state. Use `request status` to inspect all queued requests.

Cancellation withdraws pending work immediately and asks the daemon to end any
confirmed phone attempt; `cancel_settled` becomes true once phone cancellation
is resolved. It does not stop worker tasks or the native operator's already
submitted task. Cancelling context already submitted cannot retract that prompt.
Unknown phone creation requires exact-ticket reconciliation:

```bash
# Load credentials from the documented private file or process environment.
python3 voip.py call reconcile ATTEMPT_KEY
python3 voip.py request resume REQUEST_KEY
```

Use the attempt key shown by status. If prompt submission was interrupted after
sending but before acknowledgement, the request blocks rather than repeating
context blindly. Inspect native session history; only if no prompt was accepted
use `request resume KEY --confirm-no-prompt`. Otherwise leave it blocked or cancel
it. Never assert that flag just to clear an error. A cancelled ambiguous phone
attempt stays pending until reconciled; the daemon then finishes cancelling it.

## Cron

See [the disabled example](../schedules.toml.example). Each enabled entry names
an agenda file, recipient, and session. Agenda paths are relative to the schedules
file. Schedules load each tick; use `config check` and `schedule next` before
enabling one. Copy the example agenda and session too, and change placeholder
numbers and PIN file paths locally before deployment.

```toml
[[schedules]]
id = "daily-review"
enabled = true
cron = "0 9 * * 1-5"
timezone = "America/Los_Angeles"
session = "default"
recipient = "alice"
reason = "Daily work review"
agenda_file = "daily-agenda.txt"
mode = "non_preemptive"
ttl_seconds = 1800
misfire = "skip"
max_lateness_seconds = 600

[schedules.redial]
max_attempts = 2
delay_seconds = 300
retry_on = ["busy", "no-answer"]
```

Five numeric cron fields support `*`, lists, ranges, and steps. Weekday 0 or 7
means Sunday. Restricted day-of-month and weekday fields use traditional cron
OR semantics. Every schedule explicitly names an IANA timezone. Spring-forward
nonexistent times are skipped; repeated fall-back local minutes enqueue once.
`misfire = "skip"` is the default: scheduled calls missed while the scheduler was
offline (including a powered-off machine) stay skipped, rather than ringing on
startup. Their count is included in the operator's next connection notice for
the **same session and recipient**, and omitted when zero. It remains private
service context and does not precede a prepared conversational opening or
authorize catch-up calls. Counts persist across restart and session reset.

The connection notice snapshots the skipped occurrences it reports. Its accepted
submission is the receipt; merely preparing the text or a failed submission does
not consume the count. Later skipped occurrences remain for a later call, and a
frontend reconnect does not report an accepted count as new. Native RPC acceptance
is not proof of audible delivery. An ambiguous submission may still repeat the
same attributed notice, as with other connection events.

Gap recovery uses the last observed enabled schedule configuration. A newly
enabled schedule has no invented missed history before its first observation;
existing installations begin tracking when this version first observes their
schedules. The connection-notice path can recover missing counts before the
dispatcher starts, without creating requests or dialing. Records use the existing
occurrence ledger and retain DST deduplication. A network-only outage while the
scheduler continues running still uses ordinary request readiness/expiry; this
counter describes skipped schedule occurrences, not failed calls or queue expiry.

An explicitly configured `misfire = "enqueue"` still catches up within
`max_lateness_seconds`, possibly producing multiple requests; older skipped
occurrences are counted. This installation uses the default skip policy. Busy-call
waiting is handled by the ordinary request queue and its TTL, not by misfire.

Occurrence keys are durable (`schedule:ID:YYYYMMDDTHHMM`, in the schedule's local
time). Once materialization begins, its saved agenda and context survive a crash
between mailbox and queue writes. That recovery can complete even if the schedule
has since been disabled; cancel an already-created request separately. Edits
apply to future occurrences, not already queued ones. Avoid changing timezone
under an existing schedule ID if you need a new independent occurrence history.

```bash
python3 voip.py schedule list
python3 voip.py schedule next --count 5
python3 voip.py schedule tick --dry-run
```

`tick --dry-run` performs no writes or calls. Plain `tick` enqueues due occurrences;
the enabled request daemon can then dial them. `schedule-state.json` and
`requests.json` persist across reboot; do not delete them to retry work.
