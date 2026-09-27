"""Own the phone infrastructure processes; never supervise host worker agents."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from voip import (ROOT, Project, SetupError, atomic_write, audio_up, lock,
                  operator_ensure, operator_init, operator_status, process_identity, render_systemd, run)

def record(project):
    path = project.shared_state / 'service-process.json'
    try: state = json.loads(path.read_text())
    except (OSError, ValueError): return {'running': False}
    state['running'] = bool(state.get('starttime') and process_identity(state.get('pid')) == state['starttime'])
    return state

def serve(project):
    from voice_config import sessions
    from operator_reset import stop_frontend
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    children, profiles, jobs = {}, [], {}
    path = project.shared_state / 'service-process.json'
    with lock(project.shared_state / 'service-process.lock'):
        atomic_write(path, json.dumps({'pid': os.getpid(), 'starttime': process_identity(os.getpid()), 'root': str(project.root)}))
        try:
            roots = set()
            for identity in sessions(project):
                p = Project(project.root, identity)
                if p.operator.get('frontend') != 'voice':
                    raise SetupError('Foreground service requires voice frontend profiles')
                current = operator_status(p)
                if current["process_lock_held"] or current["codex_process_alive"]:
                    raise SetupError("An existing frontend already owns " + identity + "; stop its supervisor before starting the stack")
                profiles.append(p)
                audio_up(p)
                if not (p.state / 'operator-thread-id').exists(): operator_init(p)
                operator_ensure(p)
                for kind in ('run', 'monitor'):
                    jobs[(identity, kind)] = p.cli() + ['sip', kind]
                if p.root not in roots:
                    roots.add(p.root)
                    jobs[(identity, 'requests')] = p.cli() + ['request', 'run']
            last_ensure = 0
            from directory_service import settings
            if settings(project)['enabled']:
                jobs[('directories', 'supervisor')] = project.cli() + ['directories', 'run']
            while not stop.is_set():
                for key, command in jobs.items():
                    child = children.get(key)
                    if child is None or child.poll() is not None:
                        log_path = project.shared_state / 'logs' / ('-'.join(key) + '.log')
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                        with os.fdopen(fd, 'a') as log:
                            children[key] = subprocess.Popen(command, stdout=log, stderr=log, cwd=project.root)
                if time.monotonic() - last_ensure >= 5:
                    for p in profiles:
                        try:
                            audio_up(p)
                            operator_ensure(p)
                        except SetupError:
                            print('Recovery pending for session ' + p.session_id, flush=True)
                    last_ensure = time.monotonic()
                stop.wait(3)
        finally:
            # Stop dispatch before taking audio/SIP down. Saved requests remain durable.
            ordered = sorted(children, key=lambda k: k[1] != 'requests')
            for key in ordered:
                child = children[key]
                if child.poll() is None: child.terminate()
            for p in profiles:
                try: stop_frontend(p)
                except SetupError: pass  # Ownership checks refuse foreign processes.
            for child in children.values():
                try: child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
            path.unlink(missing_ok=True)
    return {'stopped': True, 'pending_requests': 'preserved'}

def service(project, arguments):
    if len(arguments) != 1 or arguments[0] not in {'run', 'start', 'stop', 'status', 'enable', 'disable'}:
        raise SetupError('Use service run|start|stop|status|enable|disable')
    action = arguments[0]
    state = record(project)
    if action == 'status': return state
    if action == 'run': return serve(project)
    if action == 'start':
        if state['running']: return state
        directory = project.shared_state / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / 'service.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            child = subprocess.Popen(project.cli() + ['service', 'run'], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            if child.poll() is not None: raise SetupError('Service failed to start; inspect private state/logs/service.log')
            state = record(project)
            if state['running']: return state
            time.sleep(.1)
        raise SetupError('Service start outcome pending; inspect service status before retrying')
    if action == 'stop':
        if state['running']:
            pid = state['pid']
            command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(ROOT / 'voip.py').encode() not in command or b'service' not in command or b'run' not in command:
                raise SetupError('Cannot verify service process ownership')
            fd = os.pidfd_open(pid)
            try:
                if process_identity(pid) != state['starttime']: raise SetupError('Service identity changed; inspect status')
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            finally: os.close(fd)
            for _ in range(150):
                if not record(project)['running']: return {'stopped': True, 'pending_requests': 'preserved'}
                time.sleep(.2)
            return {'stopping': True}
        return {'stopped': True}
    # One foreground supervisor is the same runtime used by containers.
    unit = 'voip-stack-' + __import__('hashlib').sha256(str(project.root).encode()).hexdigest()[:12] + '.service'
    destination = Path.home() / '.config/systemd/user' / unit
    if action == 'disable':
        run(['systemctl', '--user', 'disable', '--now', unit])
        return {'startup_enabled': False, 'unit': unit}
    quote = lambda value: json.dumps(str(value).replace('%', '%%').replace('$', '$$'))
    command = ' '.join(quote(x) for x in project.cli() + ['service', 'run'])
    atomic_write(destination, '[Unit]\nDescription=Voice phone service\nAfter=network-online.target pipewire-pulse.service\n'
        '[Service]\nExecStart=' + command + '\nEnvironment=' + quote('PATH=' + os.environ.get('PATH', '/usr/bin:/bin')) +
        '\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=60\n[Install]\nWantedBy=default.target\n', 0o644)
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', unit])
    return {'startup_enabled': True, 'unit': unit, 'logout': 'Enable user lingering if required by your host'}
