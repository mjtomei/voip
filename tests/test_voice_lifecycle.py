"""Demand lifetime and hangup delivery; no provider, model, or real phone calls."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from call_events import CallEvents
from service_fixture import single_recipient
from voice_lifecycle import acquire,demand,release,serve,ended_notice
from voip import Project,ROOT,atomic_write

class FakeRpc:
    calls=[]
    status='idle'
    fail=False
    def __init__(self,*args): self.closed=False
    async def start(self): pass
    async def close(self): self.closed=True
    async def request(self,method,params):
        self.calls.append((method,params))
        if method in {'thread/read','thread/resume'}:return {'thread':{'status':{'type':self.status}}}
        if method=='thread/turns/list':return {'data':[{'id':'working-turn','status':'inProgress'}]}
        if self.fail and method in {'turn/start','turn/steer'}:raise OSError('ambiguous response loss')
        return {'turn':{'id':'review-turn'}}

class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'config.toml').write_text((ROOT/'config.example.toml').read_text());single_recipient(self.root)
        self.p=Project(self.root);atomic_write(self.p.state/'operator-thread-id','11111111-1111-1111-1111-111111111111')
        self.events=CallEvents(self.p);self.events.snapshot({})
        FakeRpc.calls=[];FakeRpc.status='idle';FakeRpc.fail=False
        self.opened=[];self.closed=[];self.block_start=False
        outer=self
        class Session:
            def __init__(self,p):
                self.thread=p.thread_id();self.microphone=self.speaker=SimpleNamespace(last_signal_at=0)
            async def start(self):
                outer.opened.append(self.thread)
                if outer.block_start:await asyncio.Event().wait()
            async def wait(self,stop):await stop.wait()
            async def close(self):outer.closed.append(self.thread)
        self.factory=Session;self.stop=asyncio.Event();self.worker=None
    async def asyncTearDown(self):
        self.stop.set()
        if self.worker:await asyncio.wait_for(self.worker,3)
    async def until(self,test):
        async with asyncio.timeout(4):
            while not test():await asyncio.sleep(.02)
    def run_controller(self):self.worker=asyncio.create_task(serve(self.p,self.stop,self.factory,FakeRpc))
    def connect(self,key='call'):
        self.events.snapshot({key:True},recipients={key:'alice'})
    async def test_idle_incoming_hangup_and_second_call_keep_saved_conversation(self):
        self.run_controller();await asyncio.sleep(.35)
        self.assertFalse(self.opened);self.assertFalse(FakeRpc.calls)
        self.connect();await self.until(lambda:len(self.opened)==1)
        self.events.snapshot({});await self.until(lambda:len(self.closed)==1)
        await self.until(lambda:any(m=='turn/start' for m,_ in FakeRpc.calls))
        self.assertFalse(any('realtime' in m or m=='turn/interrupt' for m,_ in FakeRpc.calls))
        await asyncio.sleep(.35);self.assertEqual(len(self.opened),1)
        self.connect('second');await self.until(lambda:len(self.opened)==2)
        self.assertEqual(self.opened,[self.p.thread_id()]*2)
    async def test_outgoing_lease_is_consumed_by_call_and_not_retained_after_hangup(self):
        acquire(self.p,'attempt');self.run_controller();await self.until(lambda:len(self.opened)==1)
        self.connect();await self.until(lambda:not json.loads((self.p.state/'voice-demand.json').read_text()))
        self.events.snapshot({});await self.until(lambda:len(self.closed)==1)
        self.assertFalse(demand(self.p))
    async def test_expired_or_cancelled_lease_closes_and_never_reopens(self):
        acquire(self.p,'attempt',seconds=.6);self.run_controller()
        await self.until(lambda:len(self.opened)==1);await self.until(lambda:len(self.closed)==1)
        await asyncio.sleep(.35);self.assertEqual(len(self.opened),1)
        acquire(self.p,'cancelled');await self.until(lambda:len(self.opened)==2)
        release(self.p,'cancelled');await self.until(lambda:len(self.closed)==2)
    async def test_hangup_during_negotiation_closes_the_partial_session(self):
        self.block_start=True;self.connect();self.run_controller()
        await self.until(lambda:len(self.opened)==1);self.events.snapshot({})
        await self.until(lambda:len(self.closed)==1)
    async def test_observer_stale_does_not_keep_audio_open(self):
        self.connect();self.run_controller();await self.until(lambda:len(self.opened)==1)
        data=self.events.load();data['observed_at']=time.time()-20
        atomic_write(self.events.path,json.dumps(data));await self.until(lambda:len(self.closed)==1)
    async def test_invalid_thread_and_expired_leases_do_not_wake(self):
        acquire(self.p,'wrong');path=self.p.state/'voice-demand.json';d=json.loads(path.read_text())
        d['wrong']['thread_id']='other';atomic_write(path,json.dumps(d));self.assertFalse(demand(self.p))
        acquire(self.p,'expired',seconds=.01);await asyncio.sleep(.03);self.assertFalse(demand(self.p))
    async def test_busy_hangup_steers_existing_work_and_ambiguous_submission_is_not_repeated(self):
        self.connect();self.events.snapshot({});FakeRpc.status='active';FakeRpc.fail=True
        await ended_notice(self.p,FakeRpc);await ended_notice(self.p,FakeRpc)
        requests=[(m,p) for m,p in FakeRpc.calls if m in {'turn/start','turn/steer'}]
        self.assertEqual(len(requests),1);self.assertEqual(requests[0][0],'turn/steer')
        self.assertEqual(requests[0][1]['expectedTurnId'],'working-turn')
        self.assertEqual(self.events.load()['events'][-1]['deliveries'][self.p.thread_id()]['state'],'uncertain')
    async def test_pause_drops_demand_without_deleting_conversation(self):
        self.connect();self.run_controller();await self.until(lambda:len(self.opened)==1)
        atomic_write(self.p.state/'operator-paused','paused')
        await self.until(lambda:len(self.closed)==1)
        self.assertEqual(self.p.thread_id(),'11111111-1111-1111-1111-111111111111')

class OutgoingDemandTest(unittest.TestCase):
    def test_failed_or_cancelled_phone_attempt_releases_warmup(self):
        from test_outgoing_calls import OutgoingTest
        from voice_lifecycle import acquire,demand
        for outcome in ['no-answer','cancel']:
            with self.subTest(outcome=outcome):
                fixture=OutgoingTest();fixture.setUp()
                try:
                    key='demand-check'
                    fixture.preparations.preload.side_effect=lambda record:acquire(fixture.calls.project,key)
                    fixture.calls.start(key,'Synthetic demand lifecycle')
                    self.assertTrue(demand(fixture.calls.project))
                    if outcome=='cancel':fixture.calls.cancel(key)
                    else:
                        fixture.api.phone['status']=outcome;fixture.calls.status(key)
                    self.assertFalse(demand(fixture.calls.project))
                finally:fixture.temp.cleanup()
