import json
from service_fixture import ServiceFixture
from voip import Project, SetupError, render_systemd
from voice_config import recipients, sessions
from pin_security import master_key, migrate
from pin_delivery import Deliveries
from requests_queue import Requests
from service_config import access_bytes, check_config, dispatcher_units
from operator_mailbox import Mailbox

class ExternalProjectsTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.external=self.root/'external'
        (self.external/'profiles').mkdir(parents=True)
        (self.external/'profiles/project.toml').write_text('control_port=15446\n[prompts]\ninitial="Work in this project."\n')
        (self.external/'server.toml').write_text('[service]\nprovider_root=".."\nsessions_dir="profiles"\nsession_ids=["project"]\n')
        (self.external/'recipients.toml').write_text('''default_recipient="carol"
[recipients.carol]
number="+12025550103"
pin_file="private/pin.json"
sessions=["project"]
incoming_session="project"
''')
        config=self.root/'server.toml'
        config.write_text(config.read_text()+'\n[service.projects]\nproject="external"\n')
        self.project=Project(self.root)
        self.child=Project(self.root,'project')
        # Existing global key is shared; only the new verifier/queue live externally.
        master_key(self.project,create=True)
        self.generated=Deliveries(self.child).generate('carol')
    def test_project_owns_cwd_state_mailbox_and_request_queue(self):
        self.assertEqual(self.child.root,self.external)
        self.assertEqual(self.child.shared_state,self.external/'state')
        self.assertEqual(self.child.provider_state,self.root/'state')
        self.assertEqual(self.child.profile['id'],'project')
        self.assertIn(str(self.external),self.child.mailbox_command())
        request=Requests(self.child).enqueue('daily','call','Review')
        self.assertEqual(request['spec']['recipient_id'],'carol')
        self.assertFalse((self.root/'state/requests.json').exists())
        self.assertEqual(sessions(self.child),['project'])
    def test_provider_access_map_aggregates_but_does_not_copy_private_task_config(self):
        records,_=recipients(self.project)
        self.assertEqual(set(records),{'alice','bob','carol'})
        mapping=json.loads(access_bytes(self.project))
        self.assertEqual(mapping['sessions']['project'],'operator-project')
        self.assertNotIn(self.generated,json.dumps(mapping))
        self.assertNotIn('carol',(self.root/'recipients.toml').read_text())
        self.assertEqual(check_config(self.project)['sessions'],['default','project','team'])
    def test_migration_keeps_recipient_in_own_project_and_shares_key_only(self):
        migrate(self.project)
        self.assertNotIn('carol',(self.root/'recipients.toml').read_text())
        self.assertFalse((self.external/'state/pin-master-key').exists())
        self.assertEqual(master_key(self.child),master_key(self.project))
        self.assertEqual(recipients(self.child,True)[0]['carol']['pin_auth']['length'],8)
    def test_nested_registration_and_wrong_provider_are_rejected(self):
        file=self.external/'server.toml';before=file.read_text()
        file.write_text(before+'\n[service.projects]\nloop=".."\n')
        with self.assertRaises(SetupError): Project(self.root,'project')
        file.write_text(before.replace('provider_root=".."','provider_root="."'))
        with self.assertRaises(SetupError): Project(self.root,'project')
    def test_project_worker_unit_is_scoped(self):
        render_systemd(self.project)
        text=(self.root/'systemd/generated/voip-requests@.service').read_text()
        self.assertIn(' %i\n',text)
        self.assertIn('run-requests.sh',text)

    def test_multiple_sessions_belong_to_same_project_share_mailbox_and_one_dispatcher(self):
        config=self.external/'server.toml'
        config.write_text(config.read_text().replace('session_ids=["project"]','session_ids=["project","review"]'))
        (self.external/'profiles/review.toml').write_text('control_port=15447\nextension="201"\n')
        profile=self.external/'profiles/project.toml'
        profile.write_text('extension="200"\n'+profile.read_text())
        contacts=self.external/'recipients.toml'
        contacts.write_text(contacts.read_text().replace('sessions=["project"]','sessions=["project","review"]'))
        a=Project(self.root,'project');b=Project(self.root,'review')
        self.assertEqual(a.root,b.root)
        self.assertNotEqual(a.state,b.state)
        Mailbox(a,'carol').post('w','s','Shared project agenda','agenda')
        self.assertEqual(Mailbox(b,'carol').read('agenda')['body'],'Shared project agenda')
        Mailbox(b,'carol').address('agenda','Resolved in review session')
        self.assertEqual(Mailbox(a,'carol').count(),0)
        self.assertEqual(sessions(a),['project','review'])
        checked=check_config(Project(self.root))
        self.assertEqual(checked['extensions'],{'200':'project','201':'review'})
        self.assertEqual(dispatcher_units(Project(self.root),checked['sessions']),
                         ['voip-requests.service','voip-requests@project.service'])
        self.assertEqual(len(json.loads(access_bytes(Project(self.root)))['recipients']),3)

    def test_provider_recipient_can_access_registered_project_without_copying_mailbox_or_pin(self):
        contacts=self.root/'recipients.toml'
        contacts.write_text(contacts.read_text().replace('["default", "team"]','["default", "team", "project"]'))
        child=Project(self.root,'project')
        Mailbox(self.project,'alice').post('w','s','Provider project','same')
        Mailbox(child,'alice').post('w','s','External project','same')
        self.assertEqual(Mailbox(child,'alice').read('same')['body'],'External project')
        self.assertEqual(Mailbox(self.project,'alice').read('same')['body'],'Provider project')
        self.assertEqual(Mailbox(child).recipient['id'],'carol')
        self.assertNotIn('alice',(self.external/'recipients.toml').read_text())

    def test_session_names_and_extensions_cannot_collide_across_projects(self):
        file=self.external/'server.toml';before=file.read_text()
        file.write_text(before.replace('session_ids=["project"]','session_ids=["team"]'))
        with self.assertRaises(SetupError): Project(self.root)
        file.write_text(before)
        (self.root/'sessions/team.toml').write_text('extension="200"\ncontrol_port=12345\n')
        profile=self.external/'profiles/project.toml'
        profile.write_text('extension="200"\n'+profile.read_text())
        with self.assertRaisesRegex(SetupError,'extensions must be unique'): check_config(Project(self.root))

    def test_project_can_use_existing_people_without_new_recipient_or_pin_copy(self):
        contacts=self.root/'recipients.toml'
        contacts.write_text(contacts.read_text().replace('["default", "team"]','["default", "team", "project"]'))
        (self.external/'recipients.toml').write_text('default_recipient="alice"\n')
        project=Project(self.root,'project')
        self.assertEqual(Mailbox(project).recipient['id'],'alice')
        migrate(Project(self.root))
        checked=check_config(Project(self.root))
        self.assertEqual({r['id'] for r in checked['recipients']},{'alice','bob'})
        self.assertNotIn('pin',(self.external/'recipients.toml').read_text())
