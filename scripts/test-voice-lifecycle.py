#!/usr/bin/env python3
"""Opt-in native Codex lifecycle acceptance; simulated phone events, no dialing.

Creates and cleans up one isolated test conversation. Uses real TTS, WebRTC,
Pulse audio and independent STT. Speech failures remain failures but do not skip
the remaining disconnect checks. Recordings and state stay in the output folder.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import call_key
from voice_acceptance import Capture, ask
from voice_e2e_fixture import Fixture, until
from voip import SetupError, atomic_write, operator_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize a bounded native test using the local Codex account')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--permissions', choices=['workspace-write', 'danger-full-access'], default='workspace-write',
                        help='Host tool permissions; container CLI via Docker requires explicit danger-full-access')
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this uses native voice/account capacity')
    output = args.output or ROOT / 'state/lifecycle' / time.strftime('%Y%m%d-%H%M%S')
    fixture = Fixture(output, profiles=('default',), keep_voice=False, permissions=args.permissions)
    result = {'boundary': 'Real Codex/WebRTC/Pulse; simulated phone observation; no provider calls',
              'timing_basis': 'Local monotonic controller observations, not audible latency or saved speech timestamps',
              'permissions': args.permissions, 'checks': {}, 'errors': []}
    checks = result['checks']
    try:
        fixture.setup()
        project = fixture.projects['default']
        thread = project.thread_id()
        time.sleep(3)
        checks['idle_standby'] = operator_status(project)['voice'] == 'standby'
        for attempt in (1, 2):
            identity = 'lifecycle-' + str(attempt)
            prefix = str(attempt) + '_'
            with Capture(project, output / (prefix + 'greeting')) as capture:
                started = time.monotonic()
                fixture.connect(project, 'alice', identity=identity)
                until(lambda: operator_status(project)['voice'] == 'connected', 90, 'native voice startup')
                result[prefix + 'startup_seconds'] = round(time.monotonic() - started, 2)
                checks[prefix + 'startup'] = True
                try:
                    capture.verify(['hello from the garden'], timeout=30)
                    checks[prefix + 'spoken_greeting'] = True
                except SetupError as error:
                    checks[prefix + 'spoken_greeting'] = False
                    result['errors'].append(prefix + 'greeting: ' + str(error))
            time.sleep(2)
            try:
                ask(project, output / (prefix + 'tts'),
                    'Please say exactly the green notebook is ready.',
                    ['the green notebook is ready'], timeout=35)
                checks[prefix + 'tts_stt'] = True
            except SetupError as error:
                checks[prefix + 'tts_stt'] = False
                result['errors'].append(prefix + 'TTS/STT: ' + str(error))
            started = time.monotonic()
            fixture.disconnect(project, identity)
            until(lambda: operator_status(project)['voice'] == 'standby', 15, 'voice disconnect')
            result[prefix + 'disconnect_seconds'] = round(time.monotonic() - started, 2)
            checks[prefix + 'disconnect'] = True
            checks[prefix + 'same_thread'] = project.thread_id() == thread
            marker = project.state / 'disconnected-default.txt'
            until(lambda: marker.exists() and marker.read_text().strip() == call_key(identity),
                  60, 'hangup review through text')
            checks[prefix + 'text_review'] = True
            time.sleep(3)
            checks[prefix + 'stays_idle'] = operator_status(project)['voice'] == 'standby'
            print('Completed lifecycle cycle ' + str(attempt), flush=True)
    except Exception as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
        try:
            fixture.diagnostics()
            project = fixture.projects.get('default')
            if project:
                panes = project.tmux('list-panes', '-a', '-F', '#{pane_id}', check=False).stdout.splitlines()
                for index, pane in enumerate(panes):
                    atomic_write(output / ('frontend-' + str(index) + '.log'),
                                 project.tmux('capture-pane', '-p', '-S', '-300', '-t', pane, check=False).stdout)
        except Exception as error:
            result['errors'].append('diagnostics: ' + type(error).__name__)
        result['cleanup_errors'] = fixture.cleanup()
        result['passed'] = bool(checks) and all(checks.values()) and not result['errors'] and not result['cleanup_errors']
        atomic_write(output / 'results.json', json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
