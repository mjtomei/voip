# Voice and SIP health

Inspect one operator without placing a call or restarting anything:

```bash
python3 voip.py --session daily operator health
```

The JSON reports separate voice and SIP results. These checks measure transport
health. They do not establish that reasoning/tool execution is responsive, that
a person heard speech, or that a future telephone call will succeed.

## Voice recovery

The operator supervisor checks the frontend's process identity, saved thread,
connection state, and its event-loop heartbeat. A live frontend that fails to
connect within 120 seconds, or stops updating its heartbeat for 30 seconds after
connection, is eligible for recovery. A newly connected frontend gets a 30-second
heartbeat grace period. Silence is normal: microphone/speaker activity is not a
health requirement. Existing heartbeat files without PID fields remain compatible;
they must identify the same thread and be newer than this frontend's connection.

Recovery uses the same ownership checks and locks as keypad recovery, stops only
that frontend and its private helpers, and resumes the saved conversation. It
does not archive/create a thread, interrupt backend turns, restart the shared
Codex daemon, or terminate ordinary worker agents. SIP stays running, including
an existing phone call. Recovery respects operator pause and restart throttling.
`state/voice-health-recovery.json` records the reason and retained thread ID.

The host operator timers check every five seconds with one-second timer accuracy.
Generated directory operators use the directory supervisor's configured refresh
interval (15 seconds by default). The foreground/container supervisor also checks
operators every five seconds. Restart throttling and connection establishment
still add time; these are not guaranteed audible recovery times.

## SIP registration

The existing call observer owns Baresip's single control connection. Health
checks share that connection so they cannot displace keypad or hangup monitoring.
Every snapshot reads registration status and verifies that the endpoint contains
exactly the configured SIP account, then checks all calls, including ringing calls.

A cached local `OK` is insufficient. When idle, the observer requests a fresh
registration every 60 seconds using the patched `uarefresh now 0` command. It waits for a
matching `REGISTER_OK` event from the registrar; merely accepting the local
command does not count. This also repairs registrations the registrar has
forgotten while Baresip still reports `OK`. TLS verification and the existing
credentials remain in use. The pinned build adds this command to reuse the existing
registration dialog and Contact, instead of unregistering/recreating them through
`uareg`. An in-flight request cannot be overwritten. Rebuild Baresip before
enabling the updated observer. No phone or SIP test call is originated.

A failure marks registration unhealthy. An unanswered renewal times out after
20 seconds, and attempts are separated by at least 15 seconds. The ordinary
Baresip reconnect/registration logic continues running too. Maintenance is
deferred while any call is present and resumes when the endpoint becomes idle.
Unknown/mismatched account state never authorizes a maintenance command.

`state/sip-health.json` stores only account identity, timestamps, counts and
sanitized result labels. Readiness requires a current observer snapshot (within
10 seconds), a local registered state, and a matching registrar acknowledgement
within 150 seconds. An old acknowledgement during a long call is reported as a
deferred check; the call is not terminated. Generated incoming routes and new
outgoing calls require both voice and SIP health.

These acknowledgements confirm registration at a point in time. A provider can
lose it between checks; the next idle renewal repairs it. Full carrier/media
verification remains an explicitly arranged telephone test. For example, Twilio
[error 32009](https://www.twilio.com/docs/api/errors/32009) indicates that the
requested endpoint was not registered when the call was attempted.

## Reproducible validation

Run the ordinary clean-container test recipe from the release repository's
`TESTING.md`. The Python suite includes:

- `test_endpoint_health.py`: stale startup/heartbeats, process/thread identity,
  silence, pause, cached registration versus actual ACK, retry timing, foreign
  accounts, observer outages, and maintenance deferred during ringing/active calls.
- `test_voip.py`: an owned, live-but-stalled frontend is replaced while retaining
  the saved conversation. Existing ownership/refusal cases remain in the suite.
- `test_keypad_control.py`: keypad and hangup events remain live on the same
  control socket while health snapshots and reset handling run.
- `test_sip_health_transport.py`: installed Baresip talks to a local TLS registrar
  with a synthetic account and the real observer. The registrar forgets a binding,
  then rejects renewal temporarily; registration recovers in both cases without
  restarting Baresip. The test asserts that only REGISTER traffic was sent, the
  same dialog was retained, and no unregister request occurred during maintenance.

The local registrar test requires the pinned Baresip build and OpenSSL. It needs
no Twilio/OpenAI credentials, native Codex daemon, production audio devices, or
telephone numbers. A private container network is sufficient.

See [on-demand voice lifecycle](ON-DEMAND-VOICE.md): healthy idle operators now remain in standby without a realtime connection.
