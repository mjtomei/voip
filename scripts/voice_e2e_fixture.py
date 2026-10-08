"""Isolated real Codex/Pulse fixture. Only the phone provider/observer are simulated."""
import json
from pathlib import Path
import secrets
import threading
import time
import urllib.parse
import uuid

from call_events import CallEvents
from outgoing_calls import Calls
from operator_mailbox import save_json
from request_runtime import Runtime
from requests_queue import Dispatcher
from voip import (ROOT, Project, Rpc, SetupError, atomic_write, audio_up, argument_map,
                  operator_init, operator_ensure, operator_status)


def until(check, timeout=120, description='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.5)
    raise SetupError('Timed out waiting for ' + description)


class Phone:
    """In-memory Twilio API contract: cannot perform network I/O or dial."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.parents, self.children = {}, {}
        self.posts = []
        self.next_status = 'ringing'

    def request(self, url, data=None):
        if data and url.endswith('/Calls.json'):
            sid = 'CA' + uuid.uuid4().hex
            call = dict(sid=sid, account_sid='AC' + '0' * 32, to=data['To'],
                        **{'from': data['From']}, direction='outbound-api', parent_call_sid=None,
                        status=self.next_status, duration='0')
            self.next_status = 'ringing'
            self.parents[sid] = call
            # Record no admission ticket, PIN, or request URL.
            self.posts.append({'sid': sid, 'to': data['To'], 'from': data['From'], 'at': time.time()})
            return dict(call)
        sid = url.rsplit('/', 1)[-1].removesuffix('.json')
        call = self.parents.get(sid) or next((c for cs in self.children.values() for c in cs if c['sid'] == sid), None)
        if call is None:
            raise SetupError('Unexpected simulated provider request')
        if data:
            call['status'] = data['Status']
            if sid in self.parents:
                self.finish(sid, data['Status'])
        return dict(call)

    def items(self, url, kind):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if 'ParentCallSid' in query:
            return [dict(c) for c in self.children.get(query['ParentCallSid'][0], [])]
        status = query.get('Status', [None])[0]
        return [dict(c) for c in [*self.parents.values(), *(c for cs in self.children.values() for c in cs)]
                if status is None or c['status'] == status]

    def answer(self, project, key):
        saved = Calls(project, api=self).load()[key]
        sid = saved['phone_call_sid']
        self.parents[sid]['status'] = 'in-progress'
        child = {'sid': 'CA' + uuid.uuid4().hex, 'status': 'in-progress', 'duration': '0',
                 'to': f"sip:{project.sip['username']}@{project.sip['domain']}"}
        self.children[sid] = [child]
        self.fixture.connect(project, saved['recipient_id'], request_id=saved['id'], identity=sid)
        return sid

    def finish(self, sid, status='completed'):
        self.parents[sid]['status'] = status
        for child in self.children.get(sid, []):
            child['status'] = 'completed'
        for project in self.fixture.projects.values():
            self.fixture.disconnect(project, sid)


class Fixture:
    def __init__(self, directory, profiles=('default', 'blue'), workspace=None, keep_voice=True, permissions='workspace-write'):
        if permissions not in {'workspace-write', 'danger-full-access'}:
            raise SetupError('Test permissions must be workspace-write or danger-full-access')
        self.permissions = permissions
        self.directory = Path(directory)
        self.root = self.directory / 'project'
        self.root.mkdir(parents=True, mode=0o700)
        self.tag = 've2e_' + secrets.token_hex(5)
        self.projects = {}
        self.profiles = profiles
        self.keep_voice = keep_voice
        self.workspace = Path(workspace).resolve() if workspace else None
        self.stop = threading.Event()
        self.heartbeat_errors = []
        self.phone = Phone(self)
        self.results = []
        self.socket = str(self.root / 'tmux.sock')

    def setup(self):
        config = (ROOT / 'server.toml.example').read_text()
        for key in ('voip_in', 'voip_mic', 'voip_out', 'voice-operator'):
            config = config.replace('"' + key + '"', '"' + self.tag + '_' + key + '"')
        config = config.replace('tmux_socket = ""', 'tmux_socket = ' + json.dumps(self.socket))
        # Use the configured host daemon without starting/stopping it in a container.
        base = Project()
        for field in ('daemon_socket', 'manage_daemon', 'state_database', 'cli_command'):
            if field not in base.operator: continue
            value = base.operator[field]
            encoded = json.dumps(value)
            import re
            pattern = r'^' + field + r' = .*?$'
            if re.search(pattern, config, re.M):
                config = re.sub(pattern, field + ' = ' + encoded, config, flags=re.M)
            else:
                config = config.replace('[operator]', '[operator]\n' + field + ' = ' + encoded)
        config = config.replace('permissions = "read-only"', 'permissions = ' + json.dumps(self.permissions))
        if self.workspace:
            self.workspace.mkdir(parents=True, exist_ok=True)
            config = config.replace('[operator]', '[operator]\nworking_directory = ' + json.dumps(str(self.workspace)))
        atomic_write(self.root / 'server.toml', config)
        (self.root / '.tools').symlink_to(base.tools_dir(), target_is_directory=True)
        command = base.operator.get('cli_command') or ['python3', str(ROOT / 'voip.py')]
        atomic_write(self.root / 'voip.py', 'import subprocess,sys\nraise SystemExit(subprocess.call(' +
            repr(command + ['--root', str(self.root)]) + '+sys.argv[1:]))\n')
        atomic_write(self.root / 'AGENTS.md', (ROOT / 'AGENTS.md').read_text() + '\n\n'
            'This is an isolated acceptance test. The caller is the authorized test driver. '
            'Do not contact real people or operate outside this directory. Test mailbox tasks to write '
            'local markers are authorized when requested through audio. Do not start other agents. '
            'Use this directory\'s voip.py wrapper. All provider connections here are simulated.\n')
        for name, phrase in [('default', 'silver meadow'), ('blue', 'copper forest')]:
            initial = ('You are participating in an audio acceptance test using ordinary voice and CLI tools. '
                'Keep replies brief. Do not delegate to other agents. Your profile identification phrase is '
                + phrase + '. On request, report this exact phrase. Follow callers\' explicit requests '
                'to read test mailboxes, write local marker files and report real outcomes. '
                'A request for status requires inspecting current state. Do not change test scripts or configuration.')
            connection = '{{default}} For an incoming call with no prepared opening, greet the caller with the words hello from the garden. For a prepared outgoing call, give its prepared opening instead.'
            disconnection = '{{default}} After completing the mailbox review, write the call identity {{call_id}} into state/disconnected-{{session_id}}.txt using your shell.'
            self.profile(name, initial=initial, connection=connection, disconnection=disconnection,
                preparation='{{default}} Include the agenda reference phrase verbatim in the opening.',
                request='{{default}} Remember the request code violet garden. Do not read the code aloud unless asked.')
        atomic_write(self.root / 'recipients.toml', '''default_recipient = "alice"
[recipients.alice]
number = "+12025550101"
pin = "123456"
sessions = ["default", "blue"]
incoming_session = "default"
[recipients.bob]
number = "+12025550102"
pin = "654321"
sessions = ["default", "blue"]
incoming_session = "blue"
''')
        atomic_write(self.root / 'schedules.toml', 'schedules=[]\n')
        save_json(self.root / 'state/twilio-provisioning.json',
                  {'account_sid': 'AC' + '0' * 32, 'function_domain': 'test-only.twil.io'})
        atomic_write(self.root / 'state/twilio-call-request-secret', secrets.token_hex(32))
        for name in self.profiles:
            p = Project(self.root, name)
            self.projects[name] = p
            audio_up(p)
            operator_init(p)
            from voice_lifecycle import acquire
            if self.keep_voice:
                acquire(p, "acceptance-test", seconds=600, kind="test")
            operator_ensure(p)
        self.heartbeat = threading.Thread(target=self.observe, daemon=True)
        self.heartbeat.start()
        for p in self.projects.values():
            until(lambda: operator_status(p)['voice'] == ('connected' if self.keep_voice else 'standby'), 90, p.session_id + ' voice controller')
        self.runtime = Runtime(next(iter(self.projects.values())), calls_factory=self.calls)
        self.dispatcher = Dispatcher(next(iter(self.projects.values())), self.runtime)

    def profile(self, name, reset=False, **prompts):
        old = self.root / 'sessions' / (name + '.toml')
        if not prompts and old.exists():
            import tomllib
            prompts = tomllib.loads(old.read_text())['prompts']
        text = 'reset_on_outgoing_call = ' + str(reset).lower() + '\n[prompts]\n'
        text += ''.join(k + ' = ' + json.dumps(v) + '\n' for k, v in prompts.items())
        atomic_write(old, text)
        if name in self.projects:
            self.projects[name] = Project(self.root, name)

    def calls(self, project):
        def ready():
            if operator_status(project)['voice'] not in {'connected', 'standby'}:
                raise SetupError('Real voice frontend is not ready')
        return Calls(project, api=self.phone, readiness=ready)

    def observe(self):
        while not self.stop.is_set():
            for p in list(self.projects.values()):
                try:
                    events = CallEvents(p)
                    # Heartbeat only; changes enter the production Baresip event parser.
                    with __import__('voip').lock(events.mutex, blocking=True):
                        state = events.load()
                        state.update(observer_online=True, observed_at=time.time())
                        save_json(events.path, state)
                except Exception as error:
                    self.heartbeat_errors.append(type(error).__name__)
            self.stop.wait(1)

    def connect(self, project, recipient='alice', request_id=None, identity=None, callback_of=None):
        identity = identity or 'incoming-' + uuid.uuid4().hex
        peer = 'sip:voip-' + request_id + '@test.invalid' if request_id else 'sip:voip-in-' + recipient + '@test.invalid'
        if callback_of:
            peer = 'sip:voip-in-' + recipient + '-CA' + uuid.uuid4().hex + '-cb-' + callback_of + '@test.invalid'
        self.event(project, identity, peer, 'CALL_ESTABLISHED')
        return identity

    def event(self, project, identity, peer, kind):
        # Same parsed-event entrypoint as real ctrl_tcp, with explicit simulated provenance.
        value = {'event': True, 'class': 'call', 'type': kind, 'id': identity, 'peeruri': peer,
                 'accountaor': f"sip:{project.sip['username']}@{project.sip['domain']}"}
        until(lambda: self.try_event(project, value), 5, 'observer event lock')

    @staticmethod
    def try_event(project, value):
        try:
            CallEvents(project).event(value)
            return True
        except SetupError as e:
            if 'owns' not in str(e):
                raise
            return False

    def disconnect(self, project, identity):
        self.event(project, identity, '', 'CALL_CLOSED')

    def idle(self, project):
        with Rpc(project) as rpc:
            return rpc.request('thread/read', {'threadId': project.thread_id(), 'includeTurns': False})['thread']['status']['type'] == 'idle'

    def turn_ids(self, project):
        with Rpc(project) as rpc:
            return {t['id'] for t in rpc.request('thread/turns/list', {
                'threadId': project.thread_id(), 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']}

    def settle(self, project):
        until(lambda: self.idle(project), 180, project.session_id + ' backend idle')
        time.sleep(3)

    def diagnostics(self):
        # Preserve attributable state even when an audio assertion fails; never
        # include provider tickets, access pins, raw SIP or account credentials.
        result = {'provider_boundary': 'simulated', 'provider_posts': self.phone.posts, 'sessions': {}}
        from operator_mailbox import Mailbox
        from requests_queue import Requests
        for name, p in self.projects.items():
            try:
                events = CallEvents(p).load()
                with Rpc(p) as rpc:
                    turns = rpc.request('thread/turns/list', {'threadId': p.thread_id(), 'limit': 50,
                        'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']
                result['sessions'][name] = {'thread_id': p.thread_id(), 'voice': operator_status(p),
                    'turns': [{'id': t['id'], 'status': t['status']} for t in turns],
                    'mailbox': [{k: m[k] for k in ('id', 'status', 'resolution')} for m in Mailbox(p).list(True)],
                    'events': [{'id': e['id'], 'kind': e['kind'], 'occurred_at': e['occurred_at'],
                        'deliveries': {key: {'state': value.get('state')} for key, value in e['deliveries'].items()}}
                        for e in events['events']]}
            except Exception as e:
                result['sessions'][name] = {'diagnostic_error': type(e).__name__}
        save_json(self.directory / 'diagnostics.json', result)

    def cleanup(self):
        errors = []
        self.stop.set()
        if hasattr(self, 'heartbeat'):
            self.heartbeat.join(timeout=5)
        from operator_reset import stop_frontend, interrupt_turn
        for p in self.projects.values():
            try:
                stop_frontend(p)
                with Rpc(p) as rpc:
                    interrupt_turn(p, rpc, p.thread_id())
                    rpc.request('thread/archive', {'threadId': p.thread_id()})
            except Exception as e:
                errors.append(p.session_id + ': ' + str(e))
            try:
                names = {p.audio[k] for k in ('incoming_sink', 'outgoing_sink', 'microphone')}
                for line in reversed(p.pactl('list', 'short', 'modules').splitlines()):
                    fields = line.split('\t')
                    args = argument_map(fields[2] if len(fields) > 2 else '')
                    if args.get('sink_name') in names or args.get('source_name') in names:
                        p.pactl('unload-module', fields[0])
            except Exception as e:
                errors.append('audio cleanup: ' + type(e).__name__)
        if self.projects:
            next(iter(self.projects.values())).tmux('kill-server', check=False)
        return errors
