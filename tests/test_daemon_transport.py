"""Exercise production RPC clients against a real Unix-socket WebSocket server."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

if importlib.util.find_spec("websockets") is None:
    raise unittest.SkipTest("Use .tools/voice-venv/bin/python for daemon transport tests")

from websockets.asyncio.server import unix_serve
from voip import Project, ROOT, Rpc, SetupError
from voice_frontend import AppServer


class DaemonTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_upgrade_notifications_and_disconnect(self):
        with tempfile.TemporaryDirectory(prefix="voip-daemon-test-") as directory:
            root = Path(directory)
            socket = root / "daemon.sock"
            config = (ROOT / "config.example.toml").read_text().replace(
                'daemon_socket = ""', 'daemon_socket = ' + json.dumps(str(socket)))
            (root / "config.toml").write_text(config)
            project = Project(root)
            requests = []
            connections = []

            async def serve(connection):
                connections.append(connection.request)
                async for data in connection:
                    request = json.loads(data)
                    requests.append(request)
                    if "id" not in request:
                        continue
                    if request["method"] == "fail":
                        await connection.close()
                        return
                    if request["method"] == "test":
                        await connection.send(json.dumps({"method": "thread/realtime/sdp",
                            "params": {"threadId": "test-thread", "sdp": "answer"}}, indent=2))
                    await connection.send(json.dumps({"id": request["id"], "result": {"ok": True}}))

            async with unix_serve(serve, str(socket)):
                def sync_requests():
                    with Rpc(project) as rpc:
                        self.assertEqual(rpc.request("test", {}), {"ok": True})
                await asyncio.wait_for(asyncio.to_thread(sync_requests), 10)
                rpc = AppServer(project, "test-thread")
                try:
                    await rpc.start()
                    self.assertEqual(await rpc.request("test", {}), {"ok": True})
                    self.assertEqual((await rpc.next_event())["params"]["sdp"], "answer")
                    with self.assertRaisesRegex(SetupError, "closed"):
                        await rpc.request("fail", {})
                    self.assertTrue(rpc.failed.is_set())
                finally:
                    await rpc.close()
            self.assertEqual(len(connections), 2)
            for connection in connections:
                self.assertEqual(connection.path, "/rpc")
                self.assertNotIn("Sec-WebSocket-Extensions", connection.headers)
            self.assertEqual([x["method"] for x in requests],
                ["initialize", "initialized", "test", "initialize", "initialized", "test", "fail"])
