# Reproducing validation from a clean installation

Tests have four distinct boundaries. A successful fake provider call is never
reported as handset acceptance. All commands below use this checkout's code and
generate fresh evidence; private recordings and original native rollouts are not
distributed. Historical observed passes **and failures** remain in
[native results](docs/VOICE-ACCEPTANCE-RESULTS.md) and
[handset results](docs/MANUAL-ACCEPTANCE-2026-09-19.md).
Current container evidence and remaining gaps are summarized in
[release validation](docs/RELEASE-VALIDATION.md).

## 1. Clean container: deterministic contracts and local media

Prerequisites: Linux Docker Engine, sufficient disk for the image and the
checksum-verified `small.en` model, internet during build. No credentials, host
Codex socket, host audio devices, accounts or phones are required.

```bash
docker build --target test -t voice-operator:test .
docker network create --internal voice-test-isolated
docker run --name voice-tests --network voice-test-isolated voice-operator:test
docker cp voice-tests:/opt/voip/state ./test-evidence
docker rm voice-tests
docker network rm voice-test-isolated
```

The internal Docker network provides a local non-loopback interface for ICE/SIP
while preventing external connectivity. `--network none` leaves only loopback,
which the ICE/SIP implementations exclude, so it cannot run the media tests.
The container entrypoint starts private PipeWire, pipewire-pulse and WirePlumber
under D-Bus. `scripts/test-clean-install.py` runs all steps, retains per-step
logs and `state/acceptance/TIMESTAMP/results.json`, and exits nonzero on any
failure. Do not delete a failed container before copying evidence. Files include
synthetic test PINs and synthetic conversation content, never real credentials.

| Check | Executable | Assertions and limits |
| --- | --- | --- |
| CLI/config/bootstrap | `tests/test_release_interfaces.py` | Fresh init without services/PINs, no overwrites, strict fields, permissions, literal private credentials, stable JSON and idempotent mailbox writes |
| Supervision and identity | `tests/test_voip.py`, `test_operator_reset.py`, `test_daemon_transport.py` | Isolated tmux/fake Codex, real local Unix WebSocket framing, identity/ownership, unknown creation recovery, interrupted reset recovery |
| Audio transport | `tests/test_voice_transport.py` | Real local WebRTC and PipeWire devices; fake signaling, echoed audio; no model or carrier |
| Separate audio paths | `scripts/check-audio.py` | Signal on intended input/output path; cross-routing below threshold; no speech assertion |
| SIP keepalive | `scripts/check-sip-keepalive.py` | Real patched Baresip against a local TLS peer; registration stays healthy with CRLF keepalives; no carrier |
| Mailbox scope | `test_mailbox.py`, `test_recipient_mailboxes.py`, `test_external_projects.py` | Person/project ownership, project sharing, migration, address/reopen semantics, preserved outcomes |
| Requests/preparation | `test_requests_queue.py`, `test_request_runtime.py`, `test_call_preparation.py` | Durable deduplication, recipient admission, priority, prompt receipts, cancellation and uncertain outcomes |
| Schedule/retry | `test_call_schedule.py`, `test_retry_policy.py` | Cron/timezones/DST, skipped counts, crash recovery, arbitrary retry delays after failures and cap/TTL behavior |
| Provider and access | `test_outgoing_calls.py`, `test_twilio_setup.py`, `twilio/*.test.js` | Signed tickets, recipient PIN/extension routing, number isolation, crypto interoperability; mocked provider |
| Directory extensions | `test_directory_routes.py`, `test_directory_service.py`, `twilio/directories.test.js` | Name inference, tree numbering, manual reservations, separate native workspace, scoped routes, expiration and PIN-entry map changes |
| PIN lifecycle | `test_pin_security.py`, `test_pin_delivery.py`, `twilio/pin-delivery.test.js` | Full-code confirmation, `9#` opt-out, encrypted prompt deletion, replay, offline receipt and ambiguous dial behavior |
| Spoken PIN IVR | `scripts/test-pin-audio.py` | Actual handler with in-memory Sync, synthetic TTS through isolated audio, independent STT of digits/confirmation/opt-out; no Twilio TTS |
| Call/keypad notices | `test_call_events.py`, `test_keypad_control.py` | Identified transitions, recipient scope, duplicate notices, independent command recognition/cooldown |
| Inspection/acceptance harness | `test_session_inspector.py`, `test_voice_acceptance.py` | Transcript/timing separation, safe rendering, capture and verification behavior |

Run one test inside the same image with `docker run --rm voice-operator:test
python3 -m unittest discover -s tests -p test_release_interfaces.py -v`.
The optional `test_tmux_resume_exclusion.py` is explicitly skipped unless the
third-party tmux-resume tool and compatible patch are installed. It is not a
dependency of the container, which owns its tmux instance. An existing compatible
source file can be mounted read-only and selected with `TMUX_RESUME_SOURCE`:

