# One meeting: inspect, skip or start early

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

## Time and scope

Occurrence keys follow the existing DST policy: one meeting for a repeated local
fall-back minute; a skip covers both instants. A nonexistent spring-forward
minute cannot be selected. Invalid dates, times that do not match cron, another
session/recipient and unrecorded past occurrences are rejected. Existing saved
records remain inspectable even if their schedule was later disabled.

These commands support skipping a dated occurrence and doing a future meeting
now on an existing call. They do not schedule a replacement phone call at an
arbitrary different time, add effective-from/until dates to cron, or automate a
rotating work calendar. Do not infer those changes from a note. Retain the ledger;
deleting it loses the durable skips and crash recovery records.
