# Read-only voice session inspector

Generate a private, standalone HTML view of the saved operator:

```bash
cd /path/to/voip
python3 session_inspector.py
```

Open `state/voice-session-timeline.html` in a browser. The page has an interactive
timeline, a turn selector, filters for saved speech/connection events, and an event
table. It also shows current backend status from `thread/read`, the frontend's
connection flag, and a tmux session listing. No transcripts, tool arguments,
tool outputs, credentials, or audio are copied into the page.

To keep the view updated, leave this command running and keep the file open:

```bash
python3 session_inspector.py --watch
```

The collector regenerates the file every ten seconds; the page reloads at the
same interval. Ctrl-C stops the collector. The displayed snapshot timestamp/age
lets you detect a stopped collector. No HTTP server or service is installed.
Use `--no-rpc` to inspect history without querying the Codex daemon; current
backend status is then explicitly unavailable.

The inspector only reads the saved operator's local state/log, performs
`thread/read`, and lists tmux sessions. It never resumes a thread, submits a
request, interrupts a task, or sends input to a worker. Generated files are
mode 0600 under the ignored `state/` directory. It currently supports the default
local Codex state store, not a custom daemon socket.

Interpretation matters:

- Backend turn completion is not a verified business-task outcome and does not
  establish that background jobs or tmux workers have stopped.
- A tool call/result interval includes wrapper and record timing; it is not a
  precise subprocess runtime measurement.
- Saved speech transcript timestamps are not first-audio or handset-playback
  timestamps. The speech events shown near a turn are not automatically correlated
  to that turn.
- “Repeated” means identical transcript text was saved earlier. It does not prove
  another tool execution or another audible playback.
- tmux presence is not an inventory of each worker's active jobs. The frontend's
  connection flag is not an end-to-end responsiveness check.
- Missing/incomplete records remain unknown; an interruption record alone does
  not establish successful cancellation of external work.

The [latency report](VOICE-LATENCY-REPORT.md#e-follow-up-the-fresh-tmux-query-did-not-spend-minutes-executing-tools)
describes the initial tmux query shown in this history and the additional live
instrumentation needed to locate the remaining delay.
