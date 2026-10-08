from datetime import datetime
import json
import subprocess
import sys
from unittest.mock import patch

from service_fixture import ServiceFixture
from call_events import CallEvents
from call_schedule import Scheduler
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests, Dispatcher
from schedule_occurrences import Occurrences
from voip import Project, ROOT, SetupError


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class OccurrenceTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.now = stamp('2026-10-08T17:00:00+00:00')  # noon Central
        (self.root/'agenda.txt').write_text('Review the morning; plan the afternoon.')
        self.write_schedule()
        self.scheduler = Scheduler(self.team, clock=lambda: self.now)
        self.occurrences = Occurrences(self.team, clock=lambda: self.now)
        self.key = 'schedule:review:20261008T1300'

    def write_schedule(self, cron='0 13 * * *', zone='America/Chicago'):
        (self.root/'schedules.toml').write_text(f'''[[schedules]]
id="review"
cron="{cron}"
timezone="{zone}"
session="team"
recipient="bob"
reason="Afternoon meeting"
agenda_file="agenda.txt"
''')

    def connect(self, recipient='bob', call_id='connected-call', age=0):
        events = CallEvents(self.team)
        state = events.load()
        events.transition(state, call_id, True, recipient_id=recipient)
        state.update(observer_online=True, observed_at=self.now-age)
        save_json(events.path, state)
        return call_id

    def test_agenda_preview_is_read_only_and_returns_the_future_prompt_data(self):
        result = self.occurrences.agenda(self.key, 'bob')
        self.assertEqual(result['agenda'], 'Review the morning; plan the afternoon.')
        self.assertEqual(result['agenda_source'], 'current_agenda_file')
        self.assertIsNone(result['prepared_brief'])
        self.assertEqual(result['local_time'], '2026-10-08T13:00:00-05:00')
        self.assertFalse(self.scheduler.path.exists())
        self.assertEqual(Requests(self.team).load(), {})
        self.assertEqual(Mailbox(self.team, 'bob').count(), 0)

    def test_skip_persists_omits_next_and_missed_count_and_keeps_recurring_calls(self):
        config = (self.root/'schedules.toml').read_bytes()
        self.scheduler.tick()
        first = self.occurrences.change(self.key, 'bob', 'Cannot attend today')
        self.assertEqual(first['outcome'], 'caller_skipped')
        self.assertEqual(self.occurrences.change(self.key, 'bob', 'Same request'), first)
        restarted = Scheduler(self.team, clock=lambda: self.now)
        self.assertEqual(restarted.next(1)[0]['occurrence_key'], 'schedule:review:20261009T1300')
        self.now = stamp('2026-10-08T19:00:00+00:00')
        self.assertEqual(restarted.tick(), [])
        self.assertEqual(restarted.missed('bob'), [])
        self.assertEqual(Requests(self.team).load(), {})
        self.assertEqual(Mailbox(self.team, 'bob').count(), 0)
        self.now = stamp('2026-10-09T18:00:00+00:00')
        self.assertEqual(len(restarted.tick()), 1)
        self.assertEqual((self.root/'schedules.toml').read_bytes(), config)

    def test_scope_checks_prevent_reading_or_modifying_another_person_or_session(self):
        for action in ('agenda', 'change'):
            for scoped, recipient in ((self.team, 'alice'), (self.project, 'alice')):
                with self.assertRaises(SetupError):
                    method = getattr(Occurrences(scoped, clock=lambda: self.now), action)
                    method(self.key, recipient, *(['wrong scope'] if action == 'change' else []))
        self.assertFalse(self.scheduler.path.exists())

    def test_bad_keys_non_occurrences_and_nonexistent_dst_minutes_rejected(self):
        for key in ('bad', 'schedule:review:20261399T1300', 'schedule:review:20261008T1301',
                    'schedule:unknown:20261008T1300', 'schedule:review:20261007T1300'):
            with self.assertRaises(SetupError): self.occurrences.change(key, 'bob', 'Skip')
        self.write_schedule('30 2 * * *')
        with self.assertRaisesRegex(SetupError, 'real occurrence'):
            self.occurrences.agenda('schedule:review:20260308T0230', 'bob')
        self.assertFalse(self.scheduler.path.exists())

    def test_fall_back_uses_next_instant_but_one_skip_covers_both(self):
        self.write_schedule('30 1 * * *')
        key = 'schedule:review:20261101T0130'
        self.now = stamp('2026-11-01T07:00:00+00:00')
        self.assertEqual(self.scheduler.next(1)[0]['local_time'], '2026-11-01T01:30:00-06:00')
        self.assertEqual(self.occurrences.agenda(key, 'bob')['local_time'], '2026-11-01T01:30:00-06:00')
        self.occurrences.change(key, 'bob', 'Skip the repeated wall-clock meeting')
        self.now = stamp('2026-11-01T07:30:00+00:00')
        self.assertEqual(self.scheduler.tick(), [])
        self.assertEqual(Requests(self.team).load(), {})

    def test_skip_already_queued_but_unsubmitted_request_keeps_current_call(self):
        call = self.connect()
        self.now = stamp('2026-10-08T18:00:00+00:00')
        self.scheduler.tick()
        self.occurrences.change(self.key, 'bob', 'Skip while another discussion continues')
        self.assertEqual(Requests(self.team).read(self.key)['state'], 'cancelled')
        self.assertTrue(CallEvents(self.team).load()['calls'][call]['connected'])
        self.assertEqual(Mailbox(self.team, 'bob').read(self.key)['status'], 'unaddressed')
        self.assertEqual(self.scheduler.tick(), [])

    def test_dispatching_active_or_delivered_occurrence_is_not_cancelled(self):
        self.now = stamp('2026-10-08T18:00:00+00:00')
        self.scheduler.tick()
        queue = Requests(self.team)
        for status in ('active', 'dispatching', 'blocked', 'prompt_submitted', 'completed'):
            queue.update(self.key, state=status)
            before = self.scheduler.path.read_bytes()
            with self.assertRaisesRegex(SetupError, 'already dispatching'):
                self.occurrences.change(self.key, 'bob', 'Skip')
            self.assertEqual(queue.read(self.key)['state'], status)
            self.assertEqual(self.scheduler.path.read_bytes(), before)

    def test_skip_settled_retry_cancels_remaining_retry_without_provider_call(self):
        self.now = stamp('2026-10-08T18:00:00+00:00'); self.scheduler.tick()
        queue = Requests(self.team)
        queue.update(self.key, state='retry_wait', attempts=[{'key':self.key,'finished':True}])
        with patch('outgoing_calls.Calls') as calls:
            self.occurrences.change(self.key, 'bob', 'No more attempts today')
            calls.assert_not_called()
        self.assertEqual(queue.read(self.key)['state'], 'cancelled')

    def test_occurrence_edit_is_serialized_with_actual_dispatch_worker(self):
        with Requests(self.team).process_lock(self.key):
            with self.assertRaises(SetupError): self.occurrences.change(self.key, 'bob', 'Skip')
            with self.assertRaises(SetupError): Dispatcher(self.team).process(self.key)
        self.assertFalse(self.scheduler.path.exists())

    def test_start_early_freezes_agenda_on_same_call_without_dial_or_completion(self):
        call = self.connect(); thread = self.team.thread_id()
        result = self.occurrences.change(self.key, 'bob', 'Do this meeting now', call)
        self.assertEqual(result['outcome'], 'started_early')
        self.assertEqual(result['message_status'], 'unaddressed')
        self.assertEqual(result['message_id'], self.key)
        self.assertEqual(Requests(self.team).load(), {})
        self.assertEqual(self.team.thread_id(), thread)
        self.assertTrue(CallEvents(self.team).load()['calls'][call]['connected'])
        (self.root/'agenda.txt').write_text('Changed future template')
        self.assertEqual(self.occurrences.agenda(self.key,'bob')['agenda'], result['agenda'])
        self.assertEqual(self.occurrences.change(self.key,'bob','Retry same request',call), result)
        self.now = stamp('2026-10-08T18:00:00+00:00')
        self.assertEqual(self.scheduler.tick(), [])
        self.assertEqual(Requests(self.team).load(), {})
        self.assertEqual(Mailbox(self.team,'bob').count(), 1)
        Mailbox(self.team,'bob').address(self.key,'Reviewed and agreed next steps.')
        self.assertEqual(self.occurrences.agenda(self.key,'bob')['message_id'], self.key)

    def test_start_early_requires_fresh_authenticated_matching_call(self):
        for recipient, age in (('bob', 15), ('alice', 0), (None, 0)):
            call = self.connect(recipient, call_id=str((recipient,age)), age=age)
            with self.assertRaisesRegex(SetupError, 'authenticated call'):
                self.occurrences.change(self.key, 'bob', 'Start now', call)
        with self.assertRaises(SetupError): self.occurrences.change(self.key, 'bob', 'Start now', '')
        self.assertFalse(self.scheduler.path.exists())

    def test_early_crash_before_mailbox_write_recovers_without_a_later_dial(self):
        call = self.connect()
        with patch('schedule_occurrences.Mailbox.post', side_effect=OSError('injected crash')):
            with self.assertRaises(OSError): self.occurrences.change(self.key,'bob','Start now',call)
        self.assertEqual(self.scheduler.load()['occurrences'][self.key]['state'],'early_pending')
        self.assertNotEqual(self.scheduler.next(1)[0]['occurrence_key'],self.key)
        self.now = stamp('2026-10-08T18:00:00+00:00')
        self.scheduler.tick(); self.scheduler.tick()
        self.assertEqual(self.scheduler.load()['occurrences'][self.key]['outcome'],'started_early')
        self.assertEqual(Mailbox(self.team,'bob').count(),1)
        self.assertEqual(Requests(self.team).load(),{})

    def test_early_crash_after_mailbox_write_does_not_overwrite_later_resolution(self):
        call = self.connect()
        real_save = save_json
        def save(path, state):
            if state['occurrences'][self.key]['state']=='skipped': raise OSError('after mailbox')
            return real_save(path,state)
        with patch('schedule_occurrences.save_json',side_effect=save):
            with self.assertRaises(OSError): self.occurrences.change(self.key,'bob','Start now',call)
        Mailbox(self.team,'bob').address(self.key,'Handled before recovery.')
        self.scheduler.tick()
        self.assertEqual(Mailbox(self.team,'bob').read(self.key)['status'],'addressed')
        self.assertEqual(Requests(self.team).load(),{})

    def test_saved_preparation_is_exposed_as_historical_and_never_generated_by_read(self):
        from call_preparation import Preparations
        person=Mailbox(self.team,'bob').recipient
        prep={'state':'ready','messages':{self.key:'digest'},'recipient_id':'bob',
              'recipient_number':person['number'],'prepared_at':self.now,
              'brief':{'ready':True,'opening':'Review our next steps.','context':'Saved context.'}}
        save_json(Preparations(self.team).path,{'one':prep,'wrong':{**prep,'recipient_id':'alice','prepared_at':self.now+1}})
        with patch('call_preparation.Rpc') as rpc:
            result=self.occurrences.agenda(self.key,'bob');rpc.assert_not_called()
        self.assertEqual(result['prepared_brief']['opening'],'Review our next steps.')
        self.assertTrue(result['prepared_brief']['historical_snapshot'])

    def test_cancelled_queue_survives_crash_before_skip_ledger_write(self):
        self.now = stamp('2026-10-08T18:00:00+00:00'); self.scheduler.tick()
        with patch('schedule_occurrences.save_json',side_effect=OSError('ledger write failed')):
            with self.assertRaises(OSError): self.occurrences.change(self.key,'bob','Skip today')
        self.assertEqual(Requests(self.team).read(self.key)['state'],'cancelled')
        self.assertEqual(self.scheduler.tick(),[])
        result=self.occurrences.change(self.key,'bob','Skip today')
        self.assertTrue(result['original_dial_suppressed'])

    def test_start_early_does_not_reassign_an_existing_meeting_to_a_different_call(self):
        call=self.connect()
        self.occurrences.change(self.key,'bob','Start now',call)
        self.connect(call_id='another-call')
        with self.assertRaisesRegex(SetupError,'another call'):
            self.occurrences.change(self.key,'bob','Start again','another-call')
        self.assertEqual(Mailbox(self.team,'bob').count(),1)

    def test_queued_agenda_read_uses_saved_body_and_reports_mailbox_status(self):
        self.now = stamp('2026-10-08T18:00:00+00:00'); self.scheduler.tick()
        (self.root/'agenda.txt').write_text('Changed next meeting instructions.')
        result=self.occurrences.agenda(self.key,'bob')
        self.assertEqual(result['agenda'],'Review the morning; plan the afternoon.')
        self.assertEqual(result['message_id'],self.key)
        self.assertEqual(result['message_status'],'unaddressed')

    def test_changed_recipient_cannot_read_early_agenda_from_previous_number(self):
        self.occurrences.change(self.key,'bob','Start now',self.connect())
        path=self.root/'recipients.toml'
        path.write_text(path.read_text().replace('+12025550102','+12025550109'))
        with self.assertRaisesRegex(SetupError,'number changed'): self.occurrences.agenda(self.key,'bob')

    def test_cli_agenda_and_skip_use_scoped_json_interface(self):
        # Use an occurrence tomorrow relative to the real test wall clock.
        scheduler=Scheduler(self.team)
        key=scheduler.next(1)[0]['occurrence_key']
        prefix=[sys.executable,str(ROOT/'voip.py'),'--root',str(self.root),'--session','team','--json','schedule']
        def cli(action,*args):
            r=subprocess.run([*prefix,action,key,'--recipient','bob',*args],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            return json.loads(r.stdout)['data']
        self.assertEqual(cli('agenda')['agenda_source'],'current_agenda_file')
        self.assertEqual(cli('skip','--note','Skip once')['outcome'],'caller_skipped')
        self.assertNotEqual(scheduler.next(1)[0]['occurrence_key'],key)
