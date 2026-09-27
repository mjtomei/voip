# Reference installation and prompts

`examples/reference/` is a provider configuration derived from the working
installation. `examples/project/` preserves its external-project daily meeting
protocol, including all five configurable prompt phases and the recurring agenda.
People, project identifiers, endpoints and paths have been replaced. The example
backlog is fictional. The meeting remains daily at 09:00 America/Chicago,
including weekends, with retries five then ten minutes after failures.

Copy provider files to the private provider workspace and project files to its
own directory. Replace these values before configuring live calls:

| Reference | Your installation |
| --- | --- |
| `/home/operator/voip` | Absolute provider workspace |
| `/home/operator/mobile-project` | Absolute external project directory, mounted at the same container path |
| `/home/operator` | Root of active-directory discovery and default home operator |
| `alice`, `+12025550101` | Owner recipient ID and allowed caller number |
| `bob`, `+12025550102` | Collaborator recipient ID and destination number |
| `+12025550100` | Existing Twilio voice number |
| `YOUR-DOMAIN.sip.twilio.com` | Dedicated SIP domain provisioned for the installation |
| `developer@laptop.example`, `/path/to/ios-app` | Optional SSH destination and remote checkout |
| `daily`, extension `200` | Registered project session and optional manual extension |

The reference's general operator and directory discovery belong to the owner.
The external project allows both people, with a separate mailbox for each. Its
daily session belongs to the project directory and can be reached by extension.
The owner can also reach a separate generated operator in that project when
discovered through tmux; their project mailbox is shared between those sessions.
Incoming calls can be ordinary project work rather than scheduled standups.

Create each person's PIN using the private CLI; no verifiers or reusable PINs
are supplied. The provider owner's allowed-session list includes the project session,
so both routes use that same person's verifier without a duplicate project entry. Configure Twilio keys in
`state/twilio.env` with mode 0600, or supply environment variables. The service
does not source shell profiles. Use normal configuration validation and provider
deployment, then enable the schedule only after arranging delivery and calls.

All published permission defaults are `read-only`. The adjacent setting lists
`workspace-write` and `danger-full-access` as explicit choices. The original
private installation uses `danger-full-access` for host tools. To reproduce
file-writing meeting notes and unrestricted worker management, choose a suitable
broader permission mode in private configuration. A container connected to the
host daemon does not isolate these host-account tools.

The project template expects `todo.txt`, `PLAN.md`, `context.md` and `meetings/`.
Create those with current project information; no historical project notes are
distributed. Preserve user-edited files. The agenda asks what finished yesterday,
whether the schedule needs adjustment, and what concrete work is agreed today.
A hangup triggers notes and mailbox review, not a callback or cancellation.

The included `bin/voice` selects the project directory and `daily` session. Install
the host `voip` command first (native CLI or the container wrapper), or set
`VOICE_CLI` to its absolute executable path. If you rename the session, update
that wrapper and the matching prompt/configuration references. For a container
deployment, use the permission and host helper instructions in
[CONTAINERS.md](CONTAINERS.md); host Docker CLI access needs an explicit
`danger-full-access` choice.

`prompts/operator.md` retains the detailed operating rules developed in use:
worker inspection, precise call identities, mailbox completion, prepared calls,
stable request keys, priority, skipped occurrences, PIN boundaries and transfers.
The project prompts add workflow context rather than replacing those rules.
Built-in connection/preparation/disconnection notices supply current identities,
CLI commands and prepared openings at runtime.
