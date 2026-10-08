from datetime import datetime, timezone
from unittest.mock import patch
import json
from service_fixture import ServiceFixture
from call_schedule import Scheduler, Cron
from requests_queue import Requests
from voip import SetupError

def stamp(iso): return datetime.fromisoformat(iso).timestamp()

class ScheduleTests(ServiceFixture):
    def setUp(self):
        super().setUp(); self.now=stamp('2026-09-19T16:00:00+00:00')
        (self.root/'agenda.txt').write_text('Ask about the review outcome.')
        self.write_schedule()
        self.scheduler=Scheduler(self.project,clock=lambda:self.now)
    def write_schedule(self,cron='* * * * *',zone='America/Los_Angeles',extra=''):
        (self.root/'schedules.toml').write_text(f'''[[schedules]]
id="review"
cron="{cron}"
timezone="{zone}"
session="team"
recipient="bob"
reason="Review outcome"
agenda_file="agenda.txt"
{extra}
''')
    def test_same_occurrence_only_one_mailbox_message_and_request(self):
        first=self.scheduler.tick(); self.assertEqual(len(first),1)
        self.assertEqual(self.scheduler.tick(),[])
        records=Requests(self.project).load(); self.assertEqual(len(records),1)
        record=next(iter(records.values())); self.assertEqual(record['spec']['session_id'],'team')
        self.assertEqual(record['spec']['source'],'schedule:review')
        self.assertEqual(record['spec']['mode'],'non_preemptive')
    def test_dry_run_has_no_durable_side_effect(self):
        self.assertEqual(len(self.scheduler.tick(dry_run=True)),1)
        self.assertFalse(self.scheduler.path.exists()); self.assertFalse(Requests(self.project).path.exists())
    def test_explicit_retry_list_and_extended_lifetime_snapshot_into_scheduled_request(self):
        self.write_schedule(extra='[schedules.redial]\ndelays_seconds=[60,300,480,3600]')
        self.scheduler.tick()
        record=next(iter(Requests(self.project).load().values()))
        self.assertEqual(record['spec']['redial']['delays_seconds'],[60,300,480,3600])
        self.assertEqual(record['spec']['redial']['max_attempts'],5)
        self.assertEqual(record['spec']['ttl_seconds'],7440)
        self.write_schedule(extra='[schedules.redial]\ndelays_seconds=[10]')
        self.assertEqual(Requests(self.project).read(record['key'])['spec']['redial']['delays_seconds'],[60,300,480,3600])
    def test_restart_between_mailbox_and_queue_recovers_old_minute(self):
        with patch('call_schedule.Requests.enqueue',side_effect=SetupError('injected crash')):
            with self.assertRaises(SetupError): self.scheduler.tick()
        self.now+=120
        result=self.scheduler.tick()
        self.assertEqual(sum(r['state']=='enqueued' for r in result),2) # pending plus current
        self.assertEqual(sum(r['state']=='skipped' for r in result),1)
        self.assertEqual(len(Requests(self.project).load()),2)
        state=json.loads(self.scheduler.path.read_text())
        self.assertEqual(sum(x['state']=='enqueued' for x in state['occurrences'].values()),2)
    def test_skip_missed_minutes_default(self):
        self.scheduler.tick(); self.now+=300; self.scheduler.tick()
        self.assertEqual(len(Requests(self.project).load()),2)
        missed = self.missed()
        self.assertEqual(len(missed),4)
        self.assertEqual(self.scheduler.tick(),[])
        self.assertEqual(self.missed(),missed)

    def missed(self, project=None, recipient='bob'):
        return Scheduler(project or self.team, clock=lambda:self.now).missed(recipient)

    def test_connection_can_recover_skip_count_before_dispatcher_without_enqueuing(self):
        self.scheduler.tick(); self.now+=300
        self.assertEqual(len(self.missed()),4)
        self.assertEqual(len(self.missed()),4)
        self.assertEqual(len(Requests(self.project).load()),1)
        self.scheduler.tick()
        self.assertEqual(len(self.missed()),4)
        self.assertEqual(len(Requests(self.project).load()),2)

    def test_crash_before_current_occurrence_write_still_counts_unmaterialized_minute(self):
        self.scheduler.tick(); self.now+=60
        (self.root/'agenda.txt').write_text('')
        with self.assertRaises(SetupError): self.scheduler.tick()
        self.now+=120
        self.assertEqual(len(self.missed()),2)
        self.assertEqual(len(Requests(self.project).load()),1)

    def test_skips_belong_to_their_session_and_recipient(self):
        self.scheduler.tick(); self.now+=300; self.scheduler.tick()
        self.assertEqual(self.missed(self.project),[])
        self.assertEqual(self.missed(recipient='alice'),[])
        self.assertEqual(self.missed(recipient=None),[])

    def test_no_history_invented_for_newly_enabled_schedule(self):
        (self.root/'schedules.toml').write_text('schedules=[]\n')
        self.scheduler.tick(); self.now+=300
        self.write_schedule(); self.scheduler.tick()
        self.assertEqual(self.missed(),[])
        self.assertEqual(len(Requests(self.project).load()),1)

    def test_gap_counts_follow_last_observed_schedule_before_config_change(self):
        self.scheduler.tick(); self.now+=300
        self.write_schedule(cron='0 9 * * *')
        self.scheduler.tick()
        self.assertEqual(len(self.missed()),4)

    def test_skip_recovery_dry_run_does_not_commit_or_enqueue(self):
        self.scheduler.tick(); self.now+=300
        previous = self.scheduler.path.read_text()
        result = self.scheduler.tick(dry_run=True)
        self.assertEqual(sum(r.get('state')=='skipped' for r in result),4)
        self.assertEqual(self.scheduler.path.read_text(),previous)
        self.assertEqual(len(Requests(self.project).load()),1)

    def test_both_dst_fold_occurrences_skipped_count_once(self):
        self.write_schedule(cron='30 1 * * *')
        self.now=stamp('2026-11-01T07:59:00+00:00'); self.scheduler.tick()
        self.now=stamp('2026-11-01T10:01:00+00:00'); self.scheduler.tick()
        self.assertEqual(len(self.missed()),1)
        self.assertEqual(Requests(self.project).load(),{})
    def test_explicit_misfire_catchup_bounded(self):
        self.write_schedule(extra='misfire="enqueue"\nmax_lateness_seconds=120')
        self.scheduler.tick(); self.now+=600; self.scheduler.tick()
        self.assertEqual(len(Requests(self.project).load()),4)
        self.assertEqual(len(self.missed()),7) # Too old even for explicit bounded catch-up.
    def test_dst_fallback_minute_only_once(self):
        self.write_schedule(cron='30 1 * * *')
        self.now=stamp('2026-11-01T08:30:00+00:00'); self.scheduler.tick()
        self.now=stamp('2026-11-01T09:30:00+00:00'); self.scheduler.tick()
        self.assertEqual(len(Requests(self.project).load()),1)
    def test_dst_nonexistent_spring_time_is_skipped(self):
        self.write_schedule(cron='30 2 * * *')
        self.now=stamp('2026-03-08T08:00:00+00:00')
        self.assertEqual(self.scheduler.next(1)[0]['local_time'],'2026-03-09T02:30:00-07:00')
    def test_numeric_cron_and_or_day_semantics(self):
        c=Cron('*/15 9-17 1 * 1')
        self.assertTrue(c.matches(datetime(2026,9,21,9,15))) # Monday but not first
        self.assertTrue(c.matches(datetime(2026,10,1,9,15))) # first but Thursday
        self.assertFalse(c.matches(datetime(2026,9,22,9,15)))
        for expression in ['60 * * * *','* * *','*/0 * * * *','* * * * MON']:
            with self.assertRaises(SetupError): Cron(expression)
    def test_disabled_schedule_creates_nothing(self):
        (self.root/'schedules.toml').write_text('[[schedules]]\nid="disabled"\nenabled=false\n')
        self.assertEqual(self.scheduler.tick(),[]); self.assertFalse(Requests(self.project).load())

    def test_live_reschedule_changes_future_occurrences_without_cancelling_pending_retry(self):
        self.write_schedule(cron='0 9 * * *')
        self.scheduler.tick()  # 09:00 local: the existing request keeps its identity.
        original = Requests(self.project).load()
        self.write_schedule(cron='0 18 * * 1-5')
        self.now += 60
        self.assertEqual(self.scheduler.tick(), [])
        upcoming = self.scheduler.next(1)[0]
        self.assertEqual(upcoming['local_time'], '2026-09-21T18:00:00-07:00')
        self.assertEqual(Requests(self.project).load(), original)
        self.now = stamp('2026-09-21T16:00:00+00:00')  # Old morning slot.
        self.assertEqual(self.scheduler.tick(), [])
        self.now = stamp('2026-09-22T01:00:00+00:00')  # New Monday evening slot.
        self.scheduler.tick()
        self.assertEqual(len(Requests(self.project).load()), 2)
        self.write_schedule(cron='0 18 * * 1-5', extra='enabled=false')
        self.assertEqual(self.scheduler.next(1), [])
        self.now += 86400
        self.assertEqual(self.scheduler.tick(), [])
        self.assertEqual(len(Requests(self.project).load()), 2)
