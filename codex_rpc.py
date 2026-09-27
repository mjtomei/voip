#!/usr/bin/env python3
"""Translate local JSON lines to the Codex daemon's Unix-socket WebSocket.

The daemon control socket requires an HTTP WebSocket upgrade. The stock
`codex app-server proxy` copies raw bytes and does not perform that upgrade.
This transport adapter never creates threads or submits prompts on its own.
"""
import argparse
import asyncio
import json
import sys

from websockets.asyncio.client import unix_connect

LIMIT = 2 * 1024 * 1024


async def relay(path):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        # Codex 0.154.0 closes the handshake when permessage-deflate is offered.
        async with unix_connect(path, uri="ws://localhost/rpc", compression=None, open_timeout=10,
                                close_timeout=2, max_size=LIMIT, max_queue=16) as socket:
            async def outgoing():
                while line := await reader.readline():
                    await socket.send(json.dumps(json.loads(line)))

            async def incoming():
                async for message in socket:
                    # Normalize to one line even if the server pretty-prints JSON.
                    line = json.dumps(json.loads(message)) + "\n"
                    await asyncio.to_thread(write_output, line)

            tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        transport.close()


def write_output(line):
    sys.stdout.write(line)
    sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(relay(args.socket))
    except Exception as error:
        # Do not print response bodies, headers, or thread content.
        print("Codex daemon transport failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
