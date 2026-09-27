import json
import subprocess
import time
from unittest.mock import patch
from service_fixture import ServiceFixture
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests, decision, Deferred
from request_runtime import Runtime
from call_events import CallEvents
from call_preparation import Preparations
from voip import Project, SetupError, ROOT


class RecipientMailboxTests(ServiceFixture):
    def test_sessions_share_same_user_project_mailbox_and_keep_users_separate(self):
        alice = Mailbox(self.project, 'alice')
        alice.post('w', 'Alice agenda', 'Private Alice context', 'same')
        bob = Mailbox(self.team, 'bob')
        bob.post('w', 'Bob agenda', 'Private Bob context', 'same')
        self.assertEqual(Mailbox(self.team, 'alice').read('same')['body'], 'Private Alice context')
        self.assertEqual(bob.read('same')['body'], 'Private Bob context')
        Mailbox(self.team, 'alice').address('same', 'Resolved in team conversation')
        self.assertEqual(alice.count(), 0)
        self.assertEqual(bob.count(), 1)
        self.assertEqual(alice.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(SetupError): Mailbox(self.project, 'bob')

    def test_default_agenda_and_explicit_ids_cannot_cross_recipient_boundary(self):
        Mailbox(self.team, 'alice').post('w','a','Alice private','alice-only')
        Mailbox(self.team, 'bob').post('w','b','Bob private','bob-only')
        for kind in ['message', 'call']:
            r = Requests(self.team).enqueue(kind, kind, 'Review', recipient='bob')
            self.assertEqual(r['spec']['message_ids'], ['bob-only'])
            with self.assertRaises(SetupError):
                Requests(self.team).enqueue(kind+'-bad', kind, 'Review', ['alice-only'], recipient='bob')
        self.assertEqual(Mailbox(self.team, number='+12025550102').recipient['id'], 'bob')

    def test_preemptive_message_waits_for_other_recipient_call_and_rechecks_at_submission(self):
        record = Requests(self.team).enqueue('bob', 'message', 'Private', recipient='bob', mode='preemptive')
        state = dict(ready=True, connected=True, same_recipient=False, backend_busy=False, quiet=True, phone_busy=False)
        self.assertEqual(decision(record, state, time.time()), 'wait')
        runtime = Runtime(self.team)
        with patch.object(runtime, 'state', return_value=state), patch('request_runtime.Rpc') as rpc:
            with self.assertRaises(Deferred): runtime.prompt(record, state)
            rpc.assert_not_called()
        state.update(same_recipient=True)
        self.assertEqual(decision(record, state, time.time()), 'prompt')

    def test_connection_count_and_delayed_hangup_commands_use_event_recipient(self):
        Mailbox(self.team, 'alice').post('w','a','Private Alice','a')
        events = CallEvents(self.team)
        def observe(kind, identifier, recipient):
            events.event(dict(event=True, **{'class':'call'}, type=kind, id=identifier,
                accountaor='sip:operator-team@YOUR-DOMAIN.sip.twilio.com',peeruri='sip:voip-in-'+recipient+'@example.com'))
        observe('CALL_ESTABLISHED','bob-call','bob')
        connected = events.load()['events'][-1]
        self.assertNotIn('mailbox',events.message(connected))
        Mailbox(self.team,'bob').post('w','b','Private Bob','b')
        self.assertIn('1 unaddressed mailbox message',events.message(connected))
        self.assertIn('mailbox --recipient bob list',events.message(connected))
        observe('CALL_CLOSED','bob-call','bob')
        ended=events.load()['events'][-1]
        observe('CALL_ESTABLISHED','alice-call','alice')
        self.assertIn('mailbox --recipient bob address',events.message(ended))
        self.assertNotIn('Private Alice',events.message(ended))

    def test_changed_phone_number_starts_empty_mailbox(self):
        box=Mailbox(self.team,'bob');box.post('w','s','old number','old')
        path=self.root/'recipients.toml'
        path.write_text(path.read_text().replace('+12025550102','+12025550103'))
        fresh=Mailbox(Project(self.root,'team'),'bob')
        self.assertNotEqual(box.path,fresh.path)
        self.assertEqual(fresh.count(),0)
        self.assertTrue(box.path.exists())

    def test_migration_preserves_history_and_resolution_without_duplicate_import(self):
        old={'id':'legacy','sender':'w','subject':'s','body':'Original','created_at':1,
             'status':'addressed','addressed_at':2,'resolution':'Already fulfilled'}
        source=self.team.state/'mailbox.json';save_json(source,{'legacy':old})
        bob=Mailbox(self.team,'bob')
        self.assertEqual(bob.count(),0)  # Legacy content is never guessed or exposed.
        self.assertEqual(bob.migrate()['migrated'],1)
        self.assertFalse(source.exists());self.assertTrue(source.with_suffix('.json.migrated').exists())
        self.assertEqual(bob.read('legacy')['resolution'],'Already fulfilled')
        self.assertEqual(bob.migrate()['migrated'],0)
        self.assertEqual(Mailbox(self.team,'alice').count(),0)

    def test_preparation_is_bound_to_recipient_and_phone_number(self):
        with patch('call_preparation.Rpc') as rpc:
            prep=Preparations(self.team,rpc)
            brief={'ready':True,'opening':'Bob topic','context':'Details'}
            prep.prepare('p','Review',[],brief=brief,recipient='bob')
            with self.assertRaises(SetupError): prep.ready('p','Review',recipient='alice')
            with self.assertRaises(SetupError): prep.prepare('p','Review',[],brief=brief,recipient='alice')
            path=self.root/'recipients.toml'
            path.write_text(path.read_text().replace('+12025550102','+12025550103'))
            with self.assertRaises(SetupError): prep.ready('p','Review',recipient='bob')

    def test_cli_defaults_to_current_caller_but_accepts_explicit_ended_recipient(self):
        Mailbox(self.team,'alice').post('w','Alice','Alice body','a')
        Mailbox(self.team,'bob').post('w','Bob','Bob body','b')
        events=CallEvents(self.team)
        events.snapshot({'call-bob':True},recipients={'call-bob':'bob'})
        def cli(*args):
            return subprocess.run(['python3',str(ROOT/'voip.py'),'--root',str(self.root),'--session','team',
                                   'mailbox',*args],capture_output=True,text=True,check=True).stdout
        self.assertIn('Bob',cli('list'));self.assertNotIn('Alice',cli('list'))
        self.assertIn('Alice',cli('--recipient','alice','list'))
        self.assertIn('Alice',cli('list','--recipient','alice'))
