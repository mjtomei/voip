# Changing meeting settings during a call

An authenticated caller can ask their operator to move, pause or resume their
own recurring meetings, or change the conversation routine. Operators use their
normal file and CLI tools; there is no second assistant or phone menu. Project
instructions determine who may change shared workflow settings. This is an
operator protocol, not a filesystem sandbox: host tool permissions still apply.

For example, "I work weekdays from eight until five now; call me after work"
establishes availability but not an exact call time. The operator should ask for
the desired time, days, timezone and effective date as needed, agree what happens
to the other check-ins, then make and verify the requested changes. "Move my
weekday review to six p.m. Central starting now" is already specific enough to
execute. Do not demand another approval after the caller has clearly asked.

## Operator procedure

Use the current session's supplied CLI prefix, including its project root and
session selection. `config show` identifies the profile and local configuration;
`schedule list`, `schedule next --count 6` and `request status` show the current
recurrences and outstanding work. An external project owns its own schedules
file. Do not edit another project's file merely because it shares the provider.

1. Identify the authenticated recipient and the requested scope. Record agreed
   availability, timezone and whether it is temporary in the project's own notes.
   Do not infer new working hours or a start date from a possible future job.
2. Prepare complete candidate file contents. Preserve other recipients' entries,
   schedule IDs, retry policies and unrelated settings. Set `enabled = false`
   to pause a recurrence. Days use numeric cron (1-5 is Monday-Friday); every
   schedule needs an IANA timezone. Inspect the candidate's parsed schedules and
   next occurrences before publishing it, then atomically replace the file.
3. Run `config check` and `schedule next --count 6` through that same session's
   CLI. Verify the affected recipient/session and actual local dates/times.
   Correct or restore invalid edits; a saved file alone is not proof of success.
   Confirm the new next call, timezone, days and any paused check-ins aloud.
4. Inspect outstanding requests from the affected `schedule:ID` sources. Editing
   or disabling a recurrence does not cancel materialized requests or retries.
   Use `request cancel KEY` only for matching pending work the caller intends to
   withdraw, and verify its status. Never cancel the request owning the active
   phone call to change future meetings: cancellation can disconnect it. Calls
   already ringing or connected require explicit call-control intent.
5. Save the change and its effective scope in local notes. In a Git-managed
   project, review and commit the specific configuration/prompt and note changes.

For an atomic schedule edit, the existing Python interfaces can validate a
candidate without writing queues or dialing. Run with the installed service
Python import path (the container uses `/opt/voip`), and put the candidate beside
the real schedule file so relative agenda paths retain their meaning:

```python
from copy import deepcopy
from voip import Project, atomic_write
from voice_config import configured_path
from call_schedule import Scheduler

p = Project('/path/to/project', 'daily')
destination = configured_path(p, 'schedules_file', 'schedules.toml')
candidate = destination.with_name('.schedules-candidate.toml')
# Write the requested complete candidate first; never copy real secrets here.
old = destination.read_text()
trial = Project(p.root, p.session_id)
trial.config = deepcopy(trial.config)
trial.config['service']['schedules_file'] = str(candidate)
Scheduler(trial).definitions()   # Validates recipients, agendas, cron and policy.
print(Scheduler(trial).next(6))   # Read-only preview for review.
# After checking the preview, detect concurrent edits and install atomically.
assert destination.read_text() == old, 'Schedule changed; reread before editing'
atomic_write(destination, candidate.read_text())
candidate.unlink()
```

This compare-before-write recipe is for the single operator/config editor; it
does not provide a transaction against arbitrary concurrent external editors.
Coordinate overlapping edits and reread immediately before replacement.
The dispatcher reads the file on its next tick, without a service restart or
Twilio deployment. Updating a schedule to the current minute can make it due
immediately, so choose future times unless a call now was requested.

## Prompt changes

Edit the selected profile's inline prompt or referenced prompt file, and the
relevant project instructions/agenda when needed. Keep changes scoped to the
requested routine: meeting length, tone, planning questions and work priorities.
If several people share the session, label personal preferences by recipient.
Keep the `{{default}}` marker in connection/disconnection/preparation/request
templates so service notices retain authentication, call identity and mailbox
semantics. Preserve core call control and privacy rules.

Validate the candidate profile and prompt placeholders before replacing complete
files atomically; `config check` rejects invalid text, paths and placeholders.
Existing operators reread prompt text when rendering a connection, disconnection,
preparation or request notice. Initial instructions are read on the next native
thread resume, normally when voice starts for the next call. This preserves the
same conversation, SIP call and frontend. Previously delivered notices/briefs and
an already-running turn are not rewritten. Honor an agreed preference in the
current conversation, and do not reset the session to force a new system prompt.

If a live edit is temporarily invalid, an existing frontend retains its last
validated prompt set and logs a warning; a fresh `config check` still fails.
Correct the edit before claiming it is installed. This reload changes prompt
text only, not live permissions, audio, extensions, recipients or provider
settings. Those remain separately administered configuration.

## Limits

The scheduler supports recurring numeric cron, not effective-from/until dates,
rotating shift calendars or dated exceptions. Availability notes alone do not
block calls. Do not promise that "starting next month" or "skip just tomorrow"
is automated by adding a note. Clarify whether the person wants an immediate
recurrence change/pause, or a recorded future change still needing application.
Prompt changes cannot authorize unrelated work or bypass project permissions.

See [requests and schedules](REQUESTS-AND-SCHEDULES.md) for retries, queue
cancellation and DST behavior, and [manual tests](MANUAL-TESTS.md) for the
caller-directed settings acceptance procedure.
