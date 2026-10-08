# One meeting: inspect, skip, reschedule or start early

The recurring schedule stays in TOML. One-off dispositions live in the durable
project schedule ledger and survive restart. All commands use the intended
project root, session and explicit recipient. They never place a call themselves.

```sh
voip --root /path/to/project --session daily schedule next --count 6 --json
```

Each result includes an `occurrence_key`, such as
`schedule:daily-review:20261008T1300`. Copy it rather than inventing request keys.
Its date/minute is local to that schedule's configured timezone.

## See what the meeting is about

```sh
voip --root /path/to/project --session daily schedule agenda \
  schedule:daily-review:20261008T1300 --recipient alice --json
```

The result contains the reason, agenda text, scheduled local/UTC time, disposition
and mailbox ID/status when materialized. Before materialization it reads the
current agenda file; afterward it uses the occurrence's frozen snapshot, even
if the file changes. This read neither creates a request/mailbox message nor
starts a model turn, dials, skips or completes the meeting.

The model-written opening/context is normally generated shortly before outgoing
dialing. If a matching ready preparation already exists, `prepared_brief` exposes
that historical snapshot and its creation timestamp. Otherwise it is null; the
agenda still exists. A saved brief is not proof of current preparation readiness.
During an existing conversation, the operator reads the agenda and the referenced
project context itself. `call prepare` is an off-call operation and should not
be run by the active operator while waiting on its own turn.

## Skip only this occurrence

```sh
voip --root /path/to/project --session daily schedule skip \
  schedule:daily-review:20261008T1300 --recipient alice \
  --note 'Caller cannot attend this meeting' --json
```

The command saves `state=skipped`, `outcome=caller_skipped`. `schedule next`
omits that occurrence, and the due-time dispatcher does not enqueue it. Tomorrow's
meeting and other recipients/sessions are unchanged. Deliberate skips do not
increase the offline-missed-call count. Repeating the command returns its saved
result rather than changing the original note or creating another occurrence.

If the due time already produced an unsubmitted queued request, skip cancels
that request too. A settled retry waiting for its next attempt may also be
cancelled. A worker currently processing the request, unknown/in-progress attempt,
accepted topic delivery, or active/completed phone call is refused for inspection;
skip does not disconnect anyone. Cancellation and worker submission share a
per-request lock. Existing mailbox agendas remain unaddressed until their purpose
is actually handled or the recipient explicitly dismisses them through the usual
mailbox flow. A skipped meeting is not a completed meeting.

## Hold the meeting on this call instead

```sh
voip --root /path/to/project --session daily call current --json
voip --root /path/to/project --session daily schedule start-early \
  schedule:daily-review:20261008T1300 --recipient alice --call-id LOCAL_CALL_ID \
  --note 'Caller wants to hold the afternoon meeting now' --json
```

The service verifies a fresh connected call for this recipient in this session.
It snapshots the agenda, stores one unaddressed mailbox message using the same
occurrence key, and records `outcome=started_early`. The original time is omitted
from future calls. It returns the agenda to the calling operator's current tool
turn; it does not enqueue another notification, open a second call or reset the
conversation. The operator discusses it now and plans through the next remaining
meeting, keeping the original meeting time in the notes for traceability.

The active call must have passed the ordinary telephone authentication route.
Unknown, stale, ended or other-recipient/session calls are rejected. Replaying an
accepted operation on its same call is idempotent; a different call cannot silently
take ownership of that early meeting. The saved agenda remains open if interrupted.
Mark it addressed only when the requested discussion and planning are complete.

A durable `early_pending` record reserves the occurrence before the mailbox write.
Crash recovery finishes that same mailbox write without dialing at the original
time or resetting a later addressed outcome. The recurring TOML is unchanged.

If the occurrence is already due, read `schedule agenda` and its existing mailbox
instead. Use `schedule skip` only when the caller wants to suppress a still-pending
dial; respect a refusal for an already-dispatched attempt. Do not end the present
call merely to manipulate its request state.

## Move just this meeting to another time

```sh
voip --root /path/to/project --session daily schedule reschedule \
  schedule:daily-review:20261008T1300 --recipient alice \
  --at '2026-10-08T15:30:00-05:00' \
  --note 'Caller requested 3:30 p.m. today instead' --json
```

Choose a future, minute-aligned ISO date/time with an explicit UTC offset or `Z`.
The offset identifies an absolute instant, including during a repeated DST hour.
The result and `schedule next` normalize it into the schedule's IANA timezone;
verify that local date/time before confirming it to the caller. No timezone is
guessed from the host, and this command never dials immediately.

The occurrence key and mailbox agenda stay the same. The original cron trigger
is suppressed, and the replacement appears in `schedule next` with its original
and new times. The command freezes the agenda, reason, recipient number, mode,
retry policy and lifetime; editing a later template does not rewrite this meeting.
Tomorrow's regular occurrence is unchanged, even when this meeting moves across
midnight. `schedule agenda` reads the snapshot and shows the replacement time.
An older prepared brief is still labeled historical; normal call preparation
runs for the replacement request when it becomes due.

Repeating the same move returns the saved result. Moving it again uses the same
occurrence key and records another revision with a new returned `request_key`.
Only the latest revision can be dispatched. Inspect `request status REQUEST_KEY`
after it becomes due; a future reserved replacement has no queue entry yet.
Never invent revision/request keys manually. The ledger retains the move history.

Pending unsubmitted requests and settled retries are cancelled before committing
the new time. Busy workers, ambiguous attempts, accepted prompts and active or
completed calls are refused. A crash after cancellation cannot resurrect the old
attempt; repeating the command completes the move after inspection. At due time,
mailbox/request materialization recovers under the same saved identities. Reading,
moving or cancelling the meeting never marks its agenda addressed.

The replacement uses the ordinary dispatcher: a matching current call receives
the topic according to its preemption mode, another recipient waits, and another
session can operate independently. Retry timing is unchanged, and the request's
TTL starts at the replacement time. If offline across that time, its saved
misfire policy applies: default skip and count one missed occurrence, or explicit
bounded catch-up. The original time is never counted as an additional miss.

Use `schedule skip` to cancel the replacement, or `schedule start-early` to hold
it on a matching current call before its new due time. An explicit reschedule
may restore a deliberately skipped meeting, but cannot move one already started
early, delivered, completed, expired or recorded as missed while offline.

Saved replacements remain scheduled even if their recurring template is later
disabled or removed. Cancel them explicitly with `schedule skip`. A different
meeting already at the destination time remains a separate agenda/request; the
ordinary busy-call policy handles both. Do not use a temporary recurring cron
edit or an external restoration timer for new one-off requests.

## Time and scope

Occurrence keys follow the existing DST policy: one meeting for a repeated local
fall-back minute; a skip covers both instants. A nonexistent spring-forward
minute cannot be selected. Invalid dates, times that do not match cron, another
session/recipient and unrecorded past occurrences are rejected. Existing saved
records remain inspectable even if their schedule was later disabled.

These commands support skipping, moving a dated occurrence and doing a future
meeting now on an existing call. They do not add effective-from/until dates to
recurring cron or automate a rotating work calendar. Do not infer those changes
from a note. Retain the ledger;
deleting it loses the durable skips and crash recovery records.
