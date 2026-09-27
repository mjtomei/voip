"""Real local WebRTC/Pulse transport; a fake daemon supplies negotiation only."""
import array
import asyncio
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
import uuid

if importlib.util.find_spec("aiortc") is None:
    raise unittest.SkipTest("Use .tools/voice-venv/bin/python to run the voice transport tests")

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from voice_frontend import AppServer, VoiceSession, RATE, close_process
from voip import Project, ROOT, SetupError, atomic_write, audio_up, argument_map


class FakeAppServer:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.events = asyncio.Queue()
        self.failed = asyncio.Event()
        self.peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.peer.on("track", self.peer.addTrack)  # Return received audio through a second RTP stream.
        self.wrong_cwd = False

    async def start(self):
        pass

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"cwd": "/wrong" if self.wrong_cwd else str(self.project.root)}}
        if method == "thread/resume" and (not params.get("excludeTurns") or "initialTurnsPage" in params):
            # Reproduce the loaded-thread persistence failure seen in 0.154.0.
            raise SetupError("Codex RPC: no rollout found for thread id " + self.project.thread_id())
        if method == "thread/realtime/start":
            await self.peer.setRemoteDescription(RTCSessionDescription(sdp=params["transport"]["sdp"], type="offer"))
            await self.peer.setLocalDescription(await self.peer.createAnswer())
            for event, body in (("sdp", {"sdp": self.peer.localDescription.sdp}), ("started", {})):
                self.events.put_nowait({"method": "thread/realtime/" + event,
                    "params": {"threadId": self.project.thread_id(), **body}})
        return {}

    async def close(self):
        await self.peer.close()

    async def next_event(self):
        return await self.events.get()


class VoiceTransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voip-rtc-test-")
        self.root = Path(self.temporary.name)
        config = (ROOT / "config.example.toml").read_text()
        prefix = "voip_test_" + uuid.uuid4().hex[:10]
        for name in ("voip_in", "voip_out", "voip_mic"):
            config = config.replace('"' + name + '"', '"' + prefix + name + '"')
        (self.root / "config.toml").write_text(config)
        (self.root / ".tools").symlink_to(ROOT / ".tools", target_is_directory=True)
        self.project = Project(self.root)
        atomic_write(self.project.state / "operator-thread-id", str(uuid.uuid4()))
        self.rpc = FakeAppServer(self.project)
        self.session = VoiceSession(self.project, self.rpc)
        self.audio_created = False

    async def asyncTearDown(self):
        await self.session.close()
        if self.audio_created:
            names = {self.project.audio[key] for key in ("incoming_sink", "outgoing_sink", "microphone")}
            for line in reversed(self.project.pactl("list", "short", "modules").splitlines()):
                fields = line.split("\t")
                args = argument_map(fields[2] if len(fields) > 2 else "")
                if args.get("sink_name") in names or args.get("source_name") in names:
                    self.project.pactl("unload-module", fields[0])
        self.temporary.cleanup()

    async def test_wrong_thread_directory_is_rejected_before_audio(self):
        self.rpc.wrong_cwd = True
        with self.assertRaisesRegex(SetupError, "another working directory"):
            await self.session.start()
        self.assertEqual([x[0] for x in self.rpc.calls], ["thread/read"])
        self.assertIsNone(self.session.microphone.process)

    async def test_virtual_microphone_over_real_webrtc_returns_audio_to_virtual_speaker(self):
        try:
            self.project.pactl("info")
        except SetupError:
            self.skipTest("No local Pulse/PipeWire server available")
        audio_up(self.project)
        self.audio_created = True
        await asyncio.wait_for(self.session.start(), 10)
        recorder = await asyncio.create_subprocess_exec(self.project.executable("parec"),
            "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=20",
            "--device=" + self.project.audio["outgoing_sink"] + ".monitor",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self.project.pulse_env())
        captured = bytearray()
        async def capture():
            while data := await recorder.stdout.read(4096):
                captured.extend(data)
        capturing = asyncio.create_task(capture())
        player = None
        try:
            await asyncio.sleep(0.3)
            tone = array.array("h", (int(6000 * math.sin(2 * math.pi * 700 * i / RATE)) for i in range(RATE)))
            player = await asyncio.create_subprocess_exec(self.project.executable("pacat"),
                "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=20",
                "--device=" + self.project.audio["incoming_sink"], stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self.project.pulse_env())
            await asyncio.wait_for(player.communicate(tone.tobytes()), 5)
            await asyncio.sleep(0.5)
        finally:
            await close_process(player)
            await close_process(recorder)
            await capturing
        values = array.array("h")
        values.frombytes(captured)
        rms = math.sqrt(sum(float(x) * x for x in values) / len(values)) if values else 0
        self.assertGreater(rms, 500, f"Expected returned audio; measured RMS {rms}")
        self.assertGreater(self.session.speaker.frames_received, 10)
        calls = dict(self.rpc.calls)
        self.assertEqual(calls["thread/resume"]["sandbox"], "read-only")
        self.assertTrue(calls["thread/resume"]["excludeTurns"])
        self.assertNotIn("initialTurnsPage", calls["thread/resume"])
        self.assertFalse(calls["thread/realtime/start"]["clientManagedHandoffs"])
        self.assertTrue(calls["thread/realtime/start"]["includeStartupContext"])
        self.assertEqual(calls["thread/realtime/start"]["threadId"], self.project.thread_id())
        self.assertNotIn("thread/start", calls)
        self.assertFalse(any("appendText" in name or "appendSpeech" in name or name == "turn/start" for name in calls))


class RpcTest(unittest.IsolatedAsyncioTestCase):
    async def test_notification_before_response_is_preserved_and_eof_fails_requests(self):
        with tempfile.TemporaryDirectory(prefix="voip-rpc-test-") as temporary:
            root = Path(temporary)
            fake = root / "fake-codex"
            fake.write_text('''#!/usr/bin/python3
import json, sys
for line in sys.stdin:
    msg=json.loads(line)
    if "id" not in msg: continue
    if msg["method"] == "fail": sys.exit(1)
    if msg["method"] == "test":
        print(json.dumps({"method":"thread/realtime/sdp","params":{"threadId":"test-thread","sdp":"test-answer"}}),flush=True)
    print(json.dumps({"id":msg["id"],"result":{}}),flush=True)
''')
            fake.chmod(0o755)
            config = (ROOT / "config.example.toml").read_text().replace('codex = "codex"', 'codex = ' + json.dumps(str(fake)))
            (root / "config.toml").write_text(config)
            project = Project(root)
            project.rpc_command = lambda: [str(fake), "app-server", "proxy"]
            rpc = AppServer(project, "test-thread")
            try:
                await rpc.start()
                await rpc.request("test", {})
                self.assertEqual((await rpc.events.get())["params"]["sdp"], "test-answer")
                with self.assertRaisesRegex(SetupError, "transport closed"):
                    await rpc.request("fail", {})
                self.assertTrue(rpc.failed.is_set())
            finally:
                await rpc.close()
