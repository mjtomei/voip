import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from voip import Project, ROOT, SetupError
from release_config import initialize, load_credentials, validate_server

class ReleaseInterfaces(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        initialize(self.root, '+12025550100', 'alice', '+12025550101')

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'voip.py'), '--root', str(self.root),
                               '--json', *args], capture_output=True, text=True)

    def test_clean_init_does_not_start_services_or_generate_a_pin(self):
        self.assertFalse((self.root/'state').exists())
        with self.assertRaises(SetupError): initialize(self.root, '+12025550100', 'alice', '+12025550101')
        self.assertEqual(Project(self.root).permission_params(), {'sandbox':'read-only', 'approvalPolicy':'never'})

    def test_session_permissions_override_and_explicit_host_daemon(self):
        p=self.root/'sessions/default.toml'
        p.write_text('permissions="workspace-write"\n')
        project=Project(self.root)
        self.assertEqual(project.permission_params()['sandbox'], 'workspace-write')
        p.write_text('permissions="danger-full-access"\n')
        self.assertEqual(Project(self.root).permissions(), 'danger-full-access')
        p.write_text('permissions="wrong"\n')
        with self.assertRaises(SetupError): Project(self.root)

    def test_explicit_tools_directory_overrides_mounted_workspace_tools(self):
        local = self.root / '.tools/bin/baresip'
        installed = self.root / 'image-tools/bin/baresip'
        for path in (local, installed):
            path.parent.mkdir(parents=True)
            path.write_text('test binary')
        with patch.dict(os.environ, {'VOIP_TOOLS_DIR': str(installed.parent.parent)}):
            self.assertEqual(Project(self.root).executable('baresip'), str(installed))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Project(self.root).executable('baresip'), str(local))

    def test_typo_is_rejected_with_field_and_file(self):
        p=self.root/'server.toml'
        p.write_text(p.read_text().replace('restart_delay_seconds', 'restart_delay_second'))
        r=self.cli('config','show')
        self.assertEqual(r.returncode,1)
        data=json.loads(r.stdout)
        self.assertFalse(data['ok'])
        self.assertIn('restart_delay_second',data['error']['message'])

    def test_credentials_are_literal_private_and_never_in_config_show(self):
        p=self.root/'credentials.env'
        secret='literal-$(not-a-command)'
        p.write_text('TWILIO_API_KEY_SECRET='+secret+'\n');p.chmod(0o600)
        project=Project(self.root)
        project.config['service']['credentials_file']='credentials.env'
        with patch.dict(os.environ,{},clear=True):
            load_credentials(project)
            self.assertEqual(os.environ['TWILIO_API_KEY_SECRET'],secret)
        p.chmod(0o644)
        with self.assertRaises(SetupError): load_credentials(project)
        r=self.cli('config','show')
        self.assertEqual(r.returncode,0)
        self.assertNotIn(secret,r.stdout)

    def test_json_mailbox_preserves_body_and_idempotency(self):
        args=('mailbox','post','--recipient','alice','--from','test','--subject','topic',
              '--body','First line\nSecond line','--key','stable','--notify','passive')
        first=self.cli(*args);second=self.cli(*args)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        self.assertEqual(json.loads(first.stdout)['data'],json.loads(second.stdout)['data'])
        result=json.loads(self.cli('mailbox','list','--recipient','alice').stdout)
        self.assertEqual(len(result['data']),1)
        self.assertEqual(result['data'][0]['body'],'First line\nSecond line')

    def test_config_preview_has_no_provider_mutation(self):
        result=self.cli('config','show')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['schema_version'],1)
        self.assertFalse((self.root/'state/twilio-provisioning.json').exists())

    def test_missing_dependencies_fail_doctor(self):
        from release_cli import diagnose
        project=Project(self.root)
        with patch.object(project,'executable',side_effect=SetupError('missing')):
            result=diagnose(project)
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks'][0]['ok'])

    def test_status_identifies_active_calls_and_filters_ended_calls(self):
        from call_events import CallEvents
        from release_cli import overview
        from operator_mailbox import save_json
        project = Project(self.root)
        events = CallEvents(project)
        state = events.load()
        events.transition(state, 'active-call', True, request_id='request-one', recipient_id='alice')
        events.transition(state, 'ended-call', True, recipient_id='alice')
        events.transition(state, 'ended-call', False)
        save_json(events.path, state)
        with patch('release_cli.operator_status', return_value={'voice_connected': False}):
            calls = overview(project)['sessions'][0]['calls']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['id'], 'active-call')
        self.assertEqual(calls[0]['outgoing_request_id'], 'request-one')
        self.assertEqual(calls[0]['recipient_id'], 'alice')

    def test_foreground_service_does_not_adopt_or_stop_an_existing_frontend(self):
        from service_runtime import serve
        with patch('service_runtime.signal.signal'), \
             patch('service_runtime.operator_status', return_value={'process_lock_held': True, 'codex_process_alive': True}), \
             patch('service_runtime.audio_up') as audio, \
             patch('operator_reset.stop_frontend') as stop:
            with self.assertRaisesRegex(SetupError, 'existing frontend'):
                serve(Project(self.root))
            audio.assert_not_called()
            stop.assert_not_called()
        self.assertFalse((self.root/'state/service-process.json').exists())

    def test_service_diagnostics_do_not_corrupt_json_stdout(self):
        from release_cli import entry
        def run_service(*args):
            print('Recovery pending for test session')
            return {'stopped': True}
        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['voip', '--root', str(self.root), '--json', 'service', 'run']), \
             patch('service_runtime.service', side_effect=run_service), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
            self.assertEqual(entry(), 0)
        self.assertEqual(json.loads(output.getvalue())['data'], {'stopped': True})
        self.assertIn('Recovery pending', diagnostic.getvalue())
