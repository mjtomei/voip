"""Optional checks for this host's tmux-resume compatibility patch."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(os.environ.get("TMUX_RESUME_SOURCE", str(Path.home() / ".local/share/tmux-resume/tmux_resume.py")))
if not SOURCE.is_file():
    raise unittest.SkipTest("tmux-resume is not installed on this host")
spec = importlib.util.spec_from_file_location("tmux_resume_exclusion_test", SOURCE)
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)
if not hasattr(resume, "excluded_sessions"):
    raise unittest.SkipTest("The optional tmux-resume exclusion patch is not installed")


class ExclusionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voip-resume-exclusion-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / "tmux-resume/config.json"
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"exclude_sessions": ["voice-operator"]}))
        env = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.directory)})
        env.start()
        self.addCleanup(env.stop)
        self.data = {"version": 1, "server": "/tmp/test.sock|1|2", "sessions": [
            {"session_name": "voice-operator", "session_id": "$1", "links": [{"id": "@1"}]},
            {"session_name": "worker", "session_id": "$2", "links": [{"id": "@2"}]}],
            "windows": {"@1": {"panes": [{"location": "voice-operator:0"}]},
                        "@2": {"panes": [{"location": "worker:0"}]}},
            "warnings": ["voice-operator:0: old warning", "worker:0: keep", "global warning"],
            "blockers": ["global close blocker"]}

    def test_default_save_and_old_snapshot_restore_exclude_only_operator(self):
        selected = resume.select_session_records(self.data["sessions"], None)
        self.assertEqual([s["session_name"] for s in selected], ["worker"])
        selected = resume.select_snapshot(self.data, None)
        self.assertEqual(set(selected["windows"]), {"@2"})
        self.assertEqual(selected["warnings"], ["worker:0: keep", "global warning"])
        self.assertEqual(selected["blockers"], ["global close blocker"])
        self.assertEqual(len(self.data["sessions"]), 2)
        explicit = resume.select_snapshot(self.data, ["voice-operator"])
        self.assertEqual(set(explicit["windows"]), {"@1"})

    def test_empty_server_is_skipped_and_linked_worker_window_is_preserved(self):
        self.data["sessions"] = self.data["sessions"][:1]
        self.assertEqual(resume.select_parts([(self.directory, self.data)]), [])
        self.data["sessions"].append({"session_name": "worker", "session_id": "$2", "links": [{"id": "@1"}]})
        self.assertEqual(set(resume.select_snapshot(self.data, None)["windows"]), {"@1"})

    def test_invalid_configuration_is_rejected(self):
        self.config.write_text(json.dumps({"exclude_sessions": "voice-operator"}))
        with self.assertRaisesRegex(RuntimeError, "array of exact session names"):
            resume.select_session_records(self.data["sessions"], None)
