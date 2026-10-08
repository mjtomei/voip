from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from unittest.mock import patch

from test_schedule_occurrences import OccurrenceFixture, stamp
from test_requests_queue import FakeRuntime
from call_events import CallEvents
from call_schedule import Scheduler
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests, Dispatcher
from schedule_occurrences import Occurrences
from voip import ROOT, SetupError


class RescheduleTests(OccurrenceFixture):
    def setUp(self):
        super().setUp()
        timer = patch('time.time', side_effect=lambda: self.now)
        timer.start(); self.addCleanup(timer.stop)
        self.queue = Requests(self.team)

    def move(self, at='2026-10-08T15:00:00-05:00', note='Caller requested a later meeting'):
        return self.occurrences.reschedule(self.key, 'bob', at, note)

    def test_move_keeps_recurrence_and_agenda_but_dispatches_only_new_time(self):
        original = (self.root/'schedules.toml').read_bytes()
        result = self.move()
        self.assertEqual(result['original_local_time'], '2026-10-08T13:00:00-05:00')
        self.assertEqual(result['local_time'], '2026-10-08T15:00:00-05:00')
        self.assertEqual(self.queue.load(), {})
        self.assertEqual(Mailbox(self.team, 'bob').count(), 0)
        self.assertEqual(self.scheduler.next(1)[0]['occurrence_key'], self.key)
        self.assertEqual(self.scheduler.next(1)[0]['local_time'], result['local_time'])
        (self.root/'agenda.txt').write_text('A different future agenda')
        self.assertEqual(self.occurrences.agenda(self.key,'bob')['agenda_source'], 'saved_occurrence')
        self.now = stamp('2026-10-08T18:00:00Z'); self.scheduler.tick()
        self.assertEqual(self.queue.load(), {})
        self.now = stamp('2026-10-08T20:00:00Z'); self.scheduler.tick(); self.scheduler.tick()
        row = self.queue.read(result['request_key'])
        self.assertEqual(row['spec']['message_ids'], [self.key])
        self.assertEqual(row['spec']['source'], 'schedule:review')
        self.assertEqual(Mailbox(self.team,'bob').read(self.key)['body'], 'Review the morning; plan the afternoon.')
        self.assertEqual(len(self.queue.load()), 1)
        self.now = stamp('2026-10-09T18:00:00Z'); self.scheduler.tick()
        self.assertIn('schedule:review:20261009T1300', self.queue.load())
        self.assertEqual((self.root/'schedules.toml').read_bytes(), original)

    def test_move_earlier_survives_restart_and_original_time_stays_suppressed(self):
        result = self.move('2026-10-08T12:30:00-05:00')
        restarted = Scheduler(self.team, clock=lambda:self.now)
        self.now = stamp('2026-10-08T17:30:00Z'); restarted.tick()
        self.assertIn(result['request_key'], self.queue.load())
        self.now = stamp('2026-10-08T18:00:00Z'); restarted.tick()
        self.assertEqual(len(self.queue.load()), 1)

    def test_repeat_identical_move_is_idempotent_even_after_dispatch(self):
        first = self.move()
        self.assertEqual(self.move(note='Retry the command'), first)
        self.assertEqual(first['reschedule_revision'], 1)
        self.now = stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        runtime = FakeRuntime()
        Dispatcher(self.team, runtime, clock=lambda:self.now).process(first['request_key'])
        self.now += 120
        replay = self.move()
        self.assertEqual(replay['request_key'], first['request_key'])
        self.assertEqual(replay['state'], 'enqueued')
        self.assertEqual(len(runtime.dials), 1)
        self.assertEqual(len(self.queue.load()), 1)

    def test_second_move_cancels_old_queue_and_uses_one_new_revision(self):
        first = self.move()
        self.now = stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        second = self.move('2026-10-09T11:00:00-05:00')
        self.assertEqual(second['reschedule_revision'], 2)
        self.assertEqual(self.queue.read(first['request_key'])['state'], 'cancelled')
        next_calls = self.scheduler.next(2)
        self.assertEqual([r['local_time'] for r in next_calls],
                         ['2026-10-09T11:00:00-05:00','2026-10-09T13:00:00-05:00'])
        self.now = stamp('2026-10-09T16:00:00Z'); self.scheduler.tick()
        self.assertEqual(self.queue.read(second['request_key'])['state'],'queued')
        self.assertEqual(Mailbox(self.team,'bob').count(),1)

    def test_move_original_queued_request_preserves_live_call(self):
        call = self.connect()
        self.now = stamp('2026-10-08T18:00:00Z'); self.scheduler.tick()
        result = self.move()
        self.assertEqual(self.queue.read(self.key)['state'],'cancelled')
        self.assertNotIn(result['request_key'],self.queue.load())
        self.assertTrue(CallEvents(self.team).load()['calls'][call]['connected'])

    def test_same_call_reuse_and_busy_policy_apply_at_replacement_time(self):
        result = self.move(); self.now = stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        runtime = FakeRuntime(); runtime.observed.update(connected=True, same_recipient=False)
        dispatch = Dispatcher(self.team,runtime,clock=lambda:self.now)
        self.assertEqual(dispatch.process(result['request_key'])['state'],'queued')
        runtime.observed.update(same_recipient=True, backend_busy=True)
        self.assertEqual(dispatch.process(result['request_key'])['state'],'queued')
        runtime.observed.update(backend_busy=False)
        self.assertEqual(dispatch.process(result['request_key'])['state'],'prompt_submitted')
        self.assertEqual(runtime.prompts,[(result['request_key'],True)])
        self.assertEqual(runtime.dials,[])

    def test_retry_policy_is_frozen_and_lifetime_starts_at_replacement_time(self):
        with (self.root/'schedules.toml').open('a') as f:
            f.write('ttl_seconds=7200\n[schedules.redial]\ndelays_seconds=[60,300]\nretry_on=["no-answer"]\n')
        result = self.move('2026-10-10T15:00:00-05:00')
        self.write_schedule()
        self.now = stamp('2026-10-10T20:00:00Z'); self.scheduler.tick()
        row = self.queue.read(result['request_key'])
        self.assertEqual(row['expires_at'],self.now+7200)
        self.assertEqual(row['spec']['redial']['delays_seconds'],[60,300])
        runtime=FakeRuntime(); runtime.response={'state':'ended','phone_status':'no-answer','connected':False}
        dispatch=Dispatcher(self.team,runtime,clock=lambda:self.now)
        row=dispatch.process(result['request_key'])
        self.assertEqual(row['state'],'retry_wait')
        self.assertEqual(row['next_attempt_at'],self.now+60)
        self.now+=60; dispatch.process(result['request_key'])
        self.assertEqual(len(runtime.dials),2)

    def test_skip_and_start_early_use_replacement_time_and_request_lock(self):
        result=self.move()
        with self.queue.process_lock(result['request_key']):
            with self.assertRaises(SetupError): self.occurrences.change(self.key,'bob','Skip')
            with self.assertRaises(SetupError): self.move('2026-10-08T16:00:00-05:00')
        # Original time is past, but the moved meeting is still in the future.
        self.now=stamp('2026-10-08T19:00:00Z'); call=self.connect()
        early=self.occurrences.change(self.key,'bob','Hold it now',call)
        self.assertEqual(early['outcome'],'started_early')
        self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        self.assertEqual(self.queue.load(),{})
        self.assertEqual(Mailbox(self.team,'bob').count(),1)

    def test_skip_replacement_after_enqueue_cancels_only_its_current_request(self):
        result=self.move(); self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        self.occurrences.change(self.key,'bob','Skip the moved meeting')
        self.assertEqual(self.queue.read(result['request_key'])['state'],'cancelled')
        self.assertNotEqual(self.scheduler.next(1)[0]['occurrence_key'],self.key)

    def test_reschedule_can_restore_deliberately_skipped_meeting_but_not_started_one(self):
        self.occurrences.change(self.key,'bob','Skip once')
        self.move()
        self.occurrences.change(self.key,'bob','Hold now',self.connect())
        with self.assertRaisesRegex(SetupError,'already started'): self.move('2026-10-08T16:00:00-05:00')

    def test_active_completed_unknown_or_accepted_prompt_cannot_be_moved(self):
        result=self.move(); self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        for state in ('active','dispatching','blocked','completed','prompt_submitted'):
            self.queue.update(result['request_key'],state=state)
            before=self.scheduler.path.read_bytes()
            with self.assertRaisesRegex(SetupError,'already dispatching'): self.move('2026-10-08T16:00:00-05:00')
            self.assertEqual(self.scheduler.path.read_bytes(),before)

    def test_no_mutation_on_wrong_recipient_session_or_invalid_target_time(self):
        for p,person in ((self.team,'alice'),(self.project,'alice')):
            with self.assertRaises(SetupError):
                Occurrences(p,clock=lambda:self.now).reschedule(self.key,person,'2026-10-08T15:00-05:00','Move')
        for at in ('tomorrow','2026-10-08T15:00','2026-10-08T15:00:01-05:00',
                   '2026-13-08T15:00-05:00','2026-10-08T11:00-05:00','2026-10-08T15:00+25:00',
                   '2026-10-08T15:00+05:60'):
            with self.assertRaises(SetupError): self.move(at)
        self.assertFalse(self.scheduler.path.exists())

    def test_explicit_offset_disambiguates_fall_back_and_preview_normalizes_zone(self):
        self.now=stamp('2026-10-31T12:00:00Z'); self.key='schedule:review:20261031T1300'
        self.move('2026-11-01T01:30:00-06:00')
        upcoming=self.scheduler.next(2)
        self.assertEqual(upcoming[0]['local_time'],'2026-11-01T01:30:00-06:00')
        self.now=stamp('2026-11-01T06:30:00Z'); self.scheduler.tick()
        self.assertEqual(self.queue.load(),{})
        self.now=stamp('2026-11-01T07:30:00Z'); self.scheduler.tick()
        self.assertEqual(len(self.queue.load()),1)

    def test_offline_skip_counts_replacement_once_not_original(self):
        self.move()
        # No prior tick is needed to remember this explicitly scheduled time.
        self.now=stamp('2026-10-08T20:02:00Z')
        self.assertEqual(self.scheduler.missed('bob'),[self.key])
        self.assertEqual(self.scheduler.missed('bob'),[self.key])
        self.scheduler.tick()
        self.assertEqual(self.queue.load(),{})
        self.assertEqual(self.scheduler.missed('bob'),[self.key])

    def test_bounded_catchup_and_dry_run_do_not_lose_replacement(self):
        with (self.root/'schedules.toml').open('a') as f:
            f.write('misfire="enqueue"\nmax_lateness_seconds=180\n')
        result=self.move(); self.now=stamp('2026-10-08T20:02:00Z')
        before=self.scheduler.path.read_bytes()
        preview=self.scheduler.tick(dry_run=True)
        self.assertEqual([r['key'] for r in preview],[self.key])
        self.assertEqual(self.scheduler.path.read_bytes(),before)
        self.assertEqual(self.queue.load(),{})
        self.scheduler.tick()
        self.assertIn(result['request_key'],self.queue.load())
        self.assertEqual(self.scheduler.missed('bob'),[])

    def test_editing_or_disabling_template_does_not_remove_explicit_replacement(self):
        result=self.move()
        (self.root/'schedules.toml').write_text('schedules=[]\n')
        self.assertEqual(self.scheduler.next(1)[0]['occurrence_key'],self.key)
        self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        self.assertIn(result['request_key'],self.queue.load())

    def test_crash_after_cancelling_old_request_recovers_without_redial(self):
        self.now=stamp('2026-10-08T18:00:00Z'); self.scheduler.tick()
        with patch('schedule_occurrences.save_json',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.move()
        self.assertEqual(self.queue.read(self.key)['state'],'cancelled')
        self.scheduler.tick()
        result=self.move()
        self.assertEqual(result['reschedule_revision'],1)
        self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        self.assertEqual(len(self.queue.load()),2)
        self.assertEqual(self.queue.read(self.key)['state'],'cancelled')

    def test_crash_after_mailbox_write_recovers_same_replacement_without_duplication(self):
        result=self.move(); self.now=stamp('2026-10-08T20:00:00Z')
        with patch('call_schedule.Requests.enqueue',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.scheduler.tick()
        self.assertEqual(self.scheduler.load()['occurrences'][self.key]['state'],'pending')
        self.now+=120
        self.scheduler.tick(); self.scheduler.tick()
        self.assertEqual(list(self.queue.load()),[result['request_key']])
        self.assertEqual(Mailbox(self.team,'bob').count(),1)
        self.assertEqual(self.scheduler.missed('bob'),[])

    def test_changed_number_fails_closed_before_any_replacement_queue_or_mailbox(self):
        self.move()
        path=self.root/'recipients.toml'; path.write_text(path.read_text().replace('+12025550102','+12025550109'))
        with self.assertRaisesRegex(SetupError,'number changed'): self.occurrences.agenda(self.key,'bob')
        self.now=stamp('2026-10-08T20:00:00Z')
        with self.assertRaisesRegex(SetupError,'number changed'): self.scheduler.tick()
        self.assertEqual(self.queue.load(),{})
        self.assertEqual(Mailbox(self.team,'bob').count(),0)

    def test_crash_after_queue_write_reuses_it_and_preserves_addressed_agenda(self):
        result=self.move(); self.now=stamp('2026-10-08T20:00:00Z')
        real_save=save_json
        def save(path,state):
            if state['occurrences'][self.key]['state']=='enqueued': raise OSError('after queue')
            return real_save(path,state)
        with patch('call_schedule.save_json',side_effect=save):
            with self.assertRaises(OSError): self.scheduler.tick()
        Mailbox(self.team,'bob').address(self.key,'Handled on an existing call before recovery.')
        self.now+=120; self.scheduler.tick()
        runtime=FakeRuntime()
        row=Dispatcher(self.team,runtime,clock=lambda:self.now).process(result['request_key'])
        self.assertEqual(row['outcome'],'agenda_already_addressed')
        self.assertEqual(runtime.dials,[])
        self.assertEqual(len(self.queue.load()),1)

    def test_replacement_outside_catchup_limit_skips_without_queue(self):
        with (self.root/'schedules.toml').open('a') as f:
            f.write('misfire="enqueue"\nmax_lateness_seconds=60\n')
        self.move(); self.now=stamp('2026-10-08T20:02:00Z')
        self.scheduler.tick()
        self.assertEqual(self.scheduler.missed('bob'),[self.key])
        self.assertEqual(self.queue.load(),{})

    def test_settled_retry_can_be_moved_without_resubmitting_old_attempt(self):
        first=self.move(); self.now=stamp('2026-10-08T20:00:00Z'); self.scheduler.tick()
        self.queue.update(first['request_key'],state='retry_wait',
                          attempts=[{'key':first['request_key'],'finished':True}])
        second=self.move('2026-10-08T16:00:00-05:00')
        self.assertEqual(self.queue.read(first['request_key'])['state'],'cancelled')
        self.now=stamp('2026-10-08T21:00:00Z'); self.scheduler.tick()
        self.assertEqual(self.queue.read(second['request_key'])['state'],'queued')

    def test_coincident_recurrence_keeps_distinct_agendas_and_move_back_keeps_history(self):
        self.move('2026-10-09T13:00:00-05:00')
        next_calls=self.scheduler.next(2)
        self.assertEqual(len({r['occurrence_key'] for r in next_calls}),2)
        self.assertEqual(len({r['utc_time'] for r in next_calls}),1)
        result=self.move('2026-10-08T13:00:00-05:00')
        self.assertEqual(result['reschedule_revision'],2)
        self.assertTrue(self.occurrences.agenda(self.key,'bob')['rescheduled'])
        self.now=stamp('2026-10-08T18:00:00Z'); self.scheduler.tick()
        self.assertEqual(list(self.queue.load()),[result['request_key']])
        self.assertEqual(len(self.scheduler.load()['occurrences'][self.key]['reschedule_history']),2)

    def test_cli_reschedule_uses_existing_occurrence_key_and_explicit_offset(self):
        # The subprocess uses the real clock, independent of this fixture clock.
        scheduler=Scheduler(self.team)  # Default clock bound before the patch.
        key=scheduler.next(1)[0]['occurrence_key']
        at=(datetime.now(timezone.utc)+timedelta(days=2)).replace(second=0,microsecond=0).isoformat()
        command=[sys.executable,str(ROOT/'voip.py'),'--root',str(self.root),'--session','team',
                 '--json','schedule','reschedule',key,'--recipient','bob','--at',at,'--note','Move only once']
        run=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        result=json.loads(run.stdout)['data']
        self.assertEqual(result['outcome'],'rescheduled')
        self.assertEqual(result['occurrence_key'],key)
        self.assertFalse(result['recurring_schedule_changed'])
