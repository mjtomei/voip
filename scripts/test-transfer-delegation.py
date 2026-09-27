#!/usr/bin/env python3
"""Opt-in native TTS/STT check: a transfer request must reach the CLI.

Simulates arrival at a session, then speaks a request to return home. The isolated
project deliberately has no home route. Success requires an audited --to-home
CLI attempt AND speech reporting failure. No provider call or redirect is made;
this tests voice delegation, not successful telephony transfer.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import call_key
from operator_mailbox import save_json
from voice_acceptance import Capture, ask
from voice_e2e_fixture import Fixture, until
from voip import atomic_write, operator_status


class AuditedFixture(Fixture):
    def profile(self, name, **kwargs):
        super().profile(name, **kwargs)
        if name != 'default':
            return
        # setup() calls profile after writing its CLI wrapper and before starting
        # the frontend. Keep the audit on the host-side wrapper in container runs.
        audit = self.root / 'state/transfer-cli-attempt.json'
        wrapper = self.root / 'voip.py'
        original = wrapper.read_text()
        atomic_write(wrapper, 'import json,sys\nfrom pathlib import Path\n'
            'if "call" in sys.argv and any(x in sys.argv for x in ("start","hangup","cancel","transfer")):\n'
            '    if "transfer" not in sys.argv or "--to-home" not in sys.argv:\n'
            '        raise SystemExit("Test fixture refuses provider mutations")\n'
            '    Path(' + repr(str(audit)) + ').write_text(json.dumps(sys.argv[1:]))\n'
            + original)
        config = self.root / 'server.toml'
        import re
        text = re.sub(r'^cli_command = .*\n', '', config.read_text(), flags=re.M)
        text = text.replace('[operator]', '[operator]\ncli_command = ' +
                            json.dumps(['python3', str(wrapper)]))
        atomic_write(config, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=int, default=90)
    parser.add_argument('--permissions', choices=['workspace-write', 'danger-full-access'], default='workspace-write',
                        help='Host tool permissions; container CLI via Docker requires explicit danger-full-access')
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this uses native voice/account capacity')
    output = (args.output or ROOT / 'state/transfer-delegation' / time.strftime('%Y%m%d-%H%M%S')).resolve()
    fixture = AuditedFixture(output, profiles=('default',), keep_voice=False, permissions=args.permissions)
    result = {'boundary': 'Real Codex/TTS/WebRTC/Pulse/STT; simulated arrival; no telephony',
              'permissions': args.permissions, 'checks': {}, 'errors': []}

    def attempted():
        if not fixture.projects:
            return False
        audit = fixture.projects['default'].state / 'transfer-cli-attempt.json'
        if not audit.exists():
            return False
        command = json.loads(audit.read_text())
        return ('--to-home' in command and '--call-id' in command and
                command[command.index('--call-id') + 1] == call_key('simulated-transfer-arrival'))

    try:
        fixture.setup()
        project = fixture.projects['default']
        identity = 'simulated-transfer-arrival'
        transfer_id = str(uuid.uuid4())
        save_json(project.provider_state / 'call-transfers.json', {'arriving': {
            'id': transfer_id, 'source_session': 'blue', 'target_session': 'default',
            'target_thread': project.thread_id(), 'recipient_id': 'alice',
            'call_sid': 'CA' + '0' * 32, 'created': time.time(),
            'note': 'The test caller asked to reach this project.'}})
        with Capture(project, output / 'arrival') as capture:
            fixture.connect(project, 'alice', request_id=transfer_id, identity=identity)
            until(lambda: operator_status(project)['voice'] == 'connected', 90, 'native voice startup')
            try:
                capture.verify(['transfer complete'], timeout=45)
                result['checks']['arrival_confirmation'] = True
            except Exception as error:
                # A missed greeting remains a failure; still test the independent
                # caller request rather than treating it as a delegation failure.
                result['checks']['arrival_confirmation'] = False
                result['errors'].append('Arrival: ' + str(error))

        ask(project, output / 'return-home', 'Can you transfer me to the home operator?',
            [r'home', r'can t|cannot|couldn t|didn t (?:go through|work)|unable|unavailable|disabled|(?:not|isn t) (?:available|enabled|configured)|no home'],
            forbidden=['transfer complete', 'you re now with the home', 'done'],
            predicate=attempted, timeout=args.timeout)
        result['checks']['home_cli_attempt'] = attempted()
        result['checks']['spoken_failure'] = True
    except Exception as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
        # Preserve independent dispatch evidence even if the speech assertion fails.
        result['checks']['home_cli_attempt'] = attempted()
        fixture.diagnostics()
        result['cleanup_errors'] = fixture.cleanup()
        result['passed'] = (len(result['checks']) == 3 and all(result['checks'].values())
                            and not result['errors'] and not result['cleanup_errors'])
        save_json(output / 'results.json', result)
        print(json.dumps(result, indent=2), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
