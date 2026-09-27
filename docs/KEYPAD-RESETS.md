# Keypad recovery during a call

After the access code has connected your call, enter one of these commands:

| Keys | Effect |
| --- | --- |
| `*1#` | Reconnect the voice frontend to the current Codex session. |
| `*2#` | Restart the frontend and reload the current Codex session, keeping its conversation history. |
| `*3#` | Restart with a new Codex conversation. The previous conversation is saved and archived. |

Here `#` finishes the command. This is separate from access-code entry, which
still submits automatically. Leave no more than eight seconds between keys.
Wait for the spoken completion message before speaking again. The service
ignores additional commands during a reset and for 15 seconds after accepting
one. Partial commands expire and cannot carry across calls or observer outages.

The phone call stays connected while voice restarts. A local voice announces
the reset and, when the frontend reconnects, asks you to repeat your last request.
Speech during reconnection may be lost. The announcement confirms transport
connection, not successful model reasoning or tool execution. If recovery fails,
the local voice says it could not finish. Audio failure can also prevent these
announcements, but does not prevent the reset command from running.

`*2#` and `*3#` can interrupt the operator's current task. Ordinary Codex and
Claude workers in other tmux sessions keep running. Files, the mailbox, and
scheduled work outside the operator are not cleared. A new conversation still
has the project's instructions and normal workspace access. Calls continue
using whichever conversation is selected until another explicit fresh reset.
A hangup never creates a new conversation and does not cancel an accepted reset.

Keeping the conversation UUID and saved history does not guarantee that the
reloaded voice model recalls every speech-only exchange. Native testing observed
that limitation after `*2#`. The saved realtime transcript can still be inspected
through host tools; put durable decisions and work in project files or the
mailbox. The hangup review uses the identified call's transcript when an
acknowledgment is missing from the backend's working context. See the current
[release validation](RELEASE-VALIDATION.md) for the measured boundary.

## Local commands and implementation

The same actions can be run from a terminal:

```bash
python3 voip.py operator reset --scope frontend
python3 voip.py operator reset --scope session
python3 voip.py operator reset --scope fresh
python3 voip.py operator status
```

`keypad_control.py` handles Baresip's `CALL_DTMF_START` / `CALL_DTMF_END` events
inside the call-event monitor, independently of Codex. The container's foreground
supervisor owns that monitor; host service installations use
`voip-call-events.service`. Commands require an
established, currently observed call on the configured SIP account. Twilio's
existing allowlist and PIN flow still gate ordinary incoming/outgoing calls.
There is no new public listener, Twilio Function, or OpenAI API credential.
The monitor continues processing hangups and call snapshots during recovery.

Raw keypad digits stay in memory and are not copied to the mailbox, Codex,
or reset records. `state/keypad-last-command.json` records only the selected
action, hashed call identity, and acceptance time. Feedback is synthesized
locally into `voip_out`, never into Codex's microphone.

`operator_reset.py` serializes resets with the existing supervisor. It checks
the frontend's process identity, command, and tmux ownership before stopping it.
An unresponsive frontend is forcibly stopped after a grace period, including
its private audio/RPC helpers; the shared Codex daemon is never restarted.

The installed Codex 0.154.0 supports resetting the loaded session through native
archive/unarchive/resume operations. `*2#` uses those operations with the same
UUID. `*3#` archives the old session, creates and persists a new one, then commits
its UUID to `state/operator-thread-id`. Both back up the old rollout first under
`state/operator-reset-backups/`, with private permissions. The latest operation
and both identities are recorded in `state/operator-reset.json`.

Native archive also affects Codex-spawned descendants. Full/fresh reset therefore
refuses an operator with recorded child-agent relationships; frontend-only
reset remains available. This does not include ordinary independently started
tmux workers. Full/fresh reset also requires the default local Codex state store;
a custom daemon socket is not assumed to share it.

## Interrupted recovery

The normal operator timer recognizes an interrupted native reset. After allowing
an outstanding RPC time to settle, it unarchives/resumes the original session,
unless the new identity was already committed. It never repeats `thread/start`
automatically. The transient archive is not an instruction to erase history.

If creation has an unknown outcome, `state/operator-reset-create-pending.json`
blocks further fresh creations. Inspect Codex's thread list, this record, and
the reset journal to identify any created thread before removing that pending
marker. Do not delete it merely to retry. The old conversation can be restored
without choosing or creating another thread. Files and mailbox state are retained.

If supervision was deliberately paused, unpause it before resetting. Keypad
commands cannot help when the SIP call itself is disconnected or the host is
offline; use the normal remote shell and service recovery in that case.

## Validation

Automated checks cover all three sequences, configured/established-call gating,
duplicate events, timeout and call isolation, cooldown after monitor restart,
and event delivery over a real local control socket while a reset is running.
Controller tests cover history preservation, fresh creation, active-turn
interruption, child-agent protection, and ambiguous archive/create recovery.
The fresh-reset controller also successfully replaced the live operator at
the primary user's request on 2026-09-15 and reconnected voice. A subsequent real Twilio
call sent `*2#` to our SIP endpoint: the monitor recognized it, reloaded the
operator, kept the UUID/history, replaced the frontend, and reconnected voice
while the same call stayed up. No handset was dialed in that historical test.
The September 26 container handset checks exercised all three keypad commands
with the same phone call retained; current results and capture limitations are
in [release validation](RELEASE-VALIDATION.md).

To repeat the SIP-only test with an idle operator and no phone calls active:

```bash
python3 scripts/check-keypad-call.py --run-telephony --scope session
```

Load the existing Twilio environment privately first. This test uses Twilio's
[DTMF playback](https://www.twilio.com/docs/voice/twiml/play#digits), incurs the
ordinary SIP call charge, and performs a real keep-history reset.

Protocol references: [Baresip DTMF events](https://github.com/baresip/baresip/blob/v4.11.0/src/ua.c),
[Baresip control events](https://github.com/baresip/baresip/blob/v4.11.0/modules/ctrl_tcp/ctrl_tcp.c).
