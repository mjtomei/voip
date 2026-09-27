# Consolidation inventory

The release uses one source checkout. The development candidate supplied the
portable CLI, configurable permissions, credential-file loader, foreground
supervisor, Docker setup and reproducible test harness. The running installation
supplied the latest transfer startup/publication fix and its regression tests.
Shared implementations were compared before merging; private runtime state and
the candidate's original Git index were backed up outside the source tree.

| Capability | Retained implementation and verification |
| --- | --- |
| Installation and configuration | `release_cli.py`, `release_config.py`, `bin/voip`, installer, strict TOML, JSON output and private literal credentials |
| Container and host tools | Dockerfile, Compose, audio entrypoint, foreground supervisor, host CLI wrapper, discovery snapshots and reset metadata mounts |
| Session access | Server/session permission settings; public references use read-only; explicit private configuration may grant broader tools |
| Directory routes | Active tmux discovery, name/common-parent inference, tree extensions, home aliases, separate persistent directory operators |
| Mailboxes | Recipient/project scope, shared project mailbox across sessions, explicit outcomes and delayed-call identity |
| Requests and preparation | Durable keys, prepared openings, priority, same-call reuse, cancellation settlement and separate session admission |
| Schedules and retries | Timezones, skipped-occurrence counts, failure-relative delay arrays, caps and PIN-delivery opt-out |
| Authentication | Per-recipient verifiers, encrypted pending PIN delivery, full PIN confirmation, extension routing and independent keypad recovery |
| Lifecycle and health | On-demand realtime, idle standby, bounded warmup leases, SIP registration and transport health recovery |
| Call routing | Three-hour retry-refreshed callback window consumed on success, missed-call context, explicit transfer and hangup |
| Transfer recovery | Published-target admission before redirect, source revalidation, bounded fallback and sanitized hosted diagnostics |
| Recovery | Frontend reconnect, same-conversation reload, fresh conversation, persisted state and supervisor ownership checks |
| Evidence | Isolated local media, native TTS/STT and tool tests, documented arranged handset procedures; see current release validation |

The full reusable operating protocol is in `prompts/operator.md`. The provider
and project references reproduce the operational settings and daily-meeting
workflow with fictional recipients and substitute paths. See
[reference setup](REFERENCE-SETUP.md). No credentials, conversation logs, PIN
material or historical runtime state are part of this inventory's public files.

The combined tree must pass its own validation; earlier results for separate
trees are historical evidence only. Final results and any remaining gaps belong
in [release validation](RELEASE-VALIDATION.md).
