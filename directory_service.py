"""Optional directory operators. Public routes contain opaque identities, never paths."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time

from directory_routes import discover, directory_id, extensions
from voip import Project, SetupError, atomic_write, lock


def settings(project):
    values = project.config.get('directory_extensions', {})
    allowed = {'enabled', 'root', 'recipients', 'mode', 'tmux_socket', 'interval_seconds', 'snapshot_file'}
    if not isinstance(values, dict) or set(values) - allowed:
        raise SetupError('Unknown directory_extensions setting')
    result = {'enabled': False, 'root': str(Path.home()), 'recipients': [], 'mode': 'smart',
              'tmux_socket': '', 'interval_seconds': 15, 'snapshot_file': '', **values}
    if type(result['enabled']) is not bool or result['mode'] not in {'smart', 'sessions', 'panes'}:
        raise SetupError('Invalid directory discovery enable/mode setting')
    if any(not isinstance(result[k], str) for k in ('root', 'tmux_socket', 'snapshot_file')):
        raise SetupError('Directory discovery paths must be strings')
    root = Path(result['root']).expanduser()
    if not root.is_absolute(): raise SetupError('Directory discovery root must be absolute')
    result['root'] = str(root.resolve())
    people = result['recipients']
    if (not isinstance(people, list) or any(not isinstance(p, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', p) for p in people)
            or len(set(people)) != len(people) or (result['enabled'] and not people)):
        raise SetupError('Enable directory extensions for explicit recipient names')
    interval = result['interval_seconds']
    if type(interval) is not int or not 5 <= interval <= 60:
        raise SetupError('Directory refresh interval must be 5–60 seconds')
    return result


def policy(project):
    cfg = settings(project)
    if not cfg['enabled']: return None
    value = {'home': directory_id(cfg['root']), 'recipients': sorted(cfg['recipients'])}
    value['id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def index(project):
    path = project.provider_state / 'directory-index.json'
    return json.loads(path.read_text()) if path.exists() else {}


def home_target(project):
    """Resolve the configured home route, never the legacy profile name default."""
    provider = Project(project.provider_root)
    cfg = settings(provider)
    if not cfg['enabled']:
        raise SetupError('Home directory routing is not enabled')
    path = provider.state / 'directory-status.json'
    status = json.loads(path.read_text()) if path.exists() else {}
    if (not 0 <= time.time() - status.get('observed_at', 0) < max(90, cfg['interval_seconds'] * 3)
            or status.get('plan', {}).get('default') != cfg['root']):
        raise SetupError('Home directory route is not current; wait for directory discovery')
    route = status.get('targets', {}).get(directory_id(cfg['root']))
    if not route:
        raise SetupError('Home directory operator is unavailable')
    target = Project(provider.root, route['session'])
    if str(target.workspace) != cfg['root'] or target.sip['username'] != route['username']:
        raise SetupError('Home directory route does not match its configured operator')
    return target.session_id


def transfer_target(project, selector):
    """Public transfer names resolve once; stored session identities stay stable."""
    provider = Project(project.provider_root)
    if not settings(provider)['enabled']:
        return selector
    if selector == 'default':
        return home_target(provider)
    # Keep the original provider-directory operator reachable by its directory
    # name (for example voip). Explicit registered/generated sessions win.
    from voice_config import sessions
    if (selector == provider.workspace.name and selector not in sessions(provider)
            and selector not in index(provider)):
        return 'default'
    return selector


def generated_project(root, identity):
    if not re.fullmatch(r'dir_[a-f0-9]{24}', identity): return None
    path = Path(root) / 'state/directory-index.json'
    if not path.exists() or identity not in json.loads(path.read_text()): return None
    return Path(root) / 'state/directory-projects' / identity


def is_generated(project):
    return (project.root == project.provider_state / 'directory-projects' / project.session_id
            and project.session_id in index(project))


def snapshot(project, *, use_file=True):
    from voice_config import sessions
    cfg = settings(project)
    if use_file and cfg['snapshot_file']:
        path = (project.root / cfg['snapshot_file']).resolve()
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or not isinstance(data.get('observed_at'), (int, float))
                or data.get('root') != cfg['root'] or data.get('mode') != cfg['mode']
                or not 0 <= time.time() - data.get('observed_at', 0) <= 45):
            raise SetupError('Host tmux snapshot is stale or belongs to another discovery configuration')
        if (not isinstance(data.get('directories'), list) or any(not isinstance(row, dict)
                or not isinstance(row.get('directory'), str) for row in data['directories'])):
            raise SetupError('Invalid host tmux snapshot directories')
        return data
    excluded = [Project(project.root, sid).operator['session'] for sid in sessions(project)]
    excluded += [project.operator['session'] + '-' + sid for sid in index(project)]
    rows = discover(cfg['root'], socket=cfg['tmux_socket'], mode=cfg['mode'], exclude=excluded)
    return {'root': cfg['root'], 'mode': cfg['mode'], 'observed_at': time.time(), 'directories': rows}


def preview(project, observed=None):
    from service_config import extensions as manual_extensions
    cfg = settings(project)
    observed = observed or snapshot(project)
    manual = manual_extensions(project)
    result = extensions(cfg['root'], [d['directory'] for d in observed['directories']], reserved=manual)
    return {**result, 'manual': manual, 'discovery': observed, 'enabled': cfg['enabled']}


def ensure_project(provider, directory):
    """All generated metadata is private provider state; no files in worker directories."""
    from voice_config import sessions
    identity = directory_id(directory)
    records = index(provider)
    root = provider.provider_state / 'directory-projects' / identity
    if identity not in records:
        occupied = {Project(provider.root, sid).sip.get('control_port', 4444) for sid in sessions(provider)}
        occupied.update(row['port'] for row in records.values())
        port = 10000 + int(identity[4:12], 16) % 45000
        while port in occupied: port = 10000 + (port - 10000 + 1) % 45000
        if root.exists():
            raise SetupError('Unregistered directory metadata exists; inspect it before retrying initialization')
        record = {'directory': directory, 'port': port, 'created_at': time.time()}
        # Persist registration before writes so interruption can finish the same identity.
        records[identity] = record
        atomic_write(provider.provider_state / 'directory-index.json', json.dumps(records, indent=2)+'\n')
    elif records[identity]['directory'] != directory:
        raise SetupError('Directory identity does not match its saved workspace')
    cfg = settings(provider)
    files = {
        'server.toml': '[service]\nprovider_root = ' + json.dumps(str(provider.root)) +
            '\nsession_ids = [' + json.dumps(identity) + ']\n[operator]\nworking_directory = ' + json.dumps(directory) +
            '\ntmux_socket = ' + json.dumps(provider.operator.get('tmux_socket') or str(provider.provider_state / 'directory-tmux.sock')) +
            '\n[sip]\nusername = "dir"\n',  # Twilio caps full usernames at 32 characters.
        'recipients.toml': 'default_recipient = ' + json.dumps(cfg['recipients'][0]) + '\n',
        'schedules.toml': '# Directory schedules are optional.\n',
        'sessions/' + identity + '.toml': 'control_port = ' + str(records[identity]['port']) + '\n',
    }
    for name, content in files.items():
        path = root / name
        if not path.exists(): atomic_write(path, content)
    project = Project(root, identity)
    if project.workspace != Path(directory).resolve():
        raise SetupError('Saved directory configuration has a different working directory')
    return project


def public_map(provider, plan, targets, now=None):
    now = time.time() if now is None else now
    p = policy(provider)
    if not p: raise SetupError('Directory extensions are disabled')
    routes = {code: directory_id(path) for code, path in plan['extensions'].items()}
    body = {'v': 1, 'policy_id': p['id'], 'home': p['home'], 'routes': routes, 'targets': targets}
    body['generation'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    body['expires_at'] = int(now + max(90, settings(provider)['interval_seconds'] * 3))
    if len(json.dumps(body).encode()) > 15000:
        raise SetupError('Directory route document exceeds its 15 KB safety limit')
    return body


class Runtime:
    def __init__(self, provider, api=None):
        from outgoing_calls import api_client
        self.provider = provider
        self.api = api or api_client()
        self.deployment = json.loads((provider.provider_state / 'twilio-provisioning.json').read_text())
        from service_config import access_bytes
        from twilio_source import function_source
        from voip import ROOT
        if (self.deployment.get('access_asset', {}).get('sha256') != hashlib.sha256(access_bytes(provider)).hexdigest()
                or any(self.deployment.get('functions', {}).get(name, {}).get('sha256') !=
                       hashlib.sha256(function_source(ROOT, name)).hexdigest() for name in ('incoming', 'authorize', 'outgoing'))):
            raise SetupError('Deploy current directory routing with config apply --hosted-only before starting it')
        self.children, self.owned = {}, {}

    def publish(self, body):
        d = self.deployment
        if not re.fullmatch(r'IS[0-9a-fA-F]{32}', d.get('pin_sync_service_sid', '')):
            raise SetupError('Deploy the directory-capable hosted handlers first')
        base = 'https://sync.twilio.com/v1/Services/' + d['pin_sync_service_sid'] + '/Documents'
        url = base + '/directory-routes'
        try:
            current = self.api.request(url)
        except SetupError as error:
            if 'HTTP 404,' not in str(error): raise
            self.api.request(base, {'UniqueName': 'directory-routes', 'Data': json.dumps(body)})
        else:
            self.api.request(url, {'Data': json.dumps(body)}, revision=current['revision'])

    def start(self, project):
        from voip import audio_up, operator_init, operator_ensure, sip_configure
        sid = project.session_id
        if sid not in self.owned:
            if len(self.owned) >= 32:
                raise SetupError('32 directory operators are already owned; restart the idle directory service to release retired endpoints')
            d = self.deployment
            for key, pattern in [('account_sid', r'AC[0-9a-fA-F]{32}'), ('credential_list_sid', r'CL[0-9a-fA-F]{32}')]:
                if not re.fullmatch(pattern, d.get(key, '')): raise SetupError('Missing directory SIP provisioning prerequisites')
            secret = project.state / 'twilio-sip-secret'
            url = ('https://api.twilio.com/2010-04-01/Accounts/' + d['account_sid'] +
                   '/SIP/CredentialLists/' + d['credential_list_sid'] + '/Credentials.json')
            matches = [c for c in self.api.items(url+'?PageSize=1000', 'credentials') if c['username'] == project.sip['username']]
            if matches and not secret.exists(): raise SetupError('Directory SIP secret is missing; recover it before changing credentials')
            if not secret.exists(): atomic_write(secret, 'Aa9'+secrets.token_urlsafe(36)+'\n')
            if not matches: self.api.request(url, {'Username': project.sip['username'], 'Password': secret.read_text().strip()})
            audio_up(project)
            sip_configure(project, secret.read_text().strip())
            if not (project.state/'operator-thread-id').exists(): operator_init(project)
            self.owned[sid] = project
        operator_ensure(project)
        for kind, command in [('sip', ['sip', 'run']), ('monitor', ['sip', 'monitor']), ('requests', ['request', 'run'])]:
            key = (sid, kind)
            child = self.children.get(key)
            if child is None or child.poll() is not None:
                path = project.state / ('directory-' + kind + '.log')
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a') as log:
                    self.children[key] = subprocess.Popen(project.cli()+command, stdout=log, stderr=log)

    def ready(self, project):
        from call_events import CallEvents
        from voip import operator_status
        status = operator_status(project)
        events = CallEvents(project).load()
        from endpoint_health import voice_health, sip_health
        return (status['voice'] in {'connected', 'standby'} and status['codex_process_alive'] and
                events.get('observer_online') and time.time()-events.get('observed_at', 0) < 10
                and voice_health(project, status)['ok'] and sip_health(project)['ok'])

    def close(self):
        from operator_reset import stop_frontend
        for child in self.children.values():
            if child.poll() is None: child.terminate()
        for project in self.owned.values():
            try: stop_frontend(project)
            except SetupError: pass
        for child in self.children.values():
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


def tick(project, runtime):
    """Publish only ready endpoints. Missing workers remove new-call admission only."""
    from voice_config import sessions, session_recipients
    plan = preview(project)
    paths = {plan['default'], *plan['extensions'].values()}
    if len(paths) > 32: raise SetupError('At most 32 directory operators can be active at once')
    configured = {}
    for sid in sessions(project):
        p = Project(project.root, sid)
        configured.setdefault(str(p.workspace), []).append(p)
    targets, failures = {}, {}
    # Retiring a route removes admission, not supervision of calls/work already
    # using it. Only endpoints owned by this process are recovered here.
    for retired in list(runtime.owned.values()):
        if str(retired.workspace) not in paths:
            try: runtime.start(retired)
            except (SetupError, OSError, subprocess.SubprocessError):
                failures[retired.session_id] = 'Retired endpoint recovery pending; inspect private state'
    for path in sorted(paths):
        identity = directory_id(path)
        try:
            matches = configured.get(path, [])
            if len(matches) == 1 and set(settings(project)['recipients']) <= set(session_recipients(matches[0])[0]):
                p = matches[0]  # Reuse its prompts, project mailbox and existing supervisor.
            else:
                p = ensure_project(project, path)
                runtime.start(p)
            if runtime.ready(p): targets[identity] = {'session': p.session_id, 'username': p.sip['username']}
        except (SetupError, OSError, subprocess.SubprocessError):
            failures[identity] = 'Endpoint not ready; inspect its private logs/state before retrying'
    body = public_map(project, plan, targets)
    runtime.publish(body)
    result = {'observed_at': time.time(), 'generation': body['generation'], 'plan': plan, 'targets': targets,
              'ready': sorted(targets), 'pending': sorted(set(plan['sessions'].values())-set(targets)), 'errors': failures}
    atomic_write(project.provider_state/'directory-status.json', json.dumps(result, indent=2)+'\n')
    return result


def serve(project):
    from voice_config import recipients
    cfg = settings(project)
    if not cfg['enabled']: raise SetupError('Enable directory_extensions and choose its recipients before running it')
    if set(cfg['recipients']) - set(recipients(project)[0]): raise SetupError('Unknown directory recipient')
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with lock(project.provider_state/'directory-service.lock'):
        runtime = Runtime(project)
        try:
            while not stop.is_set():
                try: tick(project, runtime)
                except (SetupError, OSError, ValueError, subprocess.SubprocessError):
                    print('Directory refresh failed; previous cloud routes expire without renewal', flush=True)
                stop.wait(cfg['interval_seconds'])
        finally:
            try: runtime.publish(public_map(project, {'extensions': {}}, {}, now=0))
            except (SetupError, OSError): pass
            runtime.close()
    return {'stopped': True}
