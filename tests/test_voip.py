import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import voip


FAKE_CODEX = '''#!/usr/bin/python3
import json, pathlib, signal, sys
root = pathlib.Path(__file__).parent
if sys.argv[1:3] == ["app-server", "proxy"]:
    for line in sys.stdin:
        msg = json.loads(line)
        with (root / "requests").open("a") as f:
            f.write(json.dumps(msg) + "\\n")
        if "id" not in msg:
            continue
        method = msg["method"]
        if (root / "fail-start").exists() and method == "thread/start":
            sys.exit(1)
        result = {}
        if method == "thread/start":
            result = {"thread": {"id": (root / "fake-id").read_text(), "cwd": str(root)}}
        elif method == "thread/read":
            result = {"thread": {"id": msg["params"]["threadId"], "cwd": str(root)}}
        response = {"id": msg["id"], "result": result}
        if (root / "fail-name").exists() and method == "thread/name/set":
            response = {"id": msg["id"], "error": {"message": "naming failed"}}
        print(json.dumps(response), flush=True)
else:
    with (root / "launches").open("a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    while True:
        signal.pause()
'''


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voip-test-")
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o755)
        self.thread = str(uuid.uuid4())
        (self.root / "fake-id").write_text(self.thread)
        config = (voip.ROOT / "config.example.toml").read_text()
        config = config.replace('frontend = "voice"', 'frontend = "text"')
        config = config.replace('codex = "codex"', 'codex = ' + json.dumps(str(self.fake)))
        config = config.replace('tmux_socket = ""', 'tmux_socket = ' + json.dumps(str(self.root / "tmux.sock")))
        config = config.replace('restart_delay_seconds = 30', 'restart_delay_seconds = 1')
        (self.root / "config.toml").write_text(config)
        self.project = voip.Project(self.root)
        self.project.rpc_command = lambda: [str(self.fake), "app-server", "proxy"]

    def tearDown(self):
        self.project.tmux("kill-server", check=False)
        deadline = time.monotonic() + 3
        while voip.lock_busy(self.project.state / "operator-process.lock") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.temp.cleanup()

    def invoke(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(self.project, *args)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for supervisor transition")

    def running(self):
        status = voip.operator_status(self.project)
        return status["codex_process_alive"] and status["process_lock_held"]

    def start(self):
        voip.atomic_write(self.project.state / "operator-thread-id", self.thread)
        self.invoke(voip.operator_ensure)
        self.wait_for(self.running)

    def allow_restart(self):
        (self.project.state / "last-start").unlink(missing_ok=True)

    def test_init_saves_once_and_never_replaces_identity(self):
        from service_fixture import single_recipient
        single_recipient(self.root)
        self.invoke(voip.operator_init)
        self.assertEqual(self.project.thread_id(), self.thread)
        with self.assertRaisesRegex(voip.SetupError, "already saved"):
            self.invoke(voip.operator_init)
        requests = [json.loads(x) for x in (self.root / "requests").read_text().splitlines()]
        self.assertEqual([x["method"] for x in requests],
                         ["initialize", "initialized", "thread/start", "thread/name/set", "thread/resume"])
        self.assertEqual(requests[2]["params"]["sandbox"], "read-only")
        self.assertEqual(requests[3]['params']['name'],
                         'Phone operator: Alice — ' + self.root.name)
        self.assertEqual((self.project.state / "operator-thread-id").stat().st_mode & 0o777, 0o600)

    def test_secondary_rpc_failure_cannot_create_another_thread(self):
        (self.root / "fail-name").touch()
        with self.assertRaisesRegex(voip.SetupError, "naming failed"):
            self.invoke(voip.operator_init)
        self.assertEqual(self.project.thread_id(), self.thread)
        with self.assertRaisesRegex(voip.SetupError, "already saved"):
            self.invoke(voip.operator_init)

    def test_adopt_uses_existing_thread(self):
        self.invoke(voip.operator_init, self.thread)
        methods = [json.loads(x)["method"] for x in (self.root / "requests").read_text().splitlines()]
        self.assertIn("thread/read", methods)
        self.assertNotIn("thread/start", methods)

    def test_unknown_creation_outcome_requires_adoption(self):
        (self.root / "fail-start").touch()
        with self.assertRaisesRegex(voip.SetupError, "connection closed"):
            self.invoke(voip.operator_init)
        with self.assertRaisesRegex(voip.SetupError, "unknown outcome"):
            self.invoke(voip.operator_init)
        self.invoke(voip.operator_init, self.thread)
        self.assertEqual(self.project.thread_id(), self.thread)
        self.assertFalse((self.project.state / "operator-init-pending").exists())

    def test_recover_dead_child_and_missing_tmux_with_same_thread(self):
        self.start()
        self.wait_for(lambda: (self.root / "launches").exists() and len((self.root / "launches").read_text().splitlines()) == 1)
        self.invoke(voip.operator_ensure)
        original = json.loads((self.project.state / "operator-process.json").read_text())
        os.kill(original["pid"], signal.SIGTERM)
        self.wait_for(lambda: not voip.operator_status(self.project)["process_lock_held"])
        self.wait_for(lambda: self.project.tmux("list-panes", "-t", "=voice-operator", "-F", "#{pane_dead}").stdout.strip() == "1")
        self.allow_restart()
        self.invoke(voip.operator_ensure)
        self.wait_for(self.running)
        self.wait_for(lambda: len((self.root / "launches").read_text().splitlines()) == 2)
        self.project.tmux("kill-session", "-t", "=voice-operator")
        self.wait_for(lambda: not voip.operator_status(self.project)["process_lock_held"])
        self.allow_restart()
        self.invoke(voip.operator_ensure)
        self.wait_for(self.running)
        self.wait_for(lambda: len((self.root / "launches").read_text().splitlines()) == 3)
        launches = [json.loads(x) for x in (self.root / "launches").read_text().splitlines()]
        self.assertEqual([x[:2] for x in launches], [["resume", self.thread]] * 3)
        self.assertIsNone(voip.process_identity(original["pid"]))

    def test_foreign_tmux_session_is_preserved(self):
        voip.atomic_write(self.project.state / "operator-thread-id", self.thread)
        self.project.tmux("new-session", "-d", "-s", "voice-operator", "sleep 100")
        before = self.project.tmux("list-panes", "-t", "=voice-operator", "-F", "#{pane_pid}").stdout
        with self.assertRaisesRegex(voip.SetupError, "already in use"):
            self.invoke(voip.operator_ensure)
        self.assertEqual(self.project.tmux("list-panes", "-t", "=voice-operator", "-F", "#{pane_pid}").stdout, before)

    def test_live_stale_voice_restarts_only_owned_frontend_with_same_thread(self):
        self.start()
        runtime = json.loads((self.project.state/'operator-process.json').read_text())
        self.project.operator.update(frontend='voice', daemon_socket='test-owned-daemon')
        self.project.operator_command = self.project.codex_command
        old = time.time()-100
        voip.atomic_write(self.project.state/'voice.json', json.dumps({**runtime, 'state':'connected', 'updated_at':old}))
        voip.atomic_write(self.project.state/'voice-activity.json', json.dumps({**runtime, 'observed_at':old}))
        self.allow_restart()
        def stop_owned(project):
            self.assertIs(project, self.project)
            os.kill(runtime['pid'], signal.SIGTERM)
            self.wait_for(lambda: not voip.operator_status(project)['process_lock_held'])
            self.wait_for(lambda: project.tmux('list-panes', '-t', '=voice-operator', '-F', '#{pane_dead}').stdout.strip() == '1')
        with patch('operator_reset.stop_frontend', side_effect=stop_owned) as stop:
            self.invoke(voip.operator_ensure)
            stop.assert_called_once()
        self.wait_for(self.running)
        after = json.loads((self.project.state/'operator-process.json').read_text())
        self.assertNotEqual(after['pid'], runtime['pid'])
        self.assertEqual(self.project.thread_id(), self.thread)
        recovery = json.loads((self.project.state/'voice-health-recovery.json').read_text())
        self.assertEqual(recovery['reason'], 'voice_heartbeat_stale')
        self.assertEqual(recovery['scope'], 'frontend')

    def test_pause_prevents_restart(self):
        voip.atomic_write(self.project.state / "operator-paused", "paused")
        self.invoke(voip.operator_ensure)
        self.assertFalse((self.root / "launches").exists())

    def test_orphan_is_not_duplicated(self):
        voip.atomic_write(self.project.state / "operator-thread-id", self.thread)
        voip.atomic_write(self.project.state / "operator-process.json", json.dumps({
            "pid": os.getpid(), "starttime": voip.process_identity(os.getpid()), "thread_id": self.thread}))
        with self.assertRaisesRegex(voip.SetupError, "no supervisor lock"):
            self.invoke(voip.operator_ensure)
        self.assertFalse((self.root / "launches").exists())

    def test_audio_rolls_back_new_modules_on_failure(self):
        commands = []
        def pactl(*args):
            commands.append(args)
            if args[:2] == ("load-module", "module-remap-source"):
                raise voip.SetupError("injected failure")
            return str(len(commands))
        with patch.object(self.project, "pulse_list", return_value=[]), patch.object(self.project, "pactl", side_effect=pactl):
            with self.assertRaisesRegex(voip.SetupError, "injected failure"):
                self.invoke(voip.audio_up)
        self.assertEqual(commands[-2:], [("unload-module", "2"), ("unload-module", "1")])

    def test_audio_conflicting_device_is_preserved(self):
        module = {"name": "module-null-sink", "argument": "sink_name=voip_in rate=8000"}
        with patch.object(self.project, "pulse_list", return_value=[module]), patch.object(self.project, "pactl") as mutate:
            with self.assertRaisesRegex(voip.SetupError, "conflicts"):
                self.invoke(voip.audio_up)
            mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
