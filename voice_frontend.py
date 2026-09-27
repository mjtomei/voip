#!/usr/bin/env python3
"""PipeWire audio transport for one saved Codex App Server realtime thread.

Codex owns authentication, conversation, tools, and voice-to-Codex handoffs.
This client connects the audio endpoints and delivers local phone service notices.
"""
import argparse
from array import array
import asyncio
import contextlib
from fractions import Fraction
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

try:
    from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame, AudioResampler
except ImportError:
    raise SystemExit("Voice dependencies missing; use .tools/voice-venv/bin/python or run scripts/install-voice.py")

from voip import Project, SetupError, atomic_write, lock, process_identity
from call_events import deliver

RATE = 48000
SAMPLES = RATE // 50
FRAME_BYTES = SAMPLES * 2


def audible(pcm):
    """Coarse local quiet-window detection; never a transcript/playback timestamp."""
    samples = array("h", pcm)
    return bool(samples) and sum(value * value for value in samples) / len(samples) > 500 ** 2


async def close_process(process):
    if process is None:
        return
    if process.stdin:
        process.stdin.close()
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


class AppServer:
    """One bounded JSON-RPC connection to the normal local Codex daemon."""
    def __init__(self, project, thread_id):
        self.project, self.thread_id = project, thread_id
        self.process = None
        self.reader = None
        self.errors = None
        self.pending = {}
        self.sequence = 0
        self.events = asyncio.Queue(maxsize=32)
        self.failed = asyncio.Event()
        self.closing = False

    async def start(self):
        command = self.project.rpc_command()
        self.errors = tempfile.TemporaryFile()
        self.process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self.errors, limit=2 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read_messages())
        await self.request("initialize", {"clientInfo": {"name": "voip_voice", "version": "0.2.0"},
                                          "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized"})

    async def send(self, value):
        if self.failed.is_set() or self.process is None or self.process.returncode is not None:
            raise SetupError("Codex daemon transport unavailable")
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await asyncio.wait_for(self.process.stdin.drain(), 5)

    async def request(self, method, params, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as error:
            raise SetupError("Timed out waiting for Codex " + method) from error
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def read_messages(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(SetupError("Codex RPC: " + str(message["error"].get("message", "request failed"))))
                        else:
                            future.set_result(message.get("result"))
                    continue
                if "id" in message:
                    # Permission policy is set on the thread, never inferred
                    # from an unsolicited server request or tool output.
                    await self.send({"id": message["id"], "error": {
                        "code": -32601, "message": "Voice client does not implement this interactive request"}})
                    print("Codex requested an unsupported interactive client action", flush=True)
                    continue
                params, method = message.get("params", {}), message.get("method", "")
                if params.get("threadId") != self.thread_id:
                    continue
                if method in {"thread/realtime/started", "thread/realtime/sdp", "thread/realtime/error", "thread/realtime/closed"}:
                    self.events.put_nowait(message)
                elif method in {"turn/started", "turn/completed", "item/started", "item/completed"}:
                    # No raw tool arguments, transcripts, SDP, or audio in logs.
                    print("Codex event: " + method, flush=True)
        except (OSError, ValueError, asyncio.QueueFull):
            pass
        finally:
            if not self.closing:
                self.failed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(SetupError("Codex daemon transport closed"))

    async def close(self):
        self.closing = True
        await close_process(self.process)
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.errors:
            self.errors.close()

    async def next_event(self):
        event = asyncio.create_task(self.events.get())
        failed = asyncio.create_task(self.failed.wait())
        try:
            done, _ = await asyncio.wait((event, failed), return_when=asyncio.FIRST_COMPLETED)
            if event in done:
                return event.result()
            raise SetupError("Codex daemon disconnected during voice negotiation")
        finally:
            event.cancel()
            failed.cancel()
            await asyncio.gather(event, failed, return_exceptions=True)


class Microphone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, project):
        super().__init__()
        self.project = project
        self.process = None
        self.pump = None
        self.frames = asyncio.Queue(maxsize=4)  # At most 80 ms, discard old input on stalls.
        self.failed = asyncio.Event()
        self.enabled = False
        self.position = 0
        self.last_signal_at = 0

    async def start_capture(self):
        self.process = await asyncio.create_subprocess_exec(self.project.executable("parec"),
            "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=20",
            "--device=" + self.project.audio["microphone"], stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.project.pulse_env(), limit=FRAME_BYTES * 4)
        self.pump = asyncio.create_task(self.capture())

    async def capture(self):
        try:
            while True:
                pcm = await self.process.stdout.readexactly(FRAME_BYTES)
                if not self.enabled:
                    pcm = bytes(FRAME_BYTES)
                elif audible(pcm):
                    self.last_signal_at = time.time()
                if self.frames.full():
                    self.frames.get_nowait()
                self.frames.put_nowait((pcm, self.position))
                self.position += SAMPLES
        except (asyncio.IncompleteReadError, OSError):
            self.failed.set()

    def enable(self):
        while not self.frames.empty():
            self.frames.get_nowait()
        self.enabled = True

    async def recv(self):
        pcm, position = await self.frames.get()
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate, frame.time_base, frame.pts = RATE, Fraction(1, RATE), position
        return frame

    async def close(self):
        self.enabled = False
        self.stop()
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        await close_process(self.process)


class Speaker:
    def __init__(self, project):
        self.project, self.process = project, None
        self.failed = asyncio.Event()
        self.frames_received = 0
        self.tasks = set()
        self.watcher = None
        self.last_signal_at = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(self.project.executable("pacat"),
            "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=20",
            "--device=" + self.project.audio["outgoing_sink"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=self.project.pulse_env())
        self.process.stdin.transport.set_write_buffer_limits(high=FRAME_BYTES * 2, low=FRAME_BYTES)
        async def watch():
            await self.process.wait()
            self.failed.set()
        self.watcher = asyncio.create_task(watch())

    def attach(self, track):
        if track.kind == "audio":
            task = asyncio.create_task(self.play(track))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def play(self, track):
        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while True:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    pcm = bytes(converted.planes[0])[:converted.samples * 2]
                    if audible(pcm):
                        self.last_signal_at = time.time()
                    self.process.stdin.write(pcm)
                    await asyncio.wait_for(self.process.stdin.drain(), 1)
                    self.frames_received += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed.set()

    async def close(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await close_process(self.process)


class VoiceSession:
    def __init__(self, project, rpc=None):
        self.project, self.thread_id = project, project.thread_id()
        self.rpc = rpc or AppServer(project, self.thread_id)
        self.microphone, self.speaker = Microphone(project), Speaker(project)
        self.peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.peer.addTrack(self.microphone)
        self.channel = self.peer.createDataChannel("oai-events", ordered=True)
        self.channel_open, self.transport_failed = asyncio.Event(), asyncio.Event()
        self.started = False
        self.closing = False
        self.peer.on("track", self.speaker.attach)
        self.channel.on("open", self.channel_open.set)
        self.channel.on("close", self.transport_failed.set)

        @self.peer.on("connectionstatechange")
        def state_changed():
            if self.peer.connectionState in {"failed", "closed"} and not self.closing:
                self.transport_failed.set()

    def status(self, state):
        atomic_write(self.project.state / "voice.json", json.dumps({
            "state": state, "thread_id": self.thread_id, "pid": os.getpid(),
            "starttime": process_identity(os.getpid()), "updated_at": time.time(),
            "input": self.project.audio["microphone"], "output": self.project.audio["outgoing_sink"],
            "received_audio_frames": self.speaker.frames_received,
        }) + "\n")

    async def start(self):
        self.status("connecting")
        await self.rpc.start()
        thread = (await self.rpc.request("thread/read", {"threadId": self.thread_id, "includeTurns": False}))["thread"]
        if Path(thread["cwd"]).resolve() != self.project.workspace:
            raise SetupError("Saved Codex thread belongs to another working directory")
        # This frontend does not render history. Requesting initialTurnsPage also
        # forces a persistence pass in Codex 0.154.0, which can reject a loaded
        # thread with "no rollout found" even though metadata-only resume works.
        await self.rpc.request("thread/resume", {"threadId": self.thread_id, "cwd": str(self.project.workspace),
            **self.project.permission_params(), "excludeTurns": True, **self.project.instructions()})
        await self.microphone.start_capture()
        await self.speaker.start()
        await asyncio.wait_for(self.peer.setLocalDescription(await self.peer.createOffer()), 10)
        self.started = True  # Stop this attempt even if the start RPC has an ambiguous outcome.
        await self.rpc.request("thread/realtime/start", {
            "threadId": self.thread_id, "version": "v3", "outputModality": "audio",
            "includeStartupContext": True, "clientManagedHandoffs": False,
            "transport": {"type": "webrtc", "sdp": self.peer.localDescription.sdp},
        }, timeout=45)
        answer, accepted = None, False
        async with asyncio.timeout(45):
            while answer is None or not accepted:
                event = await self.rpc.next_event()
                method, params = event["method"], event["params"]
                if method == "thread/realtime/sdp":
                    answer = params["sdp"]
                elif method == "thread/realtime/started":
                    accepted = True
                elif method == "thread/realtime/error":
                    raise SetupError("Codex realtime: " + str(params.get("message", "connection rejected"))[:600])
                elif method == "thread/realtime/closed":
                    raise SetupError("Codex realtime closed: " + str(params.get("reason") or "no reason supplied")[:600])
            await self.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await self.channel_open.wait()
        if self.transport_failed.is_set() or self.microphone.failed.is_set() or self.speaker.failed.is_set():
            raise SetupError("Audio transport failed during startup")
        self.microphone.enable()
        self.status("connected")
        print("WebRTC connected to saved Codex thread " + self.thread_id + "; microphone enabled", flush=True)

    async def wait(self, stop):
        async def activity():
            while not stop.is_set():
                atomic_write(self.project.state / "voice-activity.json", json.dumps({
                    "thread_id": self.thread_id, "observed_at": time.time(),
                    "pid": os.getpid(), "starttime": process_identity(os.getpid()),
                    "input_at": self.microphone.last_signal_at, "output_at": self.speaker.last_signal_at}))
                await asyncio.sleep(.5)
        async def server_events():
            while True:
                event = await self.rpc.events.get()
                if event["method"] in {"thread/realtime/error", "thread/realtime/closed"}:
                    params = event.get("params", {})
                    reason = params.get("message") or params.get("reason") or "no reason supplied"
                    print("Codex realtime ended: " + str(reason)[:600], flush=True)
                    return
        watchers = [asyncio.create_task(stop.wait()), asyncio.create_task(self.rpc.failed.wait()),
                    asyncio.create_task(self.transport_failed.wait()), asyncio.create_task(self.microphone.failed.wait()),
                    asyncio.create_task(self.speaker.failed.wait()), asyncio.create_task(server_events()),
                    asyncio.create_task(deliver(self.project, self.rpc, stop, kinds={'connected'})), asyncio.create_task(activity())]
        labels = ('stop', 'daemon transport', 'WebRTC transport', 'microphone', 'speaker',
                  'server event', 'call notice delivery', 'activity heartbeat')
        try:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            if not stop.is_set():
                reason = ', '.join(label for label, task in zip(labels, watchers) if task in done)
                raise SetupError("Voice connection ended (" + reason + "); reconnect only while a call needs voice")
        finally:
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    async def close(self):
        self.closing = True
        await self.microphone.close()
        await self.speaker.close()
        await self.peer.close()
        if self.started and not self.rpc.failed.is_set():
            with contextlib.suppress(Exception):
                await self.rpc.request("thread/realtime/stop", {"threadId": self.thread_id}, timeout=3)
        await self.rpc.close()
        self.status("disconnected")


async def run(project):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    from voice_lifecycle import serve
    await serve(project, stop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--session", default="default")
    args = parser.parse_args()
    try:
        project = Project(args.root, args.session)
        with lock(project.state / "voice-frontend.lock"):
            asyncio.run(run(project))
    except (SetupError, OSError, asyncio.TimeoutError) as error:
        print("Voice error: " + str(error), file=sys.stderr)
        sys.exit(1)
