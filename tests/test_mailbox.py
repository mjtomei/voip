import subprocess
import tempfile
import unittest
from pathlib import Path

from operator_mailbox import Mailbox
from voip import Project, ROOT, SetupError
from service_fixture import single_recipient


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.toml").write_text((ROOT / "config.example.toml").read_text())
        single_recipient(self.root)
        self.project = Project(self.root)
        self.box = Mailbox(self.project)

    def test_unaddressed_until_explicitly_addressed_and_can_reopen(self):
        self.assertEqual(self.box.count(), 0)
        self.box.post("claude worker", "Review", "Please discuss the failing build.", "build-1")
        self.box.read("build-1")
        self.box.list()
        self.assertEqual(Mailbox(self.project).count(), 1)
        self.box.address("build-1", "Discussed; worker is fixing it.")
        self.assertEqual(self.box.count(), 0)
        self.assertEqual(self.box.read("build-1")["resolution"], "Discussed; worker is fixing it.")
        self.assertEqual(len(self.box.list(all_messages=True)), 1)
        self.box.address("build-1", reopen=True)
        self.assertEqual(self.box.count(), 1)
        self.assertIsNone(self.box.read("build-1")["addressed_at"])

    def test_retries_do_not_duplicate_or_reopen_addressed_messages(self):
        first = self.box.post("worker", "Subject", "Body", "same")
        self.assertEqual(self.box.post("worker", "Subject", "Body", "same"), first)
        self.box.address("same")
        self.box.post("worker", "Subject", "Body", "same")
        self.assertEqual(self.box.count(), 0)
        with self.assertRaisesRegex(SetupError, "different content"):
            self.box.post("worker", "Subject", "Changed", "same")

    def test_message_count_excludes_only_addressed_messages(self):
        for n in range(3):
            self.box.post("worker", "Subject", "Body", str(n))
        self.box.read("0")
        self.box.address("1")
        self.assertEqual(self.box.count(), 2)
        self.assertEqual([m["id"] for m in self.box.list()], ["0", "2"])
        self.assertEqual(self.box.path.stat().st_mode & 0o777, 0o600)

    def test_cli_accepts_multiline_text_from_stdin_and_does_not_execute_it(self):
        body = "Discuss this code:\n$(touch DO_NOT_CREATE)\n`echo literal`\n"
        def cli(*args, **kwargs):
            return subprocess.run(["python3", str(ROOT / "voip.py"), "--root", str(self.root),
                                   "mailbox", *args], text=True, capture_output=True, check=True, **kwargs).stdout
        self.assertEqual(cli("post", "--from", "claude:work", "--subject", "Review", "--key", "literal", "--file", "-", input=body).strip(), "literal")
        self.assertIn(body, cli("read", "literal"))
        self.assertEqual(cli("count").strip(), "1")
        cli("address", "literal", "--note", "Done")
        self.assertEqual(cli("count").strip(), "0")
        self.assertFalse((ROOT / "DO_NOT_CREATE").exists())

    def test_invalid_or_missing_messages_leave_existing_state_intact(self):
        self.box.post("worker", "Subject", "Body", "safe")
        before = self.box.path.read_bytes()
        for values in (("worker", "Subject", " ", "empty"), ("worker\nspoof", "Subject", "Body", "bad"),
                       ("worker", "Subject", "Body", "../escape")):
            with self.assertRaises(SetupError):
                self.box.post(*values)
        with self.assertRaises(SetupError):
            self.box.address("missing")
        self.assertEqual(self.box.path.read_bytes(), before)
