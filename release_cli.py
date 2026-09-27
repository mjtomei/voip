"""Stable CLI entry point around the existing local service interfaces."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from voip import Project, ROOT, Rpc, SetupError, atomic_write, operator_status

def overview(project):
    from call_events import CallEvents
    from call_schedule import Scheduler
    from requests_queue import Requests, FINISHED
    from voice_config import sessions
    profiles, projects = [], {}
    for identity in sessions(Project(project.provider_root)):
        scoped = Project(project.provider_root, identity)
        events = CallEvents(scoped).load()
        profiles.append({'id': identity, 'root': str(scoped.root), **operator_status(scoped),
                         'health': __import__('endpoint_health').summary(scoped),
                         'calls': [{'id': call_id, **{k: c.get(k) for k in ('recipient_id', 'connected', 'seen_at', 'outgoing_request_id')}}
                                   for call_id, c in events['calls'].items() if c['connected']],
                         'observer_online': events.get('observer_online'),
                         'observed_at': events.get('observed_at')})
        if str(scoped.root) not in projects:
            projects[str(scoped.root)] = {
                'pending_requests': [{k: r.get(k) for k in ('key', 'state', 'waiting_for', 'next_attempt_at')}
                                     for r in Requests(scoped).load().values() if r['state'] not in FINISHED],
                'upcoming_schedules': Scheduler(scoped).next(5)}
    return {'sessions': profiles, 'projects': projects}

def diagnose(project, live=False):
    from release_config import load_credentials
    checks = []
    def check(name, operation, remedy):
        try:
            detail = operation()
            checks.append({'name': name, 'ok': True, 'detail': detail})
        except (SetupError, OSError, ValueError, ImportError):
            checks.append({'name': name, 'ok': False, 'remedy': remedy})
    for binary in ('tmux', 'pactl', 'parec', 'pacat', 'baresip'):
        check(binary, lambda b=binary: project.executable(b), 'Run the installation steps for this dependency')
    check('audio', lambda: project.pactl('info').splitlines()[0], 'Start PipeWire/PulseAudio; in containers use the entrypoint')
    def dependencies():
        import aiortc, cryptography, websockets
        return {'aiortc': aiortc.__version__, 'cryptography': cryptography.__version__, 'websockets': websockets.__version__}
    check('python_dependencies', dependencies, 'Use bin/voip or the managed Python environment')
    def credentials():
        load_credentials(project)
        from outgoing_calls import api_client
        api_client()
        return 'Twilio environment is present; values omitted; account validity not contacted'
    check('credentials', credentials, 'Configure TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET')
    if project.operator.get('manage_daemon', True):
        check('codex_binary', lambda: project.executable(project.operator['codex']), 'Install the tested Codex CLI or configure an external daemon socket')
    if live:
        def daemon():
            with Rpc(project): pass
            return 'initialize succeeded; native voice availability requires the separate audio probe'
        check('daemon', daemon, 'Start the host Codex daemon and verify socket permissions/mounts')
        def sip():
            from call_events import CallEvents
            import time
            state = CallEvents(project).load()
            if not state.get('observer_online') or time.time() - state.get('observed_at', 0) > 10:
                raise SetupError('SIP observer unavailable')
            return 'Observer is current; confirm provider registration with scripts/check-twilio-sip.py'
        check('sip_observer', sip, 'Start the service; run the SIP registration check')
        def transport_health(kind):
            from endpoint_health import summary
            result = summary(project)[kind]
            if not result['ok']: raise SetupError(result['reason'])
            return result
        check('sip_registration', lambda: transport_health('sip'), 'Inspect operator health and the SIP observer; wait for a fresh registrar acknowledgement')
        check('voice_transport', lambda: transport_health('voice'), 'Inspect operator health and the frontend recovery record')
        check('saved_thread', project.thread_id, 'Initialize or adopt the intended operator conversation')
    return {'ok': all(c['ok'] for c in checks), 'checks': checks,
            'live_checks': live, 'voice_probe': 'scripts/test-voice.py run (explicit model/audio test)'}

def entry():
    args = []
    for value in sys.argv[1:]:
        if any(value.startswith(flag+'=') for flag in ('--root','--session','--recipient','--to')):
            args.extend(value.split('=',1))
        else: args.append(value)
    machine = '--json' in args
    args = [x for x in args if x != '--json']
    # Scope flags have the same position-independent meaning for every command.
    root, session = Path(os.environ.get('VOIP_ROOT', str(ROOT))), 'default'
    rest, i = [], 0
    while i < len(args):
        if args[i] in ('--root', '--session') and i + 1 < len(args):
            if args[i] == '--root': root = Path(args[i+1])
            else: session = args[i+1]
            i += 2
        else:
            rest.append(args[i]); i += 1
    args = rest
    if '--help' in args and args and args[0] in {'status','doctor','service','config','directories'}:
        help_text = {
            'status': 'status [--json]: sessions, calls, pending requests and upcoming schedules',
            'doctor': 'doctor [--live] [--json]: dependencies/configuration; optionally inspect the host daemon and observer',
            'service': 'service run|start|stop|status|enable|disable [--json]: own only this phone stack',
            'config': 'config check|show|plan|apply [--hosted-only] [--json]: inspect, preview or explicitly deploy configuration',
            'directories': 'directories preview|status|run [--json]: inspect active tmux directories or run their voice supervisor',
        }
        print('voip '+help_text[args[0]]+'\nGlobal scope: --root DIRECTORY --session NAME')
        return 0
    try:
        if args[:1] == ['init']:
            p = argparse.ArgumentParser(prog='voip init')
            p.add_argument('--number', required=True)
            p.add_argument('--recipient', required=True)
            p.add_argument('--destination', required=True)
            p.add_argument('--daemon-socket')
            a = p.parse_args(args[1:])
            from release_config import initialize
            data = initialize(root, a.number, a.recipient, a.destination, a.daemon_socket)
            code = 0
        elif args[:1] in (['status'], ['doctor'], ['service'], ['directories']) or args[:2] in (['config', 'show'], ['config', 'plan'], ['config', 'apply']):
            project = Project(root, session)
            from release_config import load_credentials
            load_credentials(project)
            code = 0
            if args[0] == 'directories':
                from directory_service import preview, serve
                if args[1:] == ['preview']: data = preview(project)
                elif args[1:] == ['status']:
                    path = project.provider_state / 'directory-status.json'
                    data = json.loads(path.read_text()) if path.exists() else {'observed_at': None, 'ready': []}
                elif args[1:] == ['run']:
                    with contextlib.redirect_stdout(sys.stderr): data = serve(project)
                else: raise SetupError('Use directories preview|status|run')
            elif args[0] == 'status':
                if len(args) != 1: raise SetupError('status accepts only --root, --session and --json')
                data = overview(project)
            elif args[0] == 'doctor':
                if any(x != '--live' for x in args[1:]): raise SetupError('doctor accepts --live and --json')
                data = diagnose(project, '--live' in args)
                code = 0 if data['ok'] else 1
            elif args[0] == 'service':
                from service_runtime import service
                with contextlib.redirect_stdout(sys.stderr):
                    data = service(project, args[1:])
            else:
                from release_config import effective, plan
                from service_config import apply_config
                if any(x != '--hosted-only' for x in args[2:]): raise SetupError('Unsupported configuration option')
                hosted = '--hosted-only' in args
                if args[1] == 'show': data = effective(project)
                elif args[1] == 'plan': data = plan(project, hosted)
                else:
                    with contextlib.redirect_stdout(sys.stderr):
                        data = apply_config(project, hosted_only=hosted)
        else:
            if not args or args == ['--help']:
                print('Additional commands: init, status, doctor [--live], config show|plan|apply, service run|start|stop|enable|disable|status, directories preview|status|run\n'
                      'Global options: --root DIRECTORY, --session NAME, --json\n')
            elif args[0] != '--help':
                from release_config import load_credentials
                load_credentials(Project(root, session))
            if machine and args[:1] == ['mailbox'] and '--recipient' not in args and '--to' not in args:
                raise SetupError('Machine-readable mailbox commands require an explicit --recipient or --to')
            old = sys.argv
            sys.argv = [old[0], '--root', str(root), '--session', session, *args]
            from voip import main
            import voip
            voip.JSON_OUTPUT = machine
            try:
                if not machine: return main()
                if args[:2] == ['pin', 'generate'] and ('print' in args or '--delivery' not in args):
                    raise SetupError('PIN print delivery is a private-terminal operation; omit --json')
                output = io.StringIO()
                with contextlib.redirect_stdout(output): code = main() or 0
                text = output.getvalue().strip()
                try: data = json.loads(text)
                except ValueError:
                    data = {'message': text}
            finally:
                sys.argv = old
                voip.JSON_OUTPUT = False
        print(json.dumps({'schema_version': 1, 'ok': code == 0, 'data': data} if machine else data, indent=2))
        return code
    except (SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Never print arbitrary provider data or credential file contents.
        message = str(error) if isinstance(error, SetupError) else 'Unable to complete operation; check configuration, paths and dependencies'
        if machine:
            print(json.dumps({'schema_version': 1, 'ok': False, 'error': {'code': 'operation_failed', 'message': message}}))
        else: print('Error: ' + message, file=sys.stderr)
        return 1
