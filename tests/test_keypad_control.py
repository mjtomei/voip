import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from call_events import CallEvents, call_key, monitor, read_frame
from keypad_control import KeypadControl
from voip import Project, ROOT


class KeypadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "config.toml").write_text((ROOT / "config.example.toml").read_text())
        self.project = Project(root)
        self.events = CallEvents(self.project)
        self.control = KeypadControl(self.project, self.events)
        self.events.event(self.event("CALL_ESTABLISHED"))

    def event(self, kind="CALL_DTMF_START", digit=None, identity="one", **values):
        return {"event": True, "class": "call", "type": kind, "param": digit, "id": identity,
                "accountaor": "sip:operator@YOUR-DOMAIN.sip.twilio.com", **values}

    def type(self, digits, **kwargs):
        results = []
        for digit in digits:
            command = self.control.command(self.event(digit=digit, **kwargs))
            if command:
                results.append(command)
            self.control.command(self.event("CALL_DTMF_END", **kwargs))
        return results

    def test_three_commands_require_complete_delimited_sequences(self):
        for digits, scope in [("*1#", "frontend"), ("*2#", "session"), ("*3#", "fresh")]:
            self.assertEqual(self.type(digits), [(scope, call_key("one"))])
        for digits in ["1#", "2#", "3#", "123456#", "*11#", "*4#", "*1", "#"]:
            self.control.clear()
            self.assertEqual(self.type(digits), [])

    def test_only_current_configured_established_calls_can_reset(self):
        for overrides in [{"accountaor": "sip:other@other.example"}, {"identity": "unknown"},
                          {"event": False}, {"class": "application"}]:
            self.assertEqual(self.type("*3#", **overrides), [])
        self.events.offline()
        self.assertEqual(self.type("*3#"), [])
        self.events.snapshot({call_key("one"): True})
        self.events.event(self.event("CALL_CLOSED"))
        self.assertEqual(self.type("*3#"), [])

    def test_partial_commands_do_not_cross_calls_outages_or_timeout(self):
        self.events.event(self.event("CALL_ESTABLISHED", identity="two"))
        self.assertEqual(self.type("*2"), [])
        self.assertEqual(self.type("#", identity="two"), [])
        self.control.clear()
        self.assertEqual(self.type("#"), [])
        self.type("*2")
        with patch("keypad_control.time.monotonic", return_value=time.monotonic() + 20):
            self.assertEqual(self.type("#"), [])
        self.type("*2")
        self.control.command(self.event("CALL_CLOSED"))
        self.assertEqual(self.type("#"), [])

    def test_repeated_start_events_and_release_events_do_not_duplicate_digits(self):
        results = []
        for digit in "*1#":
            for _ in range(3):
                value = self.control.command(self.event(digit=digit))
                if value:
                    results.append(value)
            self.control.command(self.event("CALL_DTMF_END"))
        self.assertEqual(results, [("frontend", call_key("one"))])

    async def test_only_one_reset_runs_and_cooldown_survives_monitor_restart(self):
        finish = asyncio.Event()
        seen = []
        async def execute(*args):
            seen.append(args)
            await finish.wait()
        self.control.execute = execute
        def enter(sequence):
            for digit in sequence:
                self.control.handle(self.event(digit=digit))
                self.control.handle(self.event("CALL_DTMF_END"))
        enter("*2#")
        await asyncio.sleep(0)
        enter("*3#")
        self.assertEqual(seen, [("session", call_key("one"))])
        finish.set()
        await self.control.task
        self.control = KeypadControl(self.project, self.events)
        self.control.execute = AsyncMock()
        enter("*3#")
        self.assertIsNone(self.control.task)
        saved = json.loads((self.project.state / "keypad-last-command.json").read_text())
        self.assertEqual(set(saved), {"scope", "call", "accepted_at"})

    async def test_dispatch_is_literal_and_hangup_does_not_cancel_accepted_reset(self):
        self.events.event(self.event("CALL_CLOSED"))
        process = AsyncMock()
        process.wait.return_value = 0
        process.returncode = 0
        self.control.say = AsyncMock()
        with patch("keypad_control.asyncio.create_subprocess_exec", return_value=process) as start:
            await self.control.execute("session", call_key("one"))
        args = start.call_args.args
        self.assertEqual(args[-4:], ("operator", "reset", "--scope", "session"))
        self.assertNotIn("shell", start.call_args.kwargs)

    async def test_announcements_go_to_caller_output_only(self):
        self.project.executable = lambda name: name
        process = AsyncMock()
        process.wait.return_value = 0
        process.returncode = 0
        with patch("keypad_control.asyncio.create_subprocess_exec", return_value=process) as start:
            await self.control.say("Reconnecting voice.", call_key("one"))
            self.assertEqual(start.call_args_list[-1].args[1], "--device=voip_out")
            start.reset_mock()
            self.events.event(self.event("CALL_CLOSED"))
            await self.control.say("Connected.", call_key("one"))
            start.assert_not_called()

    async def test_real_control_socket_dispatches_dtmf_while_reset_is_running(self):
        dispatched, closed = asyncio.Event(), asyncio.Event()
        observed = []
        async def reset(control, scope, key):
            observed.append((scope, key))
            dispatched.set()
            await closed.wait()
        async def serve(reader, writer):
            def send(value):
                body = json.dumps(value).encode()
                writer.write(str(len(body)).encode() + b":" + body + b",")
            try:
                first = await read_frame(reader)
                self.assertEqual(first['command'], 'reginfo')
                send({'response': True, 'token': first['token'], 'ok': True,
                      'data': '--- User Agents (1) ---\n0 - sip:operator@YOUR-DOMAIN.sip.twilio.com OK Twilio Expires 600s\n'})
                await writer.drain()
                await read_frame(reader)
                send(self.event("CALL_ESTABLISHED"))
                send({"response": True, "token": "voip-snapshot", "ok": True,
                      "data": "--- Active calls (1) ---\n[line 1, id one] 00:10 ESTABLISHED sip:caller@example.com\n"})
                for digit in "*2#":
                    send(self.event(digit=digit))
                    send(self.event("CALL_DTMF_END"))
                await writer.drain()
                await dispatched.wait()
                # Closing the phone call must still be observed during a reset.
                send(self.event("CALL_CLOSED"))
                await writer.drain()
                await closed.wait()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.project.sip["control_port"] = server.sockets[0].getsockname()[1]
        task = None
        try:
            with patch.object(KeypadControl, "execute", reset):
                task = asyncio.create_task(monitor(self.project))
                await asyncio.wait_for(dispatched.wait(), 3)
                async with asyncio.timeout(3):
                    while self.events.load()["calls"][call_key("one")]["connected"]:
                        await asyncio.sleep(.02)
                self.assertEqual(observed, [("session", call_key("one"))])
        finally:
            closed.set()
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
