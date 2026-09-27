import json
import time
from unittest.mock import patch, MagicMock
from service_fixture import ServiceFixture
from requests_queue import Requests, Deferred
from request_runtime import Runtime
from operator_mailbox import save_json
from voip import Project, atomic_write
from call_events import CallEvents
from outgoing_calls import Calls

class RuntimeTests(ServiceFixture):
    def setUp(self):
        super().setUp(); self.runtime=Runtime(self.project)
        self.state=dict(ready=True,connected=False,same_recipient=False,backend_busy=False,quiet=True,phone_busy=False)
    def record(self,**kwargs): return Requests(self.team).enqueue('r','message','Review agenda',**kwargs)
    def test_offcall_message_normal_native_turn_in_selected_thread(self):
        record=self.record()
        rpc=MagicMock(); rpc.request.return_value={'turn':{'id':'turn-1'}}
        with patch.object(self.runtime,'state',return_value=self.state), patch('request_runtime.Rpc') as factory:
            factory.return_value.__enter__.return_value=rpc
            self.assertEqual(self.runtime.prompt(record,self.state)['method'],'turn')
        method, params=rpc.request.call_args.args
        self.assertEqual(method,'turn/start'); self.assertEqual(params['threadId'],self.team.thread_id())
        self.assertIn('session team',params['input'][0]['text'])
    def test_priority_busy_backend_steers_matching_turn(self):
        record=self.record(mode='preemptive'); self.state['backend_busy']=True
        rpc=MagicMock(); rpc.request.side_effect=[{'data':[{'id':'t','status':'inProgress'}]},{}]
        with patch.object(self.runtime,'state',return_value=self.state), patch('request_runtime.Rpc') as factory:
            factory.return_value.__enter__.return_value=rpc
            self.runtime.prompt(record,self.state)
        method,params=rpc.request.call_args.args
        self.assertEqual(method,'turn/steer'); self.assertEqual(params['expectedTurnId'],'t')
        self.assertFalse(any(c.args[0]=='turn/interrupt' for c in rpc.request.call_args_list))
    def test_connected_request_uses_realtime_and_retains_phone_call(self):
        record=self.record(mode='preemptive'); self.state.update(connected=True,call_id='c',same_recipient=True)
        rpc=MagicMock()
        with patch.object(self.runtime,'state',return_value=self.state), patch('request_runtime.Rpc') as factory:
            factory.return_value.__enter__.return_value=rpc
            self.assertEqual(self.runtime.prompt(record,self.state,reuse=True)['call_id'],'c')
        self.assertEqual(rpc.request.call_args.args[0],'thread/realtime/appendText')
        self.assertIn('Keep this call connected',rpc.request.call_args.args[1]['text'])
    def test_cancel_before_rpc_never_submits(self):
        record=self.record(); Requests(self.team).update('r',state='cancelled')
        with patch('request_runtime.Rpc') as factory:
            with self.assertRaises(Deferred): self.runtime.prompt(record,self.state)
            factory.assert_not_called()
    def test_changed_connected_call_defers_without_rpc(self):
        record=self.record(mode='preemptive'); observed={**self.state,'call_id':'old'}
        self.state.update(connected=True,same_recipient=True,call_id='new')
        with patch.object(self.runtime,'state',return_value=self.state), patch('request_runtime.Rpc') as factory:
            with self.assertRaises(Deferred): self.runtime.prompt(record,observed,reuse=True)
            factory.assert_not_called()
    def test_reset_once_survives_normal_retry_and_commit_before_crash(self):
        (self.root/'sessions/team.toml').write_text('reset_on_outgoing_call=true\n')
        project=Project(self.root,'team'); old=project.thread_id(); new='22222222-2222-2222-2222-222222222222'
        def reset(p,scope): atomic_write(p.state/'operator-thread-id',new)
        with patch('operator_reset.reset_operator',side_effect=reset) as resetter:
            self.runtime.reset_once(project,'a'); self.runtime.reset_once(project,'a')
            resetter.assert_called_once()
        save_json(project.state/'call-resets.json',{'b':{'state':'resetting','old_thread_id':old,'started_at':1}})
        save_json(project.state/'operator-reset.json',{'old_thread_id':old,'new_thread_id':new,'started_at':2})
        with patch('operator_reset.reset_operator') as resetter:
            self.runtime.reset_once(project,'b'); resetter.assert_not_called()
    def test_quiet_uses_live_audio_activity_not_saved_transcript_times(self):
        record=self.record(); now=time.time()
        event={'observer_online':True,'observed_at':now,'calls':{'c':{'connected':True,'recipient_id':'alice','seen_at':now-10}}}
        voice={'voice':'connected','codex_process_alive':True}
        rpc=MagicMock(); rpc.request.return_value={'thread':{'status':{'type':'idle'}}}
        activity={'thread_id':self.team.thread_id(),'observed_at':now,'input_at':now-1,'output_at':now-9}
        with patch('request_runtime.operator_status',return_value=voice), patch('request_runtime.CallEvents') as events, patch('request_runtime.Rpc') as factory:
            events.return_value.load.return_value=event; factory.return_value.__enter__.return_value=rpc
            save_json(self.team.state/'voice-activity.json',activity)
            self.assertFalse(self.runtime.state(record)['quiet'])
            activity['input_at']=now-5; save_json(self.team.state/'voice-activity.json',activity)
            self.assertTrue(self.runtime.state(record)['quiet'])
            activity['observed_at']=now-10; save_json(self.team.state/'voice-activity.json',activity)
            self.assertFalse(self.runtime.state(record)['quiet'])

    def test_same_recipient_connected_or_pending_elsewhere_does_not_reserve_this_session(self):
        record = Requests(self.team).enqueue('r', 'call', 'Team review', recipient='alice')
        save_json(self.project.shared_state / 'twilio-provisioning.json',
                  {'account_sid': 'AC' + 'a' * 32})
        base = {'observer_online': True, 'observed_at': time.time(), 'calls': {}}
        save_json(CallEvents(self.team).path, base)
        save_json(CallEvents(self.project).path, {**base, 'calls': {
            'other-call': {'connected': True, 'recipient_id': 'alice', 'seen_at': time.time()}}})
        old = {'session_id': 'default', 'to': '+12025550101', 'state': 'unknown'}
        save_json(self.project.shared_state / 'outgoing-calls.json', {'elsewhere': old})
        rpc = MagicMock(); rpc.request.return_value = {'thread': {'status': {'type': 'idle'}}}
        with patch('request_runtime.operator_status', return_value={'voice':'connected','codex_process_alive':True}), \
                patch('request_runtime.Rpc') as factory, patch.object(Calls, 'status') as status:
            factory.return_value.__enter__.return_value = rpc
            state = self.runtime.state(record)
            self.assertTrue(state['ready'])
            self.assertFalse(state['phone_busy'])
            self.assertFalse(state['connected'])
            status.assert_not_called()
            # The same unresolved attempt in this profile must still block dialing.
            old['session_id'] = 'team'
            save_json(self.project.shared_state / 'outgoing-calls.json', {'here': old})
            status.return_value = [old]
            self.assertTrue(self.runtime.state(record)['phone_busy'])
            status.assert_called_once_with('here')