```bash
docker run --rm --network none \
  -v /absolute/path/tmux_resume.py:/tmp/tmux_resume.py:ro \
  -e TMUX_RESUME_SOURCE=/tmp/tmux_resume.py voice-operator:test \
  python3 -m unittest discover -s tests -p test_tmux_resume_exclusion.py -v
```

This external tool is not distributed here; record its SHA-256 with the result. To reproduce that
historical integration, install the matching tool in a disposable user home,
apply `docs/tmux-resume-exclusion.patch`, and rerun that module; never patch the
user's active workspace-restoration tool just to obtain a green test.

## 2. Native Codex: audio, tools, prompts and agent behavior

Prerequisites: clean **test** image; host Codex login with native voice available;
host daemon running; matching UID and same-path workspace mount per
[containers](docs/CONTAINERS.md). Calls are simulated and no real phone numbers or
Twilio credentials are needed. Model usage is real. Use a separate workspace and
container, never a production session. The host operator's configured `cli_command`
must route back into the test container, since its tools run on the host.

From a fresh source checkout on the host, with `voice-operator:test` built above:

```bash
export VOICE_TEST_SOURCE="$(pwd -P)"
export VOICE_TEST_WORKSPACE="$(mktemp -d "$HOME/voice-native-test.XXXXXX")"
export VOICE_TEST_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export VOICE_TEST_SOCKET_DIR="$VOICE_TEST_CODEX_HOME/app-server-control"
codex app-server daemon start
docker run -d --name voice-native-test --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/voice-home \
  -e VOIP_ROOT="$VOICE_TEST_WORKSPACE/base" \
  -e VOIP_TOOLS_DIR=/opt/voip/.tools \
  -e CODEX_HOME="$VOICE_TEST_CODEX_HOME" \
  -v "$VOICE_TEST_WORKSPACE:$VOICE_TEST_WORKSPACE" \
  -v "$VOICE_TEST_SOCKET_DIR:/codex-control:ro" \
  -v "$VOICE_TEST_CODEX_HOME:$VOICE_TEST_CODEX_HOME:ro" \
  -w /opt/voip voice-operator:test sleep infinity
docker exec voice-native-test voip init --number +12025550100 \
  --recipient alice --destination +12025550101 \
  --daemon-socket /codex-control/app-server-control.sock
python3 - <<'PYTHON'
import json, os
from pathlib import Path
p = Path(os.environ['VOICE_TEST_WORKSPACE']) / 'base/server.toml'
command = [os.environ['VOICE_TEST_SOURCE'] + '/bin/voip-container', '--container', 'voice-native-test']
metadata = os.environ['VOICE_TEST_CODEX_HOME'] + '/state_5.sqlite'
p.write_text(p.read_text().replace('[operator]', '[operator]\ncli_command = ' + json.dumps(command)
    + '\nstate_database = ' + json.dumps(metadata)))
PYTHON
docker exec voice-native-test python3 scripts/test-features.py  # list only
docker exec voice-native-test python3 scripts/test-features.py --run \
  --permissions danger-full-access --timeout 180 --output "$VOICE_TEST_WORKSPACE/features"
docker exec voice-native-test python3 scripts/test-voice-lifecycle.py --run \
  --permissions danger-full-access --output "$VOICE_TEST_WORKSPACE/lifecycle"
docker exec voice-native-test python3 scripts/test-transfer-delegation.py --run \
  --permissions danger-full-access --output "$VOICE_TEST_WORKSPACE/transfer"
# After completion the owned frontends stop; evidence stays in the private workspace.
docker stop voice-native-test
docker rm voice-native-test
```

The test workspace is mounted at its absolute host path, separate from production
source and state. Image code and dependencies come from the tested build; the host
checkout supplies only the `bin/voip-container` wrapper used by host Codex tools.
Use an image built from the same revision. No Twilio credentials or real recipient
configuration enter this test container. Select individual cases with repeated
`--case NAME` flags and a fresh output directory.

The explicit test permission flag is necessary for host `docker exec` commands.
Without it fixtures default to `workspace-write`, whose sandbox denies Docker
socket access; a spoken promise to review a mailbox is not a successful review.
Unrestricted test threads still receive instructions limiting them to synthetic
fixtures, but those instructions are not a security boundary. Only use this option
on a host/account where that access is intended.

