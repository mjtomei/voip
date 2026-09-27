import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import operator_reset as resets
from operator_mailbox import save_json
from voip import Project, ROOT, SetupError, atomic_write


class ResetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.toml").write_text((ROOT / "config.example.toml").read_text())
        self.project = Project(self.root)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        with self.db() as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER)")
            db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT)")
        self.old = str(uuid.uuid4())
        self.new = str(uuid.uuid4())
        self.add_thread(self.old)
        atomic_write(self.project.state / "operator-thread-id", self.old)
        self.calls = []
        self.fail = None
        self.active = False
        outer = self
        class Rpc:
            def __init__(self, project): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, params):
                outer.calls.append((method, params))
                tid = params.get("threadId", outer.new)
                if method == "thread/start":
                    outer.add_thread(outer.new)
                if method in ("thread/archive", "thread/unarchive"):
                    with outer.db() as db:
                        db.execute("UPDATE threads SET archived=? WHERE id=?", (method == "thread/archive", tid))
                if method == "turn/interrupt":
                    outer.active = False
                if method == outer.fail:
                    raise SetupError("Simulated unknown RPC outcome")
                return {"thread": {"id": tid, "cwd": str(outer.root),
                    "status": {"type": "active" if outer.active else "idle"},
                    "turns": [{"id": "active-turn", "status": "inProgress"}] if outer.active else []}}
        for target, value in [("Rpc", Rpc), ("stop_frontend", None), ("operator_ensure", None),
                              ("operator_status", {"voice": "connected"})]:
            mocked = patch.object(resets, target, **({"new": value} if target == "Rpc" else {"return_value": value}))
            obj = mocked.start(); self.addCleanup(mocked.stop)
            setattr(self, target, obj)

    def db(self):
        return sqlite3.connect(self.home / "state_5.sqlite")

    def add_thread(self, tid):
        path = self.home / (tid + ".jsonl")
        atomic_write(path, json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n")
        with self.db() as db:
            db.execute("INSERT INTO threads VALUES(?,?,?,0)", (tid, str(path), str(self.root)))

    def archived(self, tid):
        with self.db() as db:
            return bool(db.execute("SELECT archived FROM threads WHERE id=?", (tid,)).fetchone()[0])

    def recover(self):
        path = self.project.state / "operator-reset.json"
        record = json.loads(path.read_text())
        record["recover_after"] = 0
        save_json(path, record)
        self.fail = None
        resets.recover_interrupted_reset(self.project)

    def test_frontend_only_never_mutates_codex_thread(self):
        result = resets.reset_operator(self.project, "frontend")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.project.thread_id(), self.old)
        self.stop_frontend.assert_called_once()

    def test_reset_waits_for_an_existing_supervisor_check(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from voip import lock
        entered = Event()
        def reset():
            entered.set()
            return resets.reset_operator(self.project, 'frontend')
        with ThreadPoolExecutor(1) as pool:
            with lock(self.project.state / 'ensure.lock'):
                future = pool.submit(reset)
                self.assertTrue(entered.wait(2))
                time.sleep(.05)
                self.stop_frontend.assert_not_called()
            result = future.result(timeout=5)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.project.thread_id(), self.old)

    def test_session_reset_preserves_uuid_and_private_history_backup(self):
        result = resets.reset_operator(self.project, "session")
        self.assertEqual(self.project.thread_id(), self.old)
        self.assertFalse(self.archived(self.old))
        methods = [m for m, _ in self.calls]
        self.assertEqual([m for m in methods if m in ["thread/archive", "thread/unarchive", "thread/start"]],
                         ["thread/archive", "thread/unarchive"])
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), (self.home / (self.old + ".jsonl")).read_bytes())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_fresh_reset_creates_once_then_commits_new_identity(self):
        result = resets.reset_operator(self.project, "fresh")
        self.assertEqual(result["new_thread_id"], self.new)
        self.assertEqual(self.project.thread_id(), self.new)
        self.assertTrue(self.archived(self.old))
        self.assertEqual(sum(m == "thread/start" for m, _ in self.calls), 1)
        self.assertFalse((self.project.state / "operator-reset-create-pending.json").exists())
        resume = next(p for m, p in self.calls if m == "thread/resume")
        self.assertEqual(resume["threadId"], self.new)
        self.assertEqual(resume["initialTurnsPage"], {"limit": 1})

    def test_unknown_archive_outcome_recovers_without_creating_a_thread(self):
        self.fail = "thread/archive"
        with self.assertRaises(SetupError):
            resets.reset_operator(self.project, "session")
        self.assertTrue(self.archived(self.old))
        with self.assertRaisesRegex(SetupError, "settle"):
            resets.recover_interrupted_reset(self.project)
        self.recover()
        self.assertFalse(self.archived(self.old))
        self.assertEqual(self.project.thread_id(), self.old)
        self.assertFalse(any(m == "thread/start" for m, _ in self.calls))

    def test_ambiguous_creation_restores_old_and_blocks_duplicate_creation(self):
        self.fail = "thread/start"
        with self.assertRaises(SetupError):
            resets.reset_operator(self.project, "fresh")
        self.recover()
        self.assertFalse(self.archived(self.old))
        self.assertEqual(self.project.thread_id(), self.old)
        with self.assertRaisesRegex(SetupError, "unknown outcome"):
            resets.reset_operator(self.project, "fresh")
        self.assertEqual(sum(m == "thread/start" for m, _ in self.calls), 1)

    def test_secondary_failure_records_new_uuid_and_restores_original(self):
        self.fail = "thread/name/set"
        with self.assertRaises(SetupError):
            resets.reset_operator(self.project, "fresh")
        record = json.loads((self.project.state / "operator-reset.json").read_text())
        self.assertEqual(record["new_thread_id"], self.new)
        self.recover()
        self.assertEqual(self.project.thread_id(), self.old)
        self.assertFalse(self.archived(self.old))

    def test_crash_after_identity_commit_keeps_new_conversation(self):
        resets.reset_operator(self.project, "fresh")
        path = self.project.state / "operator-reset.json"
        record = json.loads(path.read_text())
        record.update(needs_recovery=True, recover_after=0)
        save_json(path, record)
        resets.recover_interrupted_reset(self.project)
        self.assertEqual(self.project.thread_id(), self.new)
        self.assertTrue(self.archived(self.old))

    def test_spawned_agents_block_cascading_archive_before_frontend_stops(self):
        with self.db() as db:
            db.execute("INSERT INTO thread_spawn_edges VALUES(?,?)", (self.old, "worker"))
        with self.assertRaisesRegex(SetupError, "child agents"):
            resets.reset_operator(self.project, "session")
        self.stop_frontend.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_active_turn_is_interrupted_before_archive(self):
        self.active = True
        resets.reset_operator(self.project, "session")
        methods = [m for m, _ in self.calls]
        self.assertLess(methods.index("turn/interrupt"), methods.index("thread/archive"))
        params = next(p for m, p in self.calls if m == "turn/interrupt")
        self.assertEqual(params, {"threadId": self.old, "turnId": "active-turn"})

    def test_wrong_workspace_and_paused_recovery_do_not_stop_frontend(self):
        with self.db() as db:
            db.execute("UPDATE threads SET cwd='/wrong'")
        with self.assertRaises(SetupError):
            resets.reset_operator(self.project, "fresh")
        self.stop_frontend.assert_not_called()
        atomic_write(self.project.state / "operator-paused", "paused")
        with self.assertRaisesRegex(SetupError, "paused"):
            resets.reset_operator(self.project, "frontend")
        self.stop_frontend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
