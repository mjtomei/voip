import json
import time
from unittest.mock import patch
from service_fixture import ServiceFixture
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests, Dispatcher, Deferred
from voip import SetupError

class FakeRuntime:
    def __init__(self):
        self.observed = dict(ready=True, connected=False, backend_busy=False, quiet=True, same_recipient=False, phone_busy=False)
        self.dials = []; self.prompts = []; self.cancelled = []
        self.attempts = {}; self.response = {'state':'active','phone_call_sid':'CA-test'}
        self.prompt_error = None
    def state(self, record): return dict(self.observed)
    def local_attempt(self, record):
        return self.attempts.get(record['attempts'][-1]['key']) if record['attempts'] else None
    def dial(self, record, key, cancelled):
        self.dials.append((record['spec']['session_id'], key))
        self.attempts[key] = dict(self.response)
        return dict(self.response)
    def prompt(self, record, state, reuse=False):
        if self.prompt_error: raise self.prompt_error
        self.prompts.append((record['key'], reuse))
        return {'call_id':state.get('call_id'), 'method':'fake'}
    def status(self, record, key): return self.attempts[key]
    def cancel(self, record, key):
        self.cancelled.append(key); self.attempts[key] = {'state':'ended','connected':False,'phone_status':'canceled'}
        return self.attempts[key]

