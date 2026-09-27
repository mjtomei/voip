# On-demand voice connections

The saved Codex conversation, local controller, and SIP registration persist.
The metered realtime/audio connection opens only for an established phone call
or an explicitly bounded outgoing/test request. Silence alone does not close an
active phone conversation.

## Lifecycle

- Startup enters `standby`. No realtime start, microphone capture, or speaker
  playback process is opened. Local heartbeat/SIP checks do not submit model turns.
- An authenticated incoming SIP call wakes its own controller. The phone can be
  connected before voice finishes starting; cold startup adds a short silent gap.
- Outgoing preparation and ordinary off-call mailbox notifications use text.
  Once the agenda is prepared, preloading opens voice before dialing. This demand
  expires after 120 seconds, even if a process crashes or an attempt is ambiguous.
- A connected call takes over outgoing demand. Hangup closes audio promptly;
  unused pre-dial time cannot keep it open. Provider-confirmed failure/cancellation
  also releases demand. If a caller completes their PIN after the pre-dial demand
  expires, their SIP connection wakes voice again and its connection notice still
  carries the prepared opening.
- Hangup review goes to the saved conversation via `turn/start` or `turn/steer`,
  with no realtime connection. Existing work is not interrupted. If submission
  has an ambiguous outcome, its delivery is marked `uncertain` for inspection,
  rather than automatically submitting another turn.
- Failed negotiation retries after 30 seconds only while demand remains. An
  observer stale for more than 10 seconds cannot keep audio open indefinitely.
  Restored call observation can wake voice again. Explicit pause prevents startup.
- Keypad recovery retains its behavior during calls. Off-call resets may finish
  in standby, without opening audio. Conversation identity changes only for the
  already-supported explicit/configured fresh resets.

Readiness accepts a healthy standby controller plus fresh SIP registration. A
standby endpoint remains in the extension map; it does not need billable audio
running to accept a call. Backend tasks can continue after audio disconnects.

## Inspection and tests

`python3 voip.py --session daily operator health` reports `voice_standby_ready`
while idle, and `voice_transport_current` while audio is connected.
`voice.json`, `voice-activity.json`, and private `voice-demand.json` distinguish
controller liveness from realtime usage. Demand entries bind to the saved thread
and expire; they are not durable permission to open voice after a reset.

Explicit audio probes use a bounded test demand and release it in `finally`.
`scripts/connect-voice.py` starts standby; `--test` additionally runs the bounded
TTS/STT probe. The existing native acceptance fixture can opt into a bounded
600-second test connection; lifecycle acceptance uses `keep_voice=False`.

Run `tests/test_voice_lifecycle.py` for idle/start/hangup/reconnect, expired or
cancelled demand, hangup during negotiation, stale observation, and text review
without duplicated ambiguous submissions. `tests/test_voice_transport.py`
exercises real local WebRTC/Pulse transport with a synthetic negotiation server.
Container tests need no provider or Codex credentials. Native TTS/STT and handset
checks are distinct: a simulated SIP event is not carrier acceptance.

Native lifecycle acceptance (local Codex login, audio, TTS and STT prerequisites
from `TESTING.md`) is reproducible with:

```sh
.tools/voice-venv/bin/python scripts/test-voice-lifecycle.py --run
```

This creates one isolated test conversation, simulates two call connections,
verifies the greetings and TTS replies using independent STT, checks hangup
review through text, and checks the same conversation reconnects. It archives
only its test conversation and removes its own audio devices on exit. Speech
failures remain failures and do not skip subsequent disconnect checks. Inspect
`state/lifecycle/<run>/results.json`, recordings, diagnostics and frontend logs.
No telephone provider is contacted. Native account usage is incurred during
this opt-in test; the credential-free container suite does not open native voice.
