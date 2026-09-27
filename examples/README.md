# Configuration examples

Templates sit beside their working filenames: copy `server.toml.example` to
`server.toml`, for example, then edit the private copy. Keep the `.example` files
free of real installation settings; they are tracked by Git. When using a separate
private workspace, copy the same relative layout there. The complete project
walkthroughs remain under `examples/reference/` and `examples/project/`.
`voip init` creates the basic server, recipient, default-session and schedule files;
use these examples to customize them. Schedules in the examples are disabled.

| Private file | Committed example |
| --- | --- |
| `server.toml` for a host installation | [server.toml.example](../server.toml.example) |
| `server.toml` for a container using host Codex | [server.container.toml.example](../server.container.toml.example) |
| Legacy `config.toml` fallback | [config.example.toml](../config.example.toml); new installations should use `server.toml` |
| `recipients.toml` | [recipients.toml.example](../recipients.toml.example) |
| `sessions/default.toml` | [sessions/default.toml.example](../sessions/default.toml.example) |
| Additional session profiles and prompts | [sessions/daily.toml.example](../sessions/daily.toml.example), [prompts/daily-initial.txt.example](../prompts/daily-initial.txt.example) |
| `schedules.toml` and its agenda | [schedules.toml.example](../schedules.toml.example), [daily-agenda.txt.example](../daily-agenda.txt.example) |
| `state/twilio.env` credentials file | [state/twilio.env.example](../state/twilio.env.example) |
| Compose `.env` beside the checkout's `compose.yaml` | [.env.example](../.env.example) |
| Optional `compose.local.yaml` beside `compose.yaml` | [compose.local.yaml.example](../compose.local.yaml.example) |
| Provider configuration with an external project | [reference/](reference/) |
| Project-owned server, recipients, sessions, schedules and all five prompt phases | [project/](project/) |

The basic recipient example references both `default` and `daily`; copy both
profiles and the daily prompt when using it. Copy the agenda alongside
`schedules.toml`. The complete provider/project pair has its own relative paths;
follow [the reference walkthrough](../docs/REFERENCE-SETUP.md) when copying it.

For file-based credentials, copy `state/twilio.env.example` to `state/twilio.env` in the
private workspace, set mode `0600`, replace the placeholders, and set
`[service].credentials_file = "state/twilio.env"` in `server.toml`. Entries are
literal `KEY=value`: do not add `export`, shell quotes or variable substitutions.
Process environment values take precedence. Compose's `.env` holds deployment
paths, not Twilio credentials.

If a working file already exists, merge settings into it rather than overwriting
it. Both `.gitignore` and `.dockerignore` allow only the named templates under
`state/` and `sessions/`; their real credentials, profiles and runtime contents
remain excluded. The root configuration copies and local Compose overlay are
excluded too. On a clean checkout, copying a template preserves it for reference;
renaming it also works but appears as a tracked-file deletion in Git.

The container server template uses literal fictional paths. TOML does not expand
Compose variables: replace its paths to match `.env` and the local overlay.
The overlay adds native history access for full conversation resets and shows
an optional external-project mount. It is loaded explicitly with
`docker compose -f compose.yaml -f compose.local.yaml ...`; follow
[container setup](../docs/CONTAINERS.md) before starting the service.

## Generated files

These are outputs, not additional settings to copy from a running installation:

| Generated files | Source or command |
| --- | --- |
| `baresip/config`, `baresip/accounts` and per-session equivalents | `voip sip configure` renders audio, SIP and control settings from the server/profile configuration and privately prompts for the SIP password; provider deployment also generates these files. The renderer is `sip_configure` in [voip.py](../voip.py). |
| Per-component units under `systemd/generated/` | `voip render-systemd` renders units without enabling them. Use these only for the native per-component deployment, not alongside the container supervisor. |
| Foreground-stack user unit | `voip service enable` generates and enables it for a native supervised installation; containers use Compose restart policy instead. |
| Host daemon timer and tmux discovery unit | [install-host-helpers.py](../scripts/install-host-helpers.py) renders these from the workspace settings. Omit `--enable` to write units without starting them; `--unit-dir` selects a review directory. |
| Virtual audio devices | Created from `[audio]` by the audio CLI/container entrypoint; no separate hand-written audio configuration is required. |

PIN verifiers, encryption keys, pending deliveries, provisioned SIP secrets,
mailboxes, call/queue history, recordings and native conversation references are
private runtime state. Generate them through the CLI; no reusable security
material or live state is supplied as a template.

## Reproducing the reference workflows

| Workflow | Configuration and instructions |
| --- | --- |
| Persistent phone operator; separate recipients and PINs | Root server, recipient and session templates; [PIN generation/delivery](../docs/PIN-DELIVERY.md) explains private-terminal delivery, confirmation calls and `9#` opt-out. |
| Home operator and automatic directory extensions | `[directory_extensions]` in the server templates; [directory routing](../docs/DIRECTORY-EXTENSIONS.md). Container deployments also need the host snapshot helper. |
| Manual project extensions and separate project conversations | `examples/reference/` plus `examples/project/` demonstrate extensions `100` and `200`, allowed recipients and separate mailboxes. |
| External-project daily meeting at 9 a.m. Central | The project schedule uses `America/Chicago`, including weekends, and links the agenda and all five prompt files. Enable it only after configuring recipients and arranging calls. |
| Prepared outgoing calls, messages and busy-session behavior | [Outgoing calls](../docs/OUTGOING-CALLS.md) and [requests/schedules](../docs/REQUESTS-AND-SCHEDULES.md) show mailbox posting, stable request keys, priority and same-call reuse. These are CLI requests, not extra configuration files. |
| Custom retry delays and per-attempt fresh conversations | Session and schedule `[redial]` settings; the project reference uses explicit delay arrays. `reset_on_outgoing_call` selects conversation reuse versus fresh attempts. |
| Callbacks, transfers, arrival announcements and missed calls | [Call routing](../docs/CALL-ROUTING.md); uses recipient/session access settings and current call identities, with no separate routing configuration file. |
| Keypad recovery and startup in a container | Container server, `.env` and Compose-overlay templates; [container setup](../docs/CONTAINERS.md) covers metadata mounts, host recovery and restart policy. |

[The reference walkthrough](../docs/REFERENCE-SETUP.md) explains how the provider
and external project fit together. [Reproducible tests](../TESTING.md) separate
credential-free checks, native voice and arranged handset tests.
