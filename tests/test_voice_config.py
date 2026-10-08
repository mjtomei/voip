import json
from unittest.mock import patch
from service_fixture import ServiceFixture
from service_config import check_config, access_bytes
from voice_config import prompt, recipients, resolve_recipient, operator_thread_name
from voip import Project, SetupError, render_systemd
from operator_mailbox import Mailbox
from outgoing_calls import Calls
from call_events import CallEvents

class ConfigTests(ServiceFixture):
    def test_thread_name_uses_allowed_default_and_never_reads_pin(self):
        (self.root / 'recipients.toml').write_text(self.contacts.replace(
            'pin = "123456"', 'pin_file = "not-created-yet"'))
        self.assertEqual(operator_thread_name(self.team),
                         'Phone operator: Alice — ' + self.root.name + ' (team)')
        # A shared profile without a preferred recipient lists both identities.
        (self.root / 'recipients.toml').write_text(self.contacts.replace(
            'default_recipient = "alice"\n', ''))
        self.assertIn('Alice, Bob — ', operator_thread_name(self.team))
        # A global default that cannot access this profile must not label it.
        (self.root / 'recipients.toml').write_text(self.contacts.replace(
            'sessions = ["default", "team"]', 'sessions = ["default"]'))
        self.assertIn('Bob — ', operator_thread_name(self.team))

    def test_thread_name_uses_provider_directory_root_not_container_home(self):
        self.project.config['directory_extensions'] = {'root': str(self.root)}
        self.assertEqual(operator_thread_name(self.project), 'Phone operator: Alice — home')
        self.project.workspace = self.root / 'work' / 'project'
        self.assertEqual(operator_thread_name(self.project), 'Phone operator: Alice — work/project')

    def test_thread_name_before_contact_setup(self):
        (self.root / 'recipients.toml').unlink()
        self.assertEqual(operator_thread_name(self.project),
                         'Phone operator: Unassigned — ' + self.root.name)

    def test_sessions_share_person_project_mailbox_but_keep_distinct_audio_processes_and_paths(self):
        a, b = self.project, self.team
        self.assertEqual(a.state, self.root / 'state')
        for x, y in [(a.state, b.state), (a.sip_directory, b.sip_directory), (a.audio, b.audio),
                     (a.operator['session'], b.operator['session']), (a.sip['username'], b.sip['username'])]:
            self.assertNotEqual(x, y)
        Mailbox(a).post('w', 's', 'body', 'same')
        self.assertEqual(Mailbox(b, 'alice').count(), 1)
        self.assertEqual(Mailbox(b, 'bob').count(), 0)
        self.assertIn('--session team', b.mailbox_command())
        self.assertIn('team', b.instructions()['developerInstructions'])

    def test_validation_and_private_asset_never_contain_plaintext_pin(self):
        result = json.dumps(check_config(self.project))
        self.assertNotIn('123456', result)
        self.assertNotIn('65432100', result)
        mapping = json.loads(access_bytes(self.project))
        self.assertEqual(mapping['sessions']['team'], 'operator-team')
        self.assertNotIn('65432100', json.dumps(mapping))
        self.assertEqual(mapping['recipients'][1]['pin_auth']['length'], 8)

    def test_recipient_and_session_permission(self):
        self.assertEqual(resolve_recipient(self.team, 'bob')['incoming_session'], 'team')
        with self.assertRaises(SetupError): resolve_recipient(self.project, 'bob')
        with self.assertRaises(SetupError): resolve_recipient(self.team, number='+12025550999')

    def test_duplicate_pin_and_duplicate_number_rejected(self):
        for changed in [self.contacts.replace('65432100','123456'), self.contacts.replace('+12025550102','+12025550101')]:
            (self.root / 'recipients.toml').write_text(changed)
            with self.assertRaises(SetupError): recipients(self.project, secrets=True)

    def test_prompts_render_literal_values_and_reject_unknown_placeholders(self):
        (self.root / 'sessions/team.toml').write_text('''[prompts]
initial = "Operate {{session_id}} using {{mailbox_command}}."
connection = "{{default}} Ask about {{recipient_id}}."
''')
        p = Project(self.root, 'team')
        self.assertIn('Operate team using ' + p.mailbox_command() + '.', p.instructions()['developerInstructions'])
        self.assertEqual(prompt(p, 'connection', 'Connected.', recipient_id='bob'), 'Connected. Ask about bob.')
        (self.root / 'sessions/team.toml').write_text('[prompts]\nconnection="{{typo}}"\n')
        with self.assertRaises(SetupError): Project(self.root, 'team')

    def test_existing_frontend_reads_edited_files_and_inline_prompts_without_reset(self):
        profile = self.root / 'sessions/team.toml'
        profile.write_text('[prompts]\ninitial_file="initial.txt"\nconnection_file="hello.txt"\n'
                           'disconnection="Original review."\n')
        initial = profile.parent / 'initial.txt'
        hello = profile.parent / 'hello.txt'
        initial.write_text('Original routine.')
        hello.write_text('{{default}} Original greeting.')
        p = Project(self.root, 'team')
        thread, audio, port, permissions = p.thread_id(), dict(p.audio), p.sip['control_port'], p.permissions()
        self.assertIn('Original routine.', p.instructions()['developerInstructions'])
        initial.write_text('Use the new working hours.')
        hello.write_text('{{default}} New greeting for {{recipient_id}}.')
        profile.write_text('control_port=23456\npermissions="workspace-write"\n' +
                           profile.read_text().replace('Original review.', 'Review the revised routine.'))
        self.assertEqual(prompt(p, 'connection', 'Connected.', recipient_id='bob'),
                         'Connected. New greeting for bob.')
        self.assertEqual(prompt(p, 'disconnection', 'Ended.'), 'Review the revised routine.')
        self.assertIn('Use the new working hours.', p.instructions()['developerInstructions'])
        self.assertEqual((p.thread_id(), p.audio, p.sip['control_port'], p.permissions()),
                         (thread, audio, port, permissions))

    def test_invalid_live_edit_retains_last_good_prompts_but_validation_fails(self):
        profile = self.root / 'sessions/team.toml'
        profile.write_text('[prompts]\nconnection="Known good greeting."\n')
        p = Project(self.root, 'team')
        for text in ['[prompts', '[prompts]\nconnection="{{unknown}}"\n',
                     '[prompts]\nconnection_file="missing.txt"\n']:
            profile.write_text(text)
            with self.assertRaises(SetupError): Project(self.root, 'team')
            with patch('voice_config.print') as warning:
                self.assertEqual(prompt(p, 'connection', 'Default.'), 'Known good greeting.')
                self.assertEqual(prompt(p, 'connection', 'Default.'), 'Known good greeting.')
                warning.assert_called_once()
            profile.write_text('[prompts]\nconnection="Known good greeting."\n')
            prompt(p, 'connection', 'Default.')
        profile.write_text('[prompts]\nconnection="Repaired greeting."\n')
        self.assertEqual(prompt(p, 'connection', 'Default.'), 'Repaired greeting.')

    def test_port_collision_fails_preflight(self):
        (self.root / 'sessions/team.toml').write_text('control_port=4444\n')
        with self.assertRaises(SetupError): check_config(self.project)

    def test_extensions_require_explicit_digit_strings(self):
        for value in ['200', '"0"', '"01"', '"123456"', '"1234567"', '"*1"']:
            (self.root / 'sessions/team.toml').write_text('extension='+value+'\n')
            with self.assertRaises(SetupError): Project(self.root,'team')

    def test_conflicts_only_reserve_matching_session(self):
        calls = object.__new__(Calls); calls.project = self.team
        self.assertFalse(calls.conflicts({'session_id':'default','to':'+12025550101'}))
        self.assertFalse(calls.conflicts({'session_id':'default','to':'+12025550102'}))
        self.assertTrue(calls.conflicts({'session_id':'team','to':'+12025550101'}))
        calls.project = self.project
        self.assertTrue(calls.conflicts({'to':'+12025550101'}))  # Legacy default-session attempt.

    def test_incoming_identity_and_notices_are_scoped(self):
        events = CallEvents(self.team)
        events.event({'event': True, 'class':'call', 'type':'CALL_ESTABLISHED', 'id':'call-bob',
                      'accountaor':'sip:operator-team@YOUR-DOMAIN.sip.twilio.com',
                      'peeruri':'sip:voip-in-bob@example.com'})
        event = events.pending(self.team.thread_id())
        self.assertIsNotNone(event)
        self.assertEqual(event['recipient_id'], 'bob')
        Mailbox(self.team, 'bob').post('w','s','body','one')
        self.assertIn(self.team.mailbox_command() + ' mailbox --recipient bob list', events.message(event))
        self.assertEqual(Mailbox(self.project).count(), 0)

    def test_systemd_templates_scope_commands_and_dependencies(self):
        render_systemd(self.project)
        directory = self.root / 'systemd/generated'
        text = (directory/'voip-call-events@.service').read_text()
        self.assertIn('--session %i sip monitor', text)
        self.assertIn('After=voip-baresip@%i.service', text)
        self.assertIn('run-requests.sh', (directory/'voip-requests.service').read_text())
    def test_profiles_cannot_share_same_native_thread(self):
        (self.team.state/'operator-thread-id').write_text(self.project.thread_id())
        with self.assertRaises(SetupError): check_config(self.project)
