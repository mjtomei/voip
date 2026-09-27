"""Post-connect keypad recovery, independent of the Codex voice connection."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

from operator_mailbox import save_json
from voip import ROOT, SetupError

COMMANDS = {"*1#": "frontend", "*2#": "session", "*3#": "fresh"}
GAP_SECONDS = 8
COOLDOWN_SECONDS = 15


class KeypadControl:
    def __init__(self, project, events):
        self.project, self.events = project, events
        self.buffers, self.held = {}, {}
        self.task = None
        self.last_command = project.state / "keypad-last-command.json"

    def connected(self, key):
        state = self.events.load()
        return (state["observer_online"] and time.time() - state["observed_at"] < 10
                and state["calls"].get(key, {}).get("connected", False))

    def clear(self):
        self.buffers.clear()
        self.held.clear()

    def command(self, value):
        """Return a completed command; raw digits stay in memory and are never logged."""
        from call_events import call_key
        if not value.get("event") or value.get("class") != "call":
            return None
        aor = f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"
        if value.get("accountaor", "").split(";", 1)[0] != aor:
            return None
        kind = value.get("type")
        if kind not in {"CALL_DTMF_START", "CALL_DTMF_END", "CALL_CLOSED"}:
            return None
        key = call_key(value.get("id"))
        if kind == "CALL_CLOSED" or not self.connected(key):
            self.buffers.pop(key, None)
            self.held.pop(key, None)
            return None
        if kind == "CALL_DTMF_END":
            self.held.pop(key, None)
            return None
        digit = value.get("param")
        if not isinstance(digit, str) or len(digit) != 1 or digit not in "0123456789*#ABCD":
            self.buffers.pop(key, None)
            return None
        now = time.monotonic()
        held_digit, held_at = self.held.get(key, (None, 0))
        if held_digit == digit and now - held_at < GAP_SECONDS:
            return None  # Long presses/repeated START do not enter a digit twice.
        self.held[key] = (digit, now)
        prefix, previous = self.buffers.pop(key, ("", 0))
        if now - previous > GAP_SECONDS:
            prefix = ""
        prefix = "*" if digit == "*" else prefix + digit
        if prefix in COMMANDS:
            return COMMANDS[prefix], key
        if any(command.startswith(prefix) for command in COMMANDS):
            self.buffers[key] = (prefix, now)
        return None

    def handle(self, value):
        command = self.command(value)
        if command is None:
            return
        if self.task is not None and not self.task.done():
            return  # Never queue another reset behind one already in progress.
        previous = json.loads(self.last_command.read_text()) if self.last_command.exists() else {}
        if time.time() - previous.get("accepted_at", 0) < COOLDOWN_SECONDS:
            return
        scope, key = command
        save_json(self.last_command, {"scope": scope, "call": key, "accepted_at": time.time()})
        self.task = asyncio.create_task(self.execute(scope, key))

    async def say(self, text, key):
        if not self.connected(key):
            return
        # Playback goes only to the caller; it is never a prompt to Codex.
        try:
            with tempfile.TemporaryDirectory(prefix="voip-keypad-") as directory:
                wav = str(Path(directory) / "notice.wav")
                for args in ([self.project.executable("espeak-ng"), "-s", "165", "-w", wav, text],
                             [self.project.executable("paplay"), "--device=" + self.project.audio["outgoing_sink"], wav]):
                    if not self.connected(key):
                        return
                    process = await asyncio.create_subprocess_exec(*args, env=self.project.pulse_env(),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(process.wait(), 12)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    if process.returncode:
                        return
        except (OSError, SetupError, asyncio.TimeoutError):
            pass  # A broken audio path must not prevent a requested recovery.

    async def execute(self, scope, key):
        process = None
        try:
            await self.say({"frontend": "Reconnecting voice.",
                "session": "Restarting the operator and keeping the conversation.",
                "fresh": "Starting a new operator conversation. The previous conversation will be saved."}[scope], key)
            # Once a command is accepted, a hangup does not cancel its reset.
            process = await asyncio.create_subprocess_exec(*self.project.cli(), "operator", "reset", "--scope", scope,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            code = await asyncio.wait_for(process.wait(), 180)
            print("Keypad reset " + scope + (" completed" if code == 0 else " not completed"), flush=True)
            await self.say("Voice connected. Please repeat your last request." if code == 0 else
                           "The reset could not finish. Please check the service status.", key)
        except (OSError, asyncio.TimeoutError):
            print("Keypad reset could not finish", flush=True)
            await self.say("The reset could not finish. Please check the service status.", key)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
