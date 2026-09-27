"""Real Baresip, TLS registrar and observer; synthetic account, no provider/calls."""
import asyncio
import contextlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from call_events import monitor
from endpoint_health import sip_health
from voip import Project, ROOT


@unittest.skipUnless((ROOT/'.tools/bin/baresip').exists(), 'Requires locally built Baresip')
class SipHealthTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_forgotten_registration_and_failed_renewal_recover_without_calls(self):
        with tempfile.TemporaryDirectory(prefix='voip-sip-health-') as tmp:
            root = Path(tmp)
            addresses = [v[4][0] for v in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                         if not v[4][0].startswith('127.')]
            if addresses:
                address = addresses[0]
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(('192.0.2.1',9));address=s.getsockname()[0]
            with socket.socket() as s:
                s.bind(('127.0.0.1',0));control_port=s.getsockname()[1]
            config=(ROOT/'config.example.toml').read_text().replace('YOUR-DOMAIN.sip.twilio.com','health-test.sip.twilio.com')
            config=config.replace('control_port = 4444',f'control_port = {control_port}')
            (root/'config.toml').write_text(config)
            project=Project(root)
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key.pem'),
                            '-out',str(root/'cert.pem'),'-days','1','-subj','/CN=localhost',
                            '-addext','subjectAltName=IP:'+address],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(root/'cert.pem',root/'key.pem')
            state={'registered':False,'refuse':False,'accepted':0,'methods':set(), 'removed':0, 'dialogs':set()}
            connections=set()
            async def registrar(reader, writer):
                connections.add(asyncio.current_task())
                try:
                    while True:
                        first=await reader.readuntil(b'\r\n')
                        if first==b'\r\n':
                            await reader.readexactly(2);writer.write(b'\r\n');await writer.drain();continue
                        state['methods'].add(first.split()[0])
                        headers={}
                        while True:
                            line=await reader.readuntil(b'\r\n')
                            if line==b'\r\n':break
                            k,v=line.decode().split(':',1);headers.setdefault(k.lower(),[]).append(v.strip())
                        await reader.readexactly(int(headers.get('content-length',[0])[0]))
                        expire=int(headers.get('expires',['600'])[0])
                        contact=' '.join(headers.get('contact',[]))
                        deregister=expire==0 or ';expires=0' in contact
                        state['removed'] += int(deregister)
                        state['dialogs'].add(headers['call-id'][0])
                        refused=state['refuse'] and not deregister
                        if not refused:
                            state['registered']=not deregister
                            if not deregister:state['accepted']+=1
                        reply=['SIP/2.0 '+('503 Service Unavailable' if refused else '200 OK')]
                        for k in ['via','from','to','call-id','cseq','contact']:
                            reply.extend(k+': '+v for v in headers.get(k,[]))
                        reply+=['Content-Length: 0','','']
                        writer.write('\r\n'.join(reply).encode());await writer.drain()
                except (asyncio.IncompleteReadError, ConnectionError):pass
                finally:
                    writer.close()
            server=await asyncio.start_server(registrar,address,0,ssl=tls)
            port=server.sockets[0].getsockname()[1]
            sipdir=root/'baresip';sipdir.mkdir()
            (sipdir/'config').write_text(f'''poll_method epoll
sip_transports tls
sip_verify_server yes
sip_cafile {root/'cert.pem'}
module_path {ROOT/'.tools/lib/baresip/modules'}
ctrl_tcp_listen 127.0.0.1:{control_port}
module uuid.so
module_app account.so
module_app menu.so
module_app ctrl_tcp.so
''')
            (sipdir/'accounts').write_text(f'<sip:operator@health-test.sip.twilio.com;transport=tls>;sipnat=outbound;regint=600;outbound="sip:{address}:{port};transport=tls"\n')
            observer=None
            with (root/'client.log').open('w') as log:
                client=await asyncio.create_subprocess_exec(str(ROOT/'.tools/bin/baresip'),'-f',str(sipdir),stdout=log,stderr=log)
                async def wait_for(predicate):
                    async with asyncio.timeout(20):
                        while not predicate():
                            self.assertIsNone(client.returncode,(root/'client.log').read_text())
                            await asyncio.sleep(.05)
                try:
                    with patch('endpoint_health.REGISTRATION_REFRESH_SECONDS',2), \
                         patch('endpoint_health.REGISTRATION_RETRY_SECONDS',1), \
                         patch('endpoint_health.REGISTRATION_WAIT_SECONDS',3):
                        observer=asyncio.create_task(monitor(project))
                        await wait_for(lambda:sip_health(project)['ok'])
                        state['registered']=False  # Registrar forgets it; local cached status remains OK.
                        accepted=state['accepted']
                        await wait_for(lambda:state['accepted']>accepted and state['registered'])
                        await wait_for(lambda:sip_health(project)['ok'])
                        state['refuse']=True
                        await wait_for(lambda:not sip_health(project)['ok'])
                        state['refuse']=False
                        await wait_for(lambda:sip_health(project)['ok'] and state['registered'])
                        self.assertIsNone(client.returncode)
                        self.assertEqual(state['methods'],{b'REGISTER'})
                        self.assertEqual(state['removed'],0,'A health refresh must never unregister the endpoint')
                        self.assertEqual(len(state['dialogs']),1,'Refresh must retain the existing SIP dialog')
                finally:
                    if observer:
                        observer.cancel()
                        with contextlib.suppress(asyncio.CancelledError):await observer
                    if client.returncode is None:
                        client.terminate()
                        try:await asyncio.wait_for(client.wait(),5)
                        except asyncio.TimeoutError:client.kill();await client.wait()
                    server.close();await server.wait_closed()
                    for task in connections:task.cancel()
                    await asyncio.gather(*connections,return_exceptions=True)
