# Configurable voice sessions and calls

`server.toml` holds shared infrastructure settings, including the Twilio number.
`recipients.toml` maps people to numbers, distinct PINs, and allowed sessions.
`sessions/NAME.toml` configures each operator conversation. `schedules.toml`
creates timed requests through the same queue used by workers.

See the [configuration example index](../examples/README.md) for copyable templates
for host/container settings, recipients, sessions, prompts, schedules, credentials
and the local Compose overlay, plus the commands that generate runtime files.

The existing installation has been migrated to `default`, recipient `alice`,
and the existing `state/call-pin`. Its conversation persists across calls.
The default project has no schedules. Registered projects manage their own
schedules. `config.toml` remains a compatibility fallback only
when `server.toml` does not exist.

Scheduled calls missed during scheduler downtime are skipped by default. Their
nonzero count reaches the next connection notice for the matching session and
recipient; no automatic catch-up call is placed. See [offline handling](REQUESTS-AND-SCHEDULES.md#cron).

## Server and recipients

Start with [the examples](../server.toml.example). Keep `[audio]`, `[operator]`,
and `[sip]` in the server file. The purchased, voice-capable Twilio number belongs
in `[twilio].phone_number`; recipient and session files do not set caller ID.
The setup command configures an existing number; it never buys a number.

```toml
# server.toml, in addition to the existing audio/operator/sip settings
[twilio]
phone_number = "+12025550100"

[service]
recipients_file = "recipients.toml"
sessions_dir = "sessions"
schedules_file = "schedules.toml"
```

```toml
# recipients.toml: use real numbers and private PIN files locally.
default_recipient = "alice"

[recipients.alice]
number = "+12025550101"
pin_file = "state/alice-pin"
sessions = ["default", "daily"]
incoming_session = "default"

[recipients.bob]
number = "+12025550102"
pin_file = "state/bob-pin"
sessions = ["daily"]
incoming_session = "daily"
```

PINs contain 6–12 digits, must differ across recipients, and submit automatically
without `#`. `pin_file` now points to a mode-0600 JSON verifier, relative to the
project root. Generate with `python3 voip.py pin generate NAME --delivery print`
or use confirmed telephone delivery. Legacy inline/plaintext files are accepted
for migration; `config apply` replaces them with verifiers while preserving the
PIN. See [PIN generation and delivery](PIN-DELIVERY.md).

Numbers use E.164 and are unique. Incoming calls default to `incoming_session`;
outgoing calls select the requested session, which must be allowed in `sessions`.
`config check` never prints a PIN or verifier. Deployment uploads verifiers and
routes as a private Twilio Asset referenced by protected Functions; it removes
the legacy plaintext `CALL_PIN` variable. PIN delivery uses a separate encrypted
Sync document and a dedicated IVR, outside the operator's conversation.

## Session profiles

Manual extensions can coexist with opt-in [directory extensions](DIRECTORY-EXTENSIONS.md).
Their `[directory_extensions]` settings live in the provider's `server.toml`.
The optional absolute `[operator].working_directory` separates native tool cwd
from the project directory holding configuration and state. It defaults to that
project directory. Changing it does not move an existing native thread; resume
rejects a different workspace.

Create `sessions/daily.toml` before referencing `daily`. Names start with a
lowercase letter and use up to 32 lowercase letters, digits, `_` or `-`.
See [the complete example](../sessions/daily.toml.example).

```toml
reset_on_outgoing_call = false
control_port = 15445
extension = "200"

[redial]
max_attempts = 1
retry_on = ["busy", "no-answer"]
delay_seconds = 300
backoff_multiplier = 1
max_delay_seconds = 3600

[prompts]
initial = "Be a concise collaborative partner in the caller's work."
connection = "{{default}} Ask one question at a time."
disconnection = "{{default}} Record agreed next steps."
preparation = "{{default}} Lead with the decision needed."
request = "{{default}}"
```

Each profile has its own persistent Codex thread, frontend process,
Baresip SIP username/control port, and virtual microphone/speaker pair. All use
the same existing Codex daemon and host tools. Mailboxes belong to a person and
project directory, so sessions within that project share them. Profiles are separate conversations,
**not OS security boundaries**: they retain the requested access to this machine.
The default profile keeps the original paths and device names. Other state lives
under `state/sessions/NAME/`; requests, phone attempts, and scheduling ledgers are
shared under `state/`.

Nondefault control ports are derived from the name unless specified. Validation
rejects collisions. The default profile uses `[sip].control_port` in the server
file (4444 if omitted). User units use instances such as
`voip-baresip@daily.service`, `voip-call-events@daily.service`, and
`voip-operator@daily.timer`.

Prompt settings replace their built-in text; use `{{default}}` to preserve it and
append instructions. Omitted or empty settings retain defaults. `initial` adds
native developer instructions on thread start/resume. `connection` and
`disconnection` customize service notices; `preparation` controls the normal
backend turn that produces the pre-dial brief; `request` controls queued context.
Worker agendas remain separately appended data.

Instead of inline text, use `initial_file`, `connection_file`, etc., with paths
relative to the session file. A setting and its `_file` alternative are mutually
exclusive. Substitution is literal, with no shell evaluation:

| Prompt | Available placeholders |
| --- | --- |
| All | `default`, `session_id`, `mailbox_command` |
| Connection/disconnection | Also `call_id`, `recipient_id`, `mailbox_count`, `prepared_opening` |
| Request | Also `request_key`, `mode` |

Keep the default hangup review and prepared-opening instructions unless you
intend to replace those behaviors. `reset_on_outgoing_call = true` archives the
previous thread and creates a fresh one once per actual outgoing attempt,
before preparation. Retrying the same in-progress attempt does not reset again.
An already-connected matching call is reused without resetting. Incoming calls
keep the currently selected conversation. Worker sessions continue running.

## Release configuration

The release CLI adds `init`, `config show`, `config plan`, `status --json`, and
`service` controls; see [the quickstart](../README.md). Server settings reject
unknown fields. `[operator].permissions` defaults to `read-only`; a session can
override it with `workspace-write` or `danger-full-access`. `manage_daemon=false`
connects to an already-running daemon through `daemon_socket`. `cli_command` is
an argument array for the operator's host-side service CLI. No shell expansion
is performed. Full reset on a custom daemon additionally requires `state_database`.

## Apply and startup

```bash
python3 voip.py config check
# Credentials come from process environment or service.credentials_file.
python3 voip.py config apply
```

Apply checks configuration, provisions/verifies Twilio routing and per-session
SIP credentials, installs/enables user units, and reloads idle voice frontends
without replacing their conversation IDs. It refuses to start while a phone
call is connected. If someone connects during deployment, that session's reload
is deferred and reported; apply again after the call. Existing unrelated Twilio
application/trunk/webhook assignments are not overwritten automatically.

`voip-requests.service` runs the queue and cron scheduler and loads Twilio API
credentials from the configured private file or process environment. Workers only enqueue local requests and
do not need those credentials. **Enabling this service allows existing queued
requests and enabled schedules to place calls.** The apply operation itself does
not enqueue a test call. All configured services are enabled for subsequent
user-service startup; boot without login also needs user lingering enabled.

```bash
systemctl --user status voip-requests.service
python3 voip.py --session daily operator status
python3 voip.py request status
python3 voip.py schedule next --count 5
```

Removing a profile file does not automatically destroy its saved conversation or
stop its old units. First finish/cancel its requests and calls, stop/disable its
four unit instances, and remove it from recipients/schedules before removing the
file and applying. Existing audio modules can remain unloaded only after all
users of that profile have stopped. No automatic voice/text switching is added
by this change.

## Deployment verification: September 19, 2026

The current host passed 134 Python tests and 38 JavaScript tests. One optional
`tmux-resume` integration test was skipped because its exclusion patch is not
installed. Coverage includes real local PipeWire/WebRTC transport, request
admission/retries/cancellation, independent profile state, PIN/session routing,
schedule crash recovery and DST, and per-attempt reset recovery.

Twilio deployment and private-Asset verification passed. Unsigned requests to
all three protected handlers and the private Asset returned HTTP 403. The saved
default conversation remained unchanged and voice connected. Baresip, the call
observer, audio/operator recovery timers, and the request dispatcher are enabled;
user lingering is already enabled on this host. Live dispatcher readiness passed.
No calls were originated during this update, no schedules were enabled, and the
request queue was empty after deployment. Simultaneous calls with multiple real
recipients still require handset acceptance once additional numbers are configured.

## Project-owned voice sessions

A project with one or more named sessions can own its configuration, working directory, mailboxes, queues,
schedule state, and service logs outside the phone-service checkout. Register
only its path in the provider's server.toml:

```toml
[service.projects]
project = "/path/to/project"
```

The project's server.toml selects its sessions and refers back to the
provider installation:

```toml
[service]
provider_root = "/path/to/voip"
session_ids = ["project", "project-review"]
sessions_dir = "voice/sessions"
recipients_file = "voice/recipients.toml"
schedules_file = "voice/schedules.toml"
```

Put profiles at voice/sessions/project.toml and voice/sessions/project-review.toml.
Each may have its own extension, prompts, conversation, reset policy, and audio
endpoint. Both share the project's per-person mailboxes and request queue.
Session names and extensions must be globally unique. Prompt-file paths remain
relative to the profile file; agenda paths are relative to the schedules file;
PIN-file paths are relative to the project root. Audio/operator/SIP defaults
are inherited, while the originating Twilio number always comes from the
provider. Recipient names and numbers must be unique across the installation.
A recipient is defined once, with one number/PIN and a list of allowed sessions
across projects. A new project using existing people need not copy their records
or credentials: its recipients file can contain only `default_recipient = "alice"`.
Access is granted by adding its session names to the original recipient's
`sessions` list. Nested
registrations and registrations pointing back to the same directory are rejected.

Use the installed CLI with `--root /path/to/project --session project`, or use
`--session project` from the provider root. Both resolve to the same project.
This also sets the native operator's actual working directory, so it loads that
project's AGENTS.md. Local state stays under the project; shared provider IDs,
API configuration, the admission signing secret, and PIN encryption master key
remain provider infrastructure. Hosted access configuration aggregates the
registered routes/verifiers without copying task prompts into the provider.

Run `config apply` through the provider root after configuration/PIN changes.
It validates external schedules and installs one `voip-requests@project.service`
alongside the session's existing audio/operator/SIP units. Each external project
has its own dispatcher and queue shared by its sessions; the first configured
`session_ids` entry names that project's dispatcher unit. Keep that first entry
stable when adding sessions. Global request status does not merge these
queues. Use the intended session when reading or cancelling a request. Schedule
edits are reloaded by that project's dispatcher without redeployment.

Codex retains its standard internal database/rollouts under its own home. A
project may export its own transcript snapshots alongside its meeting notes;
do not relocate live daemon files or mix other sessions into those exports.

## Incoming project extensions

An optional `extension = "200"` at the top of a session profile registers a
1–5 digit extension beginning with 1–9. It must be shorter than every personal
PIN (6–12 digits), so routing and authentication input are unambiguous.
Quote it in TOML. Run provider `config
check` and `config apply` after changing routes or allowed sessions.

Calling the plain number and entering the personal PIN connects immediately to
`incoming_session`. There is no post-PIN extension prompt or selection timeout.
To select a particular session, send its extension followed by `#` **before**
entering the PIN. That selects the route, then the service asks for the normal
personal PIN and connects to the selected existing conversation.

Save a project-specific contact using the base number, a pause, and extension#.
For example, with a fictional provider number, `+12025550100,,200#` dials the
number, waits after answer, and sends `200#`. Enter the PIN manually when asked,
or optionally save the full sequence privately on your own phone as
`+12025550100,,200#,,YOUR_PIN`, replacing the placeholder on the phone. For the
usual session, use `+12025550100,,YOUR_PIN`. No final # is needed after the PIN;
its configured digit count submits it automatically. The second pause lets the
extension callback open the PIN collector. This stores the PIN as visible
contact data, possibly included in contact sync/backups; it is separate from
the server's protected PIN storage. Never ask an agent to retrieve or print the
real PIN, and keep it out of project files and transcripts. The same contact can be
dialed repeatedly without navigating an extension menu. Use the saved contact
for project redials; preservation of post-dial digits in call history is not
assumed. A bare `+number200` is not a separate publicly dialable phone number.

The extension is public routing data, never authentication. Both its selection
and PIN callback check the caller's configured session permissions; the latter
always verifies the personal PIN. Wrong PINs and unknown/unauthorized extensions
end the call. Callback URLs contain only the public extension, never PIN digits
or a bearer grant for skipping PIN verification. Old post-PIN selection callbacks
are rejected. Twilio still validates protected Function requests.
Outgoing calls retain their direct requested-session route after PIN verification.
Existing keypad recovery
commands work after the session connects. This does not transfer an active call
between sessions: redial to select another one. An occupied session may return
busy; separate ready sessions accept concurrent calls.

DTMF collection uses Twilio's [`Gather`](https://www.twilio.com/docs/voice/twiml/gather)
with `finishOnKey="#"` and `numDigits` equal to the caller's PIN length. Thus a
short extension can terminate with #, while a normal PIN still submits at its
usual length. Silence or # alone does not bypass authentication.
[Apple documents](https://support.apple.com/en-mt/guide/iphone/iph9b90d0aba/ios)
commas as two-second pauses and semicolons as waits requiring a tap. Two commas
give the initial collection time to start; handset/carrier timing still needs a
real call check and may require adjusting the pause.

## Recipient mailboxes and extensions validation: September 20, 2026

205 Python tests ran (204 passed; one optional integration skipped), and all
54 JavaScript tests passed. Coverage includes recipient isolation, shared
project mailboxes across sessions, separate projects for one person, legacy
migration, queued-request audience checks, distinct extension routes, default
routing, unauthorized/expired/tampered tickets, and multiple sessions per
external project with a single dispatcher.

Two real native voice acceptance cases passed using TTS microphone input and
independent local STT of output: mailbox action/pending-decision handling, and
concurrent sessions with different recipients and distinct prepared topics.
The latter rejects the other person's topic in each audio response. Native
sessions, tools, preparation, mailboxes, and virtual audio were real; phone/SIP
signaling was simulated and no handsets were called. See the
acceptance report (private historical artifact `state/e2e/recipient-project-mailboxes-20260920/report.json`).

The protected Twilio routes and access configuration were deployed and verified.
Both production session IDs were preserved and both voice frontends reconnected.
Legacy messages retained their IDs, statuses, and resolution notes. Extension
DTMF over an actual handset remains a joint acceptance check.

### Saved-contact dialing revision

The post-PIN extension menu was an initial September 20 implementation
and has been superseded by the current pre-PIN saved-contact flow. All 55
JavaScript tests pass, including immediate default routing with no Gather/Pause,
pre-PIN extension selection, caller-specific PIN lengths, wrong-PIN rejection,
permission rechecks, and rejection of obsolete post-PIN callbacks. All 26 focused
Python configuration, external-project, and provider-boundary tests pass.
No native voice or mailbox behavior changed in this revision. Automatic contact
pauses and DTMF timing remain subject to a real handset check.