class QueueTests(ServiceFixture):
    def setUp(self):
        super().setUp(); self.queue = Requests(self.project); self.runtime = FakeRuntime()
        self.now = time.time() + 0.1; self.dispatch = Dispatcher(self.project, self.runtime, clock=lambda:self.now)
    def enqueue(self, key='r', **kw):
        return self.queue.enqueue(key, kw.pop('kind','call'), 'Review work', **kw)
    def test_stable_key_is_idempotent_and_retains_mailbox_snapshot(self):
        self.enqueue(); Mailbox(self.project).post('w','s','new body','new')
        self.assertEqual(self.enqueue()['spec']['message_ids'], [])
        with self.assertRaises(SetupError): self.enqueue(mode='preemptive')
    def test_legacy_phone_keys_cannot_redial_after_migration(self):
        save_json(self.project.shared_state/'outgoing-calls.json', {'r':{'state':'ended'}})
        with self.assertRaises(SetupError): self.enqueue()
        self.assertFalse(self.runtime.dials)
    def test_same_recipient_existing_call_reused_without_dial_or_reset(self):
        self.enqueue(); self.runtime.observed.update(connected=True, same_recipient=True, call_id='old')
        r = self.dispatch.process('r')
        self.assertEqual(r['state'],'prompt_submitted')
        self.assertEqual(self.runtime.prompts,[('r',True)]); self.assertFalse(self.runtime.dials)
    def test_nonpreemptive_waits_for_both_idle_backend_and_quiet_audio(self):
        self.enqueue(); self.runtime.observed.update(connected=True, same_recipient=True)
        for busy, quiet in [(True,True),(False,False)]:
            self.runtime.observed.update(backend_busy=busy,quiet=quiet)
            self.assertEqual(self.dispatch.process('r')['state'],'queued')
        self.assertFalse(self.runtime.prompts)
        self.runtime.observed.update(backend_busy=False,quiet=True)
        self.dispatch.process('r'); self.assertEqual(len(self.runtime.prompts),1)
    def test_preemptive_context_immediate_but_no_task_cancellation(self):
        self.enqueue(mode='preemptive'); self.runtime.observed.update(connected=True,same_recipient=True,backend_busy=True,quiet=False)
        self.dispatch.process('r')
        self.assertEqual(self.runtime.prompts,[('r',True)]); self.assertFalse(self.runtime.cancelled)
    def test_different_caller_or_pending_attempt_in_same_session_wait(self):
        self.enqueue(mode='preemptive'); self.runtime.observed.update(connected=True,same_recipient=False)
        self.dispatch.process('r'); self.assertFalse(self.runtime.prompts)
        self.runtime.observed.update(connected=False,phone_busy=True)
        self.dispatch.process('r'); self.assertFalse(self.runtime.dials)
    def test_two_profiles_can_dispatch_to_different_recipients(self):
        self.enqueue(); Requests(self.team).enqueue('second','call','Work',recipient='bob')
        self.dispatch.process('r'); self.dispatch.process('second')
        self.assertEqual(self.runtime.dials,[('default','r'),('team','second')])
    def test_two_profiles_can_dispatch_to_same_recipient(self):
        self.enqueue(); Requests(self.team).enqueue('second','call','Work',recipient='alice')
        self.dispatch.process('r'); self.dispatch.process('second')
        self.assertEqual(self.runtime.dials,[('default','r'),('team','second')])
        self.assertEqual(self.queue.read('r')['spec']['to'], self.queue.read('second')['spec']['to'])
    def test_offcall_priority_notice_does_not_suppress_later_connected_prompt(self):
        self.enqueue(mode='preemptive'); self.runtime.observed.update(backend_busy=True)
        self.dispatch.process('r'); self.dispatch.process('r')
        self.assertEqual(self.runtime.prompts,[('r',False)])
        self.runtime.observed.update(connected=True,same_recipient=True,call_id='joined')
        self.dispatch.process('r'); self.assertEqual(self.runtime.prompts,[('r',False),('r',True)])
    def test_deferral_safe_to_retry_unknown_submission_blocks(self):
        self.enqueue(kind='message'); self.runtime.prompt_error=Deferred('busy')
        self.assertEqual(self.dispatch.process('r')['prompt_state'],None)
        self.runtime.prompt_error=SetupError('transport lost')
        self.assertEqual(self.dispatch.process('r')['state'],'blocked')
        self.runtime.prompt_error=None; self.dispatch.process('r')
        self.assertFalse(self.runtime.prompts)
    def test_crash_between_prompt_submission_and_ack_is_not_repeated(self):
        self.enqueue(kind='message'); self.queue.update('r',prompt_state='submitting')
        self.assertEqual(self.dispatch.process('r')['state'],'blocked')
        self.assertFalse(self.runtime.prompts)
    def test_crash_after_phone_submission_recovers_existing_attempt(self):
        self.enqueue(); self.dispatch.process('r'); self.queue.update('r',state='dispatching')
        Dispatcher(self.project,self.runtime).process('r')
        self.assertEqual(len(self.runtime.dials),1)
    def test_bounded_redial_and_delay_only_after_known_no_answer(self):
        self.enqueue(redial={'max_attempts':2,'delay_seconds':10})
        self.runtime.response={'state':'ended','phone_status':'no-answer','connected':False}
        self.assertEqual(self.dispatch.process('r')['state'],'retry_wait')
        self.dispatch.process('r'); self.assertEqual(len(self.runtime.dials),1)
        self.now+=11
        self.assertEqual(self.dispatch.process('r')['state'],'failed')
        self.assertEqual(len(self.runtime.dials),2)
        self.assertNotEqual(self.runtime.dials[0][1],self.runtime.dials[1][1])
    def test_explicit_delays_are_after_failure_and_persist_across_dispatcher_restart(self):
        self.enqueue(redial={'delays_seconds':[60,300,480,3600]}, ttl_seconds=10000)
        self.runtime.response={'state':'ended','phone_status':'no-answer','connected':False}
        for delay in [60,300,480,3600]:
            r=self.dispatch.process('r')
            self.assertEqual(r['next_attempt_at'],self.now+delay)
            self.assertEqual(r['state'],'retry_wait')
            self.dispatch=Dispatcher(self.project,self.runtime,clock=lambda:self.now)
            self.now+=delay-1
            count=len(self.runtime.dials);self.dispatch.process('r');self.assertEqual(len(self.runtime.dials),count)
            self.now+=1
        self.assertEqual(self.dispatch.process('r')['state'],'failed')
        self.assertEqual(len(self.runtime.dials),5)

    def test_connected_hangup_never_redials_and_does_not_address_mail(self):
        msg=Mailbox(self.project).post('w','s','body','m'); self.enqueue(redial={'max_attempts':3})
        self.runtime.response={'state':'ended','phone_status':'completed','connected':True}
        self.assertEqual(self.dispatch.process('r')['state'],'completed')
        self.dispatch.process('r'); self.assertEqual(len(self.runtime.dials),1)
        self.assertEqual(Mailbox(self.project).read('m')['status'],'unaddressed')
    def test_ambiguous_phone_submission_never_retries(self):
        self.enqueue(redial={'max_attempts':3}); self.runtime.response={'state':'unknown'}
        self.assertEqual(self.dispatch.process('r')['state'],'blocked')
        self.dispatch.process('r'); self.assertEqual(len(self.runtime.dials),1)
    def test_expiry_waiting_and_active_call_continues_past_expiry(self):
        self.enqueue(ttl_seconds=1); self.now+=2
        self.assertEqual(self.dispatch.process('r')['state'],'expired'); self.assertFalse(self.runtime.dials)
        self.enqueue('active',ttl_seconds=1); self.now=time.time(); self.dispatch.process('active'); self.now+=100
        self.assertEqual(self.dispatch.process('active')['state'],'active')
    def test_cancelled_inflight_attempt_is_recovered_without_redial(self):
        self.enqueue(); self.dispatch.process('r'); self.queue.update('r',state='cancelled')
        result=self.dispatch.process('r')
        self.assertEqual(result['state'],'cancelled'); self.assertTrue(result['cancel_settled'])
        self.assertEqual(self.runtime.cancelled,['r'])
        self.queue.update('r',state='active'); self.assertEqual(self.queue.read('r')['state'],'cancelled')
    def test_already_addressed_agenda_suppresses_call(self):
        box=Mailbox(self.project); box.post('w','s','body','m'); self.enqueue()
        box.address('m','handled elsewhere')
        self.assertEqual(self.dispatch.process('r')['outcome'],'agenda_already_addressed')
        self.assertFalse(self.runtime.dials)
    def test_invalid_schedule_does_not_block_ordinary_requests(self):
        import threading
        self.enqueue(kind='message'); stop=threading.Event()
        original=self.runtime.prompt
        def submitted(*args,**kw):
            result=original(*args,**kw); stop.set(); return result
        self.runtime.prompt=submitted
        timer=threading.Timer(2,stop.set); timer.start()
        try:
            with patch('call_schedule.Scheduler.tick',side_effect=SetupError('invalid cron')):
                self.dispatch.run(stop)
        finally:
            timer.cancel()
        self.assertEqual(self.runtime.prompts,[('r',False)])
        self.assertEqual(self.queue.read('r')['state'],'prompt_submitted')

    def test_parallel_workers_do_not_lose_request_records(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i:self.enqueue('parallel-'+str(i),kind='message'), range(24)))
        self.assertEqual(len(self.queue.load()),24)
