"""Read-only, metadata-only view of the saved voice operator.

Rollout timestamps describe persisted records, not handset playback. This module
never submits turns, sends tmux input, or interprets speech as task completion.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from voip import Project, ROOT, Rpc, SetupError, atomic_write, operator_status


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() * 1000 if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def summarize(records):
    events, turns, calls = [], {}, {}
    seen_transcripts = set()
    active_turn = None
    skipped = 0

    def add(at, lane, label, detail="", turn=None):
        events.append({"at": at, "lane": lane, "label": label,
                       "detail": detail, "turn": turn})

    for line in records:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        at, payload = timestamp(record.get("timestamp")), record.get("payload", {})
        if at is None or not isinstance(payload, dict):
            continue
        kind, subtype = record.get("type"), payload.get("type")
        if kind == "event_msg" and subtype == "task_started":
            active_turn = payload.get("turn_id")
            if not isinstance(active_turn, str):
                active_turn = f"unidentified-{len(turns) + 1}"
            turns[active_turn] = {"id": active_turn, "number": len(turns) + 1,
                                  "start": at, "end": None, "state": "no end record",
                                  "tools": [], "label": "Backend turn"}
            add(at, "backend", "Backend turn started", turn=active_turn)
        elif kind == "event_msg" and subtype in ("task_complete", "turn_aborted", "task_aborted"):
            identity = payload.get("turn_id") or active_turn
            state = "turn completed" if subtype == "task_complete" else "turn interrupted"
            if identity in turns:
                turns[identity].update(end=at, state=state)
            add(at, "backend", state.capitalize(),
                "This is a turn lifecycle event; it does not verify external task completion or worker cancellation.", identity)
            if active_turn == identity:
                active_turn = None
        elif kind == "response_item" and subtype in ("custom_tool_call", "function_call"):
            identity = payload.get("call_id")
            name = payload.get("name", "tool")
            name = name if isinstance(name, str) else "tool"
            source = str(payload.get("input") or payload.get("arguments") or "")
            # Only fixed labels are derived from commands; never copy arguments.
            purpose = ("List tmux sessions/panes" if "tmux list-sessions" in source else
                       "Inspect tmux pane" if "tmux capture-pane" in source else name)
            call = {"start": at, "end": None, "name": name, "label": purpose,
                    "turn": active_turn}
            if isinstance(identity, str):
                calls[identity] = call
            if active_turn in turns:
                turns[active_turn]["tools"].append(call)
                if purpose != name:
                    turns[active_turn]["label"] = purpose
            add(at, "tools", "Tool call: " + purpose, "Arguments and output omitted.", active_turn)
        elif kind == "response_item" and subtype in ("custom_tool_call_output", "function_call_output"):
            call = calls.get(payload.get("call_id"))
            detail = "Result record present. Tool contents and exit status are not evaluated by this viewer."
            if call:
                call["end"] = at
                if at >= call["start"]:
                    detail += f" Call/result record interval: {(at-call['start'])/1000:.3f} s."
            add(at, "tools", "Tool result recorded", detail, call["turn"] if call else None)
        elif kind == "response_item" and subtype == "message" and payload.get("role") == "assistant":
            add(at, "backend", "Backend assistant text recorded",
                "Text content omitted. This does not establish delivery to the voice model.", active_turn)
        elif kind == "realtime_item" and subtype == "transcript_segment":
            role = payload.get("role")
            role = role if role in ("user", "assistant") else "unknown speaker"
            content = str(payload.get("text", ""))
            fingerprint = hashlib.sha256((role + content.strip()).encode()).digest()
            repeated = fingerprint in seen_transcripts
            seen_transcripts.add(fingerprint)
            detail = f"{len(content)} characters. Saved-record time, not playback time."
            if repeated:
                detail += " Identical text was saved earlier; replay or delayed persistence is not resolved."
            add(at, "voice", f"Saved {role} speech text" + (" (repeated)" if repeated else ""), detail)
        elif kind == "realtime_item" and subtype in ("realtime_session_started", "realtime_session_closed"):
            add(at, "transport", "Voice session opened" if subtype.endswith("started") else "Voice session closed",
                "A voice transport transition is separate from the persistent Codex conversation.")
    return {"events": sorted(events, key=lambda e: e["at"]),
            "turns": list(turns.values()), "skipped_lines": skipped}


def collect(project, live_status=True):
    if project.operator.get("daemon_socket"):
        raise SetupError("Inspector requires the default local Codex state store")
    identity = project.thread_id()
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    with sqlite3.connect((codex_dir / "state_5.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT rollout_path,cwd FROM threads WHERE id=?", (identity,)).fetchone()
    if not row or Path(row[1]).resolve() != project.root:
        raise SetupError("Cannot verify the saved operator's project directory")
    with Path(row[0]).open() as source:
        data = summarize(source)
    data["thread"] = identity
    data["generated"] = datetime.now(timezone.utc).isoformat()
    data["frontend"] = operator_status(project)
    data["backend"] = "not queried"
    data["backend_observed"] = None
    if live_status:
        try:
            with Rpc(project) as rpc:
                thread = rpc.request("thread/read", {"threadId": identity, "includeTurns": False})["thread"]
            if thread.get("id") != identity:
                raise SetupError("Unexpected thread identity")
            state = thread.get("status", {}).get("type")
            data["backend"] = state if state in ("active", "idle", "notLoaded", "systemError") else "unknown"
            data["backend_observed"] = datetime.now(timezone.utc).isoformat()
        except (SetupError, OSError, ValueError, KeyError, TypeError):
            data["backend"] = "unavailable"
    # Session presence is not a claim about each worker's jobs or liveness.
    result = project.tmux("list-sessions", "-F", "#{session_name}\t#{session_windows}\t#{session_attached}", check=False)
    data["tmux"] = [line.split("\t") for line in result.stdout.splitlines() if len(line.split("\t")) == 3]
    data["tmux_available"] = result.returncode == 0
    return data


def render(data, refresh=False):
    # Script context escaping prevents even tool/session names from injecting HTML.
    encoded = json.dumps(data, ensure_ascii=True).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = (ROOT / "docs/session-inspector.html").read_text()
    return template.replace("<!--REFRESH-->", '<meta http-equiv="refresh" content="10">' if refresh else "").replace("/*SNAPSHOT*/null", encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--watch", action="store_true", help="refresh the snapshot every ten seconds; Ctrl-C stops")
    parser.add_argument("--no-rpc", action="store_true", help="skip current backend status; inspect saved records only")
    args = parser.parse_args()
    project = Project(args.root)
    output = project.state / "voice-session-timeline.html"
    try:
        while True:
            data = collect(project, live_status=not args.no_rpc)
            atomic_write(output, render(data, refresh=args.watch))
            print(f"Updated {output} ({len(data['turns'])} recorded turns, backend {data['backend']})", flush=True)
            if not args.watch:
                break
            time.sleep(10)
    except KeyboardInterrupt:
        pass
    except (SetupError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Inspector could not read session metadata: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
