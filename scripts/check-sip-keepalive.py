#!/usr/bin/env python3
"""Exercise installed Baresip against a local TLS registrar (no real calls).

The registrar intentionally omits Require: outbound, like Twilio. Verify two
CRLF pings on the same TLS flow, and no fallback for unrelated/lookalike hosts.
No production configuration, credentials, or audio devices are used.
"""
import asyncio
from pathlib import Path
import ssl
import socket
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


async def check(directory, host, expected, address):
    directory.mkdir()
    cert = directory.parent / "cert.pem"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, directory.parent / "key.pem")
    connections = []
    tasks = set()

    async def registrar(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        flow = {"registers": 0, "pings": 0, "closed": False}
        connections.append(flow)
        try:
            while True:
                first = await reader.readuntil(b"\r\n")
                if first == b"\r\n":
                    assert await reader.readexactly(2) == b"\r\n"
                    flow["pings"] += 1
                    writer.write(b"\r\n")
                    await writer.drain()
                    continue
                headers = {}
                while True:
                    line = await reader.readuntil(b"\r\n")
                    if line == b"\r\n":
                        break
                    key, value = line.decode().split(":", 1)
                    headers.setdefault(key.lower(), []).append(value.strip())
                await reader.readexactly(int(headers.get("content-length", [0])[0]))
                assert first.startswith(b"REGISTER "), first
                flow["registers"] += 1
                response = ["SIP/2.0 200 OK"]
                for key in ("via", "from", "to", "call-id", "cseq", "contact"):
                    response.extend(f"{key}: {value}" for value in headers[key])
                response.extend(["Content-Length: 0", "", ""])
                writer.write("\r\n".join(response).encode())
                await writer.drain()
        except asyncio.IncompleteReadError:
            flow["closed"] = True
        finally:
            writer.close()

    server = await asyncio.start_server(registrar, address, 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    (directory / "config").write_text(f"""poll_method epoll
sip_transports tls
sip_verify_server yes
sip_cafile {cert}
module_path {ROOT / '.tools/lib/baresip/modules'}
module uuid.so
module_app account.so
""")
    (directory / "accounts").write_text(
        f'<sip:test@{host};transport=tls>;sipnat=outbound;regint=600;'
        f'outbound="sip:{address}:{port};transport=tls"\n')
    with (directory / "client.log").open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            str(ROOT / ".tools/bin/baresip"), "-f", str(directory),
            stdout=output, stderr=output)
        try:
            await asyncio.sleep(53)
            assert process.returncode is None, (directory / "client.log").read_text()
            assert len(connections) == 1, (host, connections, (directory / "client.log").read_text())
            flow = connections[0]
            assert flow["registers"] == 1 and not flow["closed"], (host, flow)
            assert (flow["pings"] >= 2 if expected else flow["pings"] == 0), (host, flow)
            print(f"PASS {host}: {flow['pings']} pings, same verified TLS connection", flush=True)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            server.close()
            await server.wait_closed()
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result


async def main():
    # Baresip excludes loopback interfaces. Use this host's own interface IP;
    # these connections still stay in the kernel and never leave the machine.
    addresses = [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                 if not item[4][0].startswith('127.')]
    if addresses:
        address = addresses[0]  # Container hosts entry; works without an external route.
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))  # Route lookup only; sends no packet.
            address = route.getsockname()[0]
    with tempfile.TemporaryDirectory(prefix="voip-tls-test-") as temporary:
        directory = Path(temporary)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "key.pem"), "-out", str(directory / "cert.pem"),
            "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:" + address,
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.gather(
            check(directory / "twilio", "test.sip.twilio.com", True, address),
            check(directory / "other", "example.org", False, address),
            check(directory / "lookalike", "test.sip.twilio.com.example.org", False, address),
        )


if __name__ == "__main__":
    asyncio.run(main())
