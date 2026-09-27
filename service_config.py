"""Validate and apply configuration without originating calls."""
import json
from pathlib import Path
import subprocess
import sys

from call_schedule import Scheduler
from voice_config import recipients, sessions, phone_number
from voip import Project, ROOT, SetupError, atomic_write, operator_init, operator_status, render_systemd, run


def access_config(project):
    people, _ = recipients(project, secrets=True)
    from pin_security import authentication
    people = {key: {**{k: v for k, v in p.items() if k != 'pin'},
                    'pin_auth': authentication(project, p)} for key, p in people.items()}
    if len({p['pin_auth']['tag'] for p in people.values()}) != len(people):
        raise SetupError('Use a different PIN for each recipient')
    profiles = {identity: Project(project.root, identity).sip['username'] for identity in sessions(project)}
    result = {'recipients': list(people.values()), 'sessions': profiles, 'extensions': extensions(project)}
    from directory_service import policy
    directory_policy = policy(project)
    if directory_policy:
        if set(directory_policy['recipients']) - set(people): raise SetupError('Unknown directory recipient')
        result['directories'] = directory_policy
    return result


def extensions(project):
    routes = {}
    for identity in sessions(project):
        extension = Project(project.root, identity).profile['extension']
        if extension is not None:
            if extension in routes:
                raise SetupError('Session extensions must be unique across the provider and registered projects')
            routes[extension] = identity
    return routes


def access_bytes(project):
    return json.dumps(access_config(project), sort_keys=True, separators=(',', ':')).encode()


def check_config(project):
    identities = sessions(project)
    people, default = recipients(project, secrets=True)
    ports, names, threads = set(), set(), set()
    for identity in identities:
        scoped = Project(project.root, identity)
        if (scoped.state / 'operator-thread-id').exists():
            thread = scoped.thread_id()
            if thread in threads:
                raise SetupError('Session profiles must not share a native operator thread')
            threads.add(thread)
        port = scoped.sip.get('control_port', 4444)
        if port in ports:
            raise SetupError('Session control ports collide; set an explicit control_port in a session profile')
        ports.add(port)
        for value in (scoped.operator['session'], *[scoped.audio[k] for k in ('incoming_sink','outgoing_sink','microphone')]):
            if value in names:
                raise SetupError('Session audio or process names collide')
            names.add(value)
    schedules = Scheduler(project).definitions()
    project_roots = {project.root}
    for identity in identities:
        scoped = Project(project.root, identity)
        if scoped.root not in project_roots:
            project_roots.add(scoped.root)
            schedules.extend(Scheduler(scoped).definitions())
    return {'server_config': str(project.server_path), 'twilio_number': phone_number(project),
            'sessions': identities, 'extensions': extensions(project), 'default_recipient': default,
            'recipients': [{k: v for k, v in p.items() if k not in {'pin', 'pin_auth'}} for p in people.values()],
            'enabled_schedules': [s['id'] for s in schedules]}


def dispatcher_units(project, identities):
    units = ['voip-requests.service']
    seen = {project.root}
    for identity in identities:
        scoped = Project(project.root, identity)
        if scoped.root not in seen:
            seen.add(scoped.root)
            first = scoped.config['service']['session_ids'][0]
            units.append(Project(scoped.root, first).service_name('requests') + '.service')
    return units


def apply_config(project, hosted_only=False):
    if project.root != project.provider_root:
        raise SetupError('Provider deployment must use the installed project directory')
    checked = check_config(project)
    if hosted_only:
        deploy_hosted(project)
        return {**checked, 'applied': True, 'hosted_only': True}
    from call_events import CallEvents
    def connected(scoped):
        return any(c['connected'] for c in CallEvents(scoped).load()['calls'].values())
    if any(connected(Project(project.root, identity)) for identity in checked['sessions']):
        raise SetupError('A phone call is connected; apply configuration after it ends')
    workers = dispatcher_units(project, checked['sessions'])
    running = [unit for unit in workers if run(['systemctl', '--user', 'is-active', '--quiet', unit], check=False).returncode == 0]
    if running:
        run(['systemctl', '--user', 'stop', *running])
    try:
        return install_config(project, checked, connected)
    finally:
        if running:
            run(['systemctl', '--user', 'start', *running], check=False)


def install_config(project, checked, connected):
    if any(connected(Project(project.root, identity)) for identity in checked['sessions']):
        raise SetupError('A phone call connected during admission; configuration was not applied')
    # Provider configuration is staged and verified before starting new local endpoints.
    deploy_hosted(project)
    render_systemd(Project(project.root))
    destination = Path.home() / '.config/systemd/user'
    destination.mkdir(parents=True, exist_ok=True)
    for source in (project.root / 'systemd/generated').glob('voip-*'):
        atomic_write(destination / source.name, source.read_text(), 0o644)
    run(['systemctl', '--user', 'daemon-reload'])
    deferred = []
    for identity in checked['sessions']:
        scoped = Project(project.root, identity)
        if connected(scoped):
            deferred.append(identity)
            continue
        if not (scoped.state / 'operator-thread-id').exists():
            operator_init(scoped)
        units = [scoped.service_name(kind) + suffix for kind, suffix in (
            ('audio', '.timer'), ('operator', '.timer'), ('baresip', '.service'), ('call-events', '.service'))]
        run(['systemctl', '--user', 'enable', '--now', *units])
        run(['systemctl', '--user', 'restart', scoped.service_name('baresip') + '.service',
             scoped.service_name('call-events') + '.service'])
        if operator_status(scoped)['codex_process_alive']:
            from operator_reset import reset_operator
            reset_operator(scoped, 'frontend')
        else:
            from voip import audio_up, operator_ensure
            audio_up(scoped)
            # Enabling the recovery timer can start this same check first.
            # Wait for that owned activation rather than fail a valid apply.
            operator_ensure(scoped, wait=True)
    run(['systemctl', '--user', 'enable', '--now', *dispatcher_units(project, checked['sessions'])])
    from directory_service import settings
    if settings(project)['enabled']:
        run(['systemctl', '--user', 'enable', '--now', 'voip-directories.service'])
    return {**checked, 'applied': not deferred, 'reload_after_call': deferred,
            'dispatcher_enabled': True}


def deploy_hosted(project):
    for action in ('prepare', 'route'):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/twilio-setup.py'), action,
                                 '--root', str(project.root)], cwd=project.root, timeout=420, stdout=sys.stderr, stderr=sys.stderr)
        if result.returncode:
            raise SetupError('Twilio configuration did not finish; local service changes were not applied')
