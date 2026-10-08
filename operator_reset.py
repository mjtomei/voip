"""Explicit, bounded recovery of the phone operator without restarting its daemon."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import time
import uuid

from operator_mailbox import save_json
from voice_config import operator_thread_name
from voip import (ROOT, Rpc, SetupError, atomic_write, lock, lock_busy, operator_ensure,
                  operator_status, process_identity)

SCOPES = ("frontend", "session", "fresh")


def state_database(project):
    # A custom daemon may use another state store: do not guess or archive it.
    if project.operator.get("daemon_socket") and not project.operator.get("state_database"):
        raise SetupError("Session reset requires the default local Codex daemon")
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = Path(project.operator.get("state_database") or home / "state_5.sqlite")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def stored_thread(project, thread_id, require_no_children=True):
    with state_database(project) as db:
        row = db.execute("SELECT rollout_path, cwd, archived FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row or Path(row[1]).resolve() != project.workspace:
            raise SetupError("Cannot verify the saved operator in the local Codex state store")
        # Native archive cascades to descendants, including already archived ones.
        if require_no_children and db.execute("SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id=? LIMIT 1", (thread_id,)).fetchone():
            raise SetupError("Operator has spawned child agents; refusing a cascading session reset")
        return {"path": row[0], "archived": bool(row[2])}


def checked_thread(project, rpc, thread_id):
    thread = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
    if thread.get("id") != thread_id or Path(thread.get("cwd", "")).resolve() != project.workspace:
        raise SetupError("Saved operator identity or directory does not match")
    return thread


def resume(project, rpc, thread_id, new=False):
    params = {"threadId": thread_id, "cwd": str(project.workspace), **project.permission_params(), "excludeTurns": True, **project.instructions()}
    if new:
        params["initialTurnsPage"] = {"limit": 1}  # Persist an empty new thread.
    result = rpc.request("thread/resume", params)
    if result["thread"]["id"] != thread_id:
        raise SetupError("Codex resumed an unexpected thread")


def interrupt_turn(project, rpc, thread_id):
    """Stop current operator work before the final descendant check and archive."""
    thread = checked_thread(project, rpc, thread_id)
    if thread.get("status", {}).get("type") != "active":
        return
    history = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
    active = [turn["id"] for turn in history.get("turns", []) if turn.get("status") == "inProgress"]
    for turn_id in active:
        try:
            rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except SetupError:
            if checked_thread(project, rpc, thread_id).get("status", {}).get("type") == "active":
                raise
    deadline = time.monotonic() + 10
    while checked_thread(project, rpc, thread_id).get("status", {}).get("type") == "active":
        if time.monotonic() >= deadline:
            raise SetupError("Operator work did not stop; session reset was not applied")
        time.sleep(.2)


def stop_frontend(project):
    """Stop only the recorded frontend and its private audio/RPC child processes."""
    path = project.state / "operator-process.json"
    runtime = json.loads(path.read_text()) if path.exists() else {}
    pid, started = runtime.get("pid"), runtime.get("starttime")
    if not started or process_identity(pid) != started:
        if lock_busy(project.state / "operator-process.lock"):
            raise SetupError("Operator is starting or stopping; retry shortly")
        return
    if runtime.get("thread_id") != project.thread_id():
        raise SetupError("Running operator does not match the saved conversation")
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    if (str(ROOT / "voice_frontend.py").encode() not in command
            or b"--root" not in command
            or command[command.index(b"--root") + 1] != str(project.root).encode()):
        raise SetupError("Recorded process is not this project's voice frontend")
    listing = project.tmux("list-sessions", "-F", "#{session_name}\t#{session_id}").stdout
    sessions = [line.rsplit("\t", 1) for line in listing.splitlines() if "\t" in line]
    target = next((identity for name, identity in sessions if name == project.operator["session"]), None)
    if target is None:
        raise SetupError("Cannot find the owned frontend tmux session")
    owner = project.tmux("show-options", "-qv", "-t", target, "@voip_root").stdout.strip()
    if owner != str(project.root) or not lock_busy(project.state / "operator-process.lock"):
        raise SetupError("Cannot verify frontend supervisor ownership")
    # Capture pidfds before signaling. PID reuse must never target another agent.
    handles = []
    def collect(child):
        try:
            handle = os.pidfd_open(child)
            handles.append(handle)
            children = Path(f"/proc/{child}/task/{child}/children").read_text().split()
        except (ProcessLookupError, FileNotFoundError):
            return
        for descendant in children:
            collect(int(descendant))
    collect(pid)
    try:
        if not handles or process_identity(pid) != started:
            raise SetupError("Frontend identity changed during reset")
        signal.pidfd_send_signal(handles[0], signal.SIGTERM)
        deadline = time.monotonic() + 8
        while process_identity(pid) == started and time.monotonic() < deadline:
            time.sleep(.1)
        # Also reap helpers left behind by a previously crashed/frozen frontend.
        for handle in reversed(handles):
            try:
                signal.pidfd_send_signal(handle, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while lock_busy(project.state / "operator-process.lock") and time.monotonic() < deadline:
            time.sleep(.1)
        if lock_busy(project.state / "operator-process.lock"):
            raise SetupError("Frontend supervisor did not finish stopping")
    finally:
        for handle in handles:
            os.close(handle)


def recover_interrupted_reset(project):
    """Called with ensure.lock held; roll back an interrupted archive/create transaction.

    Never repeats thread/start. An ambiguous creation must be inspected separately.
    """
    path = project.state / "operator-reset.json"
    if not path.exists():
        return
    record = json.loads(path.read_text())
    if not record.get("needs_recovery"):
        return
    if time.time() < record.get("recover_after", 0):
        raise SetupError("Waiting for an interrupted reset request to settle")
    if lock_busy(project.state / "operator-process.lock"):
        raise SetupError("Interrupted reset still has a running frontend; inspect before recovery")
    old, new = record["old_thread_id"], record.get("new_thread_id")
    current = project.thread_id()
    if current == new:
        # Identity replacement is the commit point; keep the new conversation.
        with Rpc(project) as rpc:
            checked_thread(project, rpc, new)
    elif current == old:
        stored = stored_thread(project, old, require_no_children=False)
        with Rpc(project) as rpc:
            if stored["archived"]:
                rpc.request("thread/unarchive", {"threadId": old})
            resume(project, rpc, old)
    else:
        raise SetupError("Operator identity changed during reset; refusing automatic recovery")
    record.update(needs_recovery=False, status="recovered", finished_at=time.time())
    save_json(path, record)
    (project.state / "last-start").unlink(missing_ok=True)


def reset_operator(project, scope):
    if scope not in SCOPES or project.operator.get("frontend") != "voice":
        raise SetupError("Reset requires the voice frontend and a supported reset scope")
    record_path = project.state / "operator-reset.json"
    creation_pending = project.state / "operator-reset-create-pending.json"
    with lock(project.state / "operator-reset.lock"):
        # An explicit reset must wait for an ordinary supervisor check, which
        # holds this same lock briefly. Contention is not a reset failure.
        with lock(project.state / "ensure.lock", blocking=True):
            recover_interrupted_reset(project)
            if (project.state / "operator-paused").exists():
                raise SetupError("Operator recovery is paused; unpause before resetting")
            if scope == "fresh" and creation_pending.exists():
                raise SetupError("A previous new-conversation request has an unknown outcome; inspect it before retrying")
            old = project.thread_id()
            record = {"id": str(uuid.uuid4()), "scope": scope, "old_thread_id": old,
                      "started_at": time.time(), "status": "preparing", "needs_recovery": False}
            # Preflight before interrupting a working frontend.
            if scope != "frontend":
                stored_thread(project, old)
                with Rpc(project) as rpc:
                    checked_thread(project, rpc, old)
            save_json(record_path, record)
            try:
                stop_frontend(project)
                if scope != "frontend":
                    with Rpc(project) as rpc:
                        interrupt_turn(project, rpc, old)
                        stored = stored_thread(project, old)  # Recheck after stopping audio.
                        rollout = Path(stored["path"])
                        raw = rollout.read_text()
                        first = json.loads(raw.splitlines()[0])
                        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != old:
                            raise SetupError("Cannot verify the operator history backup")
                        backup = project.state / "operator-reset-backups" / (record["id"] + ".jsonl")
                        atomic_write(backup, raw)
                        record.update(status="archiving", needs_recovery=True,
                                      recover_after=time.time() + 60, backup=str(backup))
                        save_json(record_path, record)
                        rpc.request("thread/archive", {"threadId": old})
                        if scope == "session":
                            record.update(status="resuming", recover_after=time.time() + 60)
                            save_json(record_path, record)
                            rpc.request("thread/unarchive", {"threadId": old})
                            resume(project, rpc, old)
                        else:
                            record.update(status="creating", recover_after=time.time() + 60)
                            save_json(record_path, record)
                            save_json(creation_pending, {"reset_id": record["id"], "old_thread_id": old,
                                                         "requested_at": time.time()})
                            new = rpc.request("thread/start", {"cwd": str(project.workspace), **project.permission_params(), "ephemeral": False, **project.instructions()})["thread"]
                            new_id = str(uuid.UUID(new["id"]))
                            record.update(new_thread_id=new_id, status="created", recover_after=time.time() + 60)
                            save_json(record_path, record)  # Save identity before secondary requests.
                            creation_pending.unlink(missing_ok=True)
                            if new_id == old or Path(new["cwd"]).resolve() != project.workspace:
                                raise SetupError("Codex created an unexpected operator thread")
                            rpc.request("thread/name/set", {"threadId": new_id, "name": operator_thread_name(project)})
                            resume(project, rpc, new_id, new=True)
                            atomic_write(project.state / "operator-thread-id", new_id + "\n")
                record.update(needs_recovery=False, status="reconnecting")
                save_json(record_path, record)
                (project.state / "last-start").unlink(missing_ok=True)
            except Exception:
                # Keep ambiguous native mutations pending; ensure will restore
                # the original thread after the outstanding request settles.
                record.update(status="failed", failed_at=time.time())
                save_json(record_path, record)
                raise
        operator_ensure(project, wait=True)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            from voice_lifecycle import demand
            state = operator_status(project)["voice"]
            if state == "connected" or (state == "standby" and not demand(project)):
                record.update(status="completed", finished_at=time.time(), thread_id=project.thread_id())
                save_json(record_path, record)
                return record
            time.sleep(.25)
        record.update(status="awaiting_voice", finished_at=time.time(), thread_id=project.thread_id())
        save_json(record_path, record)
        raise SetupError("Reset applied; voice has not connected yet. Automatic recovery remains enabled")
