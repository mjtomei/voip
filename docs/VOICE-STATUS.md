# Voice integration status

The installed `codex-cli 0.154.0` has no built-in CLI voice UI. This project now
supplies `voice_frontend.py`: a PipeWire/Pulse audio frontend to its experimental
App Server WebRTC backend. The default `frontend = "voice"` starts that client
in tmux and rejoins the saved Codex thread. `frontend = "text"` selects ordinary
text CLI diagnostics.

The local microphone/WebRTC/speaker path and the live account connection passed.
An entire request delivered as offline TTS through `voip_in` caused Codex to
write a fresh six-digit challenge using its normal shell tool and return audio.
The connection/test command is `python3 scripts/connect-voice.py --test`.

Twilio provisioning completed later on 2026-09-14 using the supplied environment
API key. The number is routed to the protected caller/PIN flow, and Baresip's
enabled user service registered with `200 OK` over TLS. Both hosted Functions
returned HTTP 403 for unsigned test requests. The outgoing SIP dial URI explicitly
requests SRTP with `secure=true`. A fresh TTS shell probe passed with output RMS
595.72; no telephone call had been made at this checkpoint. The original
incoming-only design remains active; the broader service proposal is deferred.

The first two subsequent phone calls passed PIN authorization, but their SIP
legs failed with Twilio 32011 because the client TLS flow had closed. The local
libre build now includes a Twilio-specific CRLF keepalive fallback. Three local
TLS integration cases passed. A direct Twilio-to-SIP call then connected,
negotiated SRTP, and completed after three seconds without Twilio errors. This
establishes signaling connectivity without router port forwarding. A subsequent
handset call connected for 95 seconds with bidirectional RTP at about 64 kbps
per direction. Both legs completed at the same time, and the same TLS flow
survived a 100-second observation. the primary user confirmed hearing Codex respond, completing
the first end-to-end phone test. See
[SIP keepalive recovery](SIP-KEEPALIVE.md).

## Live validation on 2026-09-14

- Existing ChatGPT authentication worked; no additional API key was used.
- The operator has a persistent native conversation (its UUID is kept in private
  state) and is connected
  in the `voice-operator` tmux session with unrestricted user permissions.
- TTS reached native voice, which handed the task to the normal Codex execution
  thread. The thread ran a shell command and verified the challenge file.
- The first passing recording measured RMS 546.45. The backend's assistant transcript
  includes “Voice input and shell access verified.” The test verifies audio
  energy; it does not independently transcribe the recorded waveform.
- The first attempt correctly heard the digits but confused a hyphen in the
  filename with a slash. The test now uses `state/voice/probe/result.txt`.
- Both a terminated frontend and a removed operator tmux session were restored
  automatically by the installed user timer, using the same saved thread.
- Audio and operator timers are enabled; the primary user already has user lingering enabled.
  At that checkpoint, SIP registration and an actual Twilio call remained pending;
  initial registration passed during the later provisioning described above.
- After the host reboot at 12:19 PDT, the timer automatically reconnected the
  same saved thread. The tmux-resume exclusion preserved its owned session
  while the other workspaces were restored. The first reboot had exposed this
  integration conflict; three regression tests now cover the exclusion.
- The complete spoken shell probe passed again after that automatic recovery,
  with a matching fresh challenge and output RMS 650.70.

Three integration fixes were necessary:

1. The daemon's Unix control socket expects WebSocket framing. The stock
   `app-server proxy` relays bytes without supplying the WebSocket handshake.
   `codex_rpc.py` now bridges JSON lines to that local WebSocket.
2. Offering `permessage-deflate` caused this daemon to close the handshake.
   Disabling compression negotiation produced a successful initialization.
3. Resuming a newly created empty paginated thread with metadata alone failed
   with “missing source rollout.” Requesting a one-item initial history page
   persists the empty rollout and allows subsequent resumes, without inserting
   a text prompt or model turn. Creation and resume now request that page.

The transport regression test exercises both production RPC clients against a
real Unix-socket WebSocket server. All 14 Python tests passed. Live test and
recovery evidence are in `state/voice/probe/last-result.json` and
`state/voice-acceptance.json`.

Checked against the matching upstream tag `rust-v0.154.0`, commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`, on 2026-09-14:

- The CLI has no voice startup command, and the TUI slash-command implementation
  has no realtime/voice command.
- The source includes a separate `codex-voice-host` component. Its README states:
  “It does not enable voice in the TUI. The existing CLI is unchanged.”
- That component opens default input and output devices. Its presence in source
  does not establish consumption of the `[audio]` device-name preferences.
- The installed package does not contain the component at
  `codex-resources/voice/bin/codex-voice-host` or its prepared native runtime.
- The app-server exposes experimental realtime methods accepting external audio.
  Using those would require an audio frontend; the normal CLI does not acquire
  one merely by setting microphone and speaker names.

Sources: [voice-host README](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/voice-host/README.md),
[device capture code](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/voice-host/src/devices.rs),
[TUI commands](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/tui/src/slash_command.rs).

This is an explicit upstream removal: [PR #27801](https://github.com/openai/codex/pull/27801),
merged June 12, 2026, deleted `/realtime`, TUI capture/playback, and device selection.
Instructions describing those commands apply to older implementations.

## Public Linux examples found

| Example | Evidence and relevance |
| --- | --- |
| [Ubuntu Codex app voice report](https://github.com/openai/codex/issues/40391) | August 24 report using desktop app 26.818.31338 on Ubuntu, with voice previously working against a remote Ubuntu project. Reports a regression in project association. Evidence of desktop use, not stock CLI voice. |
| [ha-codex-voice](https://github.com/AurelioB/ha-codex-voice) | Linux/Home Assistant implementation of an audio frontend to experimental Codex App Server WebRTC, authenticated through a Codex login. Its native voice route deliberately disables tools and owns temporary threads. Useful transport reference; needs adaptation to preserve our full-access operator. |
| [codex-realtime-linux](https://github.com/sergiopesch/codex-realtime-linux) | Independent Electron Linux client with a [published meetup demo](https://www.sergiopesch.com/projects/codex-realtime-linux). Uses separately billed OpenAI Realtime API voice and Codex app-server execution. It adds a separate voice agent and is not the intended single native voice operator. |

I did not find a verified working example of voice in the current stock Linux
terminal client. [Yorishiro](https://github.com/sktkkoo/Yorishiro) is another close
architectural reference, but its README explicitly says Linux support is not yet
available. None of these projects has been installed or executed here.

## Implemented connection

The frontend uses `thread/read` to check identity, `thread/resume` to rejoin the
saved thread with the requested permissions, and `thread/realtime/start` with
WebRTC, version V3, audio output, startup context enabled, and
`clientManagedHandoffs=false`. Codex owns the normal tools and the automatic
handoff between voice and execution. The frontend does not call `turn/start`,
`appendText`, or `appendSpeech` to turn speech into separate text jobs.

Capture uses the named Pulse source at 48 kHz mono; playback selects the named
sink. Audio passes through aiortc/PyAV, installed only in `.tools/voice-venv`.
Microphone frames are bounded to 80 ms and input stays silent during negotiation.
Transport failures end the frontend so the supervisor can resume the same
thread. No per-call lifecycle hook or separate API voice agent is introduced.

The earlier read-only daemon-lock blocker was resolved by selecting Full access
for the setup session. The live test then established account access, native
tool handoffs, and both audio directions without changing system packages.

Remaining acceptance checks include barge-in, long idle intervals, real network
and audio-server interruptions during a session, SIP registration refresh, and
phone calls. Initial TLS SIP registration has passed.