Full reset cases require the read-only Codex home mount and `state_database`
setting above. Read the privacy and compatibility boundaries in
[container resets](docs/CONTAINERS.md#resets-and-historical-inspection): this bind
exposes native history and can expose existing account credential files to the
container. Nothing from it is baked into the image. For socket-only smoke tests,
omit the home mount/metadata setting and do not select session/full reset cases.

| Case | Reproduction assertion |
| --- | --- |
| `smoke` | Entire request arrives as TTS; a fresh numeric marker must be written by the real shell tool, and the spoken confirmation must pass independent STT |
| `directory_workspace` | TTS requests shell `pwd` without `cd`; its saved output must match the selected directory, separate from voice metadata, and the spoken confirmation must pass independent STT |
| `profiles` | Two concurrent native profiles speak distinct startup identities; no cross-profile phrase |
| `mailbox` | Configured incoming greeting; message read/action/outcome, pending decision stays open |
| `prepared` | Operator loads agenda before fake dial; prepared phrase spoken after identified connection |
| `hangup` | Delivered/acknowledged message followed by ended notice; actual CLI review and outcome/disconnection marker required |
| `requests` | Ordinary context waits during input activity; priority context arrives; same recipient reuses call, other recipient waits |
| `backend_requests` | Real waiting tool remains active; priority context steers it; ordinary context waits; spoken status and completed tool result both checked |
| `schedules` | Real local scheduler occurrence, stable deduplication and same-call topic reuse with spoken reference |
| `missed` | Offline skips count once, do not dial, appear only for the matching recipient/session and only when nonzero |
| `redial` | Failure-relative delays/cap, prepared retry, cancellation/expiry prevent unwanted fake calls |
| `concurrency` | Different native sessions call the same synthetic number and speak independent topics |
| `different_recipients` | Distinct synthetic numbers, separate conversations and topics |
| `frontend_reset` | Same UUID and restored audio; spoken-only recall checked separately |
| `session_reset` | Same conversation reload, restored audio and separate spoken-only recall assertion |
| `outgoing_reset` | Fresh conversation before outgoing attempt; no repeated reset on the same attempt or connected-call reuse |

Each case creates isolated project state, devices, threads and tmux sockets.
`report.json` records source hashes, pass/fail/not-run, cleanup errors and the
simulated-provider boundary. Audio folders contain input text/WAV, captured WAV,
independent STT and state assertions. Diagnostics contain private native activity.
Cleanup stops owned frontends/audio and preserves evidence; saved native threads
remain in the account for inspection. Do not delete/reset other threads.

If interrupted, inspect the case's project state and exact owned tmux socket;
stop that frontend, unload only audio modules bearing that fixture's unique
prefix, and record cleanup failure. Do not blindly kill all tmux sessions.

The earlier single-session smoke can also be reproduced with
`scripts/test-voice.py prepare`, `check-input`, then `run`, against a dedicated
initialized test profile. `scripts/test-voice.py --help` lists root/session and
timeout controls. A microphone-path pass alone is not a Codex/tool pass.

## 3. Provider and handset acceptance

Prerequisites: deployed test installation, billable Twilio account/number,
explicitly arranged recipient availability, private PIN delivery, and a second
authorized phone for isolation/concurrency. See the step-by-step
[manual and agent runbook](docs/MANUAL-TESTS.md). These cases cannot run without
external accounts and people merely by starting a container.

`scripts/check-twilio-sip.py` places a short, billable call to the configured SIP
operator (no PSTN destination); arrange this signaling test before running it.
`check-sip-keepalive.py`
reproduces the local TLS keepalive protocol regression. `check-phone-audio.py`,
`check-keypad-call.py`, and `check-mailbox-calls.py` require their documented
telephony opt-in and arranged calls. Inspect `--help` before execution.

## 4. Agent-guided diagnosis and timing

For a tool-delay investigation, use the same fresh TTS request on each network
and on text, asking for a short harmless shell operation and a distinctive reply.
Save the request, source/build versions, native thread/turn/item identities and
`session_inspector.py --root TEST_PROJECT` output. Inspect real tool start/end and
backend turns; independently capture and transcribe the speaker output. Repeat
without claiming a reliability rate from a few samples.

Measure separately: tool execution/recording, backend turn duration, result
handoff/acceptance, local PCM playback and saved speech. Saved transcript time is
not audible latency. Record handset observations separately from local audio.
Use [the latency report](docs/VOICE-LATENCY-REPORT.md) for the original hypotheses
and evidence qualifications; do not copy private session logs into a public issue.

## Known limitations

Native voice tests have historically failed automatic hangup review, some
opening/topic delivery, and spoken-only recall after recovery. A green unit test,
accepted notice or preserved UUID does not overrule those failures. Report them
as failures with current evidence. Native voice access and protocol support are
account/build dependent. Real carrier NAT, call-waiting and keypad behavior need
the handset layer. Host-tool access through a container is not host isolation.
