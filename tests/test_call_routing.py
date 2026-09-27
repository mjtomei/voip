import copy
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
from service_fixture import ServiceFixture
from voip import ROOT,SetupError,atomic_write
from call_events import CallEvents,call_key
from call_history import History
from call_control import Control,transfers
from twilio_source import function_source

SID='CA'+'1'*32
ACCOUNT='AC'+'a'*32
class RoutingTest(ServiceFixture):
    def setUp(self):
        super().setUp()
        atomic_write(self.project.state/'twilio-provisioning.json',json.dumps({'account_sid':ACCOUNT,'function_domain':'test.twil.io',
            'pin_sync_service_sid':'IS'+'a'*32,'functions':{'call-status':{},'handoff':{'sha256':hashlib.sha256(function_source(ROOT,'handoff')).hexdigest()}}}))
        atomic_write(self.project.state/'twilio-call-request-secret','ab'*32)
        self.api=Mock();self.history=History(self.project,self.api)

    def test_journal_conflict_retries_preserve_terminal_state_and_original_attempt_time(self):
        now=int(time.time());old={'created_at':now-100,'status':'completed','ended_at':now-70,'connected':True,'authenticated':True}
        self.history.document=Mock(return_value={'data':{'v':1,'calls':{SID:old}},'revision':'r1'})
        self.api.request.side_effect=[SetupError('HTTP 412, conflict'),{}]
        self.history.merge('alice',SID,{'created_at':now-100,'status':'ringing','connected':False})
        saved=json.loads(self.api.request.call_args.args[1]['Data'])['calls'][SID]
        self.assertEqual(saved['created_at'],now-100);self.assertEqual(saved['status'],'completed');self.assertTrue(saved['connected'])
        self.assertEqual(self.api.request.call_count,2)

    def test_missed_notice_is_durable_scoped_and_not_repeated(self):
        now=int(time.time());record={'call_sid':SID,'recipient_id':'alice','session_id':'team','created_at':now-100,
            'ended_at':now-60,'direction':'outgoing','status':'no-answer','authenticated':False,'connected':False}
        self.history.document=lambda recipient:{'data':{'calls':{SID:record} if recipient=='alice' else {}}}
        events=CallEvents(self.team);events.snapshot({});self.history.poll();self.history.poll()
        state=events.load();self.assertEqual(len(state['events']),1);event=events.pending(self.team.thread_id(),kinds={'missed'})
        self.assertEqual(event['recipient_id'],'alice');self.assertIn('not a request to call',events.message(event))
        self.assertFalse(CallEvents(self.project).load()['events'])
        events.delivery(event['id'],self.team.thread_id(),'submitted');self.assertIsNone(events.pending(self.team.thread_id(),kinds={'missed'}))
        self.assertEqual(self.history.list('team','alice'),[record]);self.assertEqual(self.history.list('team','bob'),[])

    def test_successful_callback_consumes_only_its_bound_outgoing_attempt(self):
        now=int(time.time());original='CA'+'2'*32;newer='CA'+'3'*32
        rows={original:{'call_sid':original,'recipient_id':'alice','session_id':'team','direction':'outgoing',
                        'accepted':True,'created_at':now-90},
              newer:{'call_sid':newer,'recipient_id':'alice','session_id':'default','direction':'outgoing',
                     'accepted':True,'created_at':now-10},
              SID:{'call_sid':SID,'recipient_id':'alice','session_id':'team','direction':'incoming',
                   'authenticated':True,'connected':False,'created_at':now-30,'callback_of':original}}
        self.history.document=lambda person:{'data':{'calls':copy.deepcopy(rows) if person=='alice' else {}}}
        self.history.merge=Mock(side_effect=lambda person,sid,values:rows[sid].update(values))
        self.assertIsNone(self.history.consume_callback(rows[SID]))
        events=CallEvents(self.team)
        events.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'callback',
                      'peeruri':'sip:voip-in-alice-'+SID+'@test.invalid',
                      'accountaor':f"sip:{self.team.sip['username']}@{self.team.sip['domain']}"})
        self.history.poll()
        self.assertEqual(rows[original]['callback_call_sid'],SID)
        self.assertNotIn('callback_consumed_at',rows[newer])
        calls=self.history.merge.call_count;self.history.poll()
        self.assertEqual(self.history.merge.call_count,calls)
        self.assertTrue(next(r for r in self.history.list('team','alice') if r['call_sid']==original)['callback_consumed_at'])
        self.assertIsNone(self.history.consume_callback({**rows[SID],'session_id':'default'}))

    def test_incoming_greeting_precedes_mailbox_work_and_is_not_repeated(self):
        from operator_mailbox import Mailbox
        self.project.workspace=Path('/srv/helios')
        Mailbox(self.project,'alice').post('worker','Question','Please review the plan.','agenda')
        e=CallEvents(self.project)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'greeting',
                 'peeruri':'sip:voip-in-alice-'+SID+'@test.invalid',
                 'accountaor':f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"})
        event=e.pending(self.project.thread_id());text=e.message(event)
        self.assertIn("You're with the helios directory operator. How can I help?",text)
        self.assertIn('Do not wait for the caller to speak',text)
        self.assertLess(text.index("You're with"),text.index('mailbox --recipient alice list'))
        e.delivery(event['id'],self.project.thread_id(),'submitted','first-voice')
        self.assertNotIn("You're with",e.message(e.load()['events'][0]))
        self.assertIn('do not repeat the greeting',e.message(e.load()['events'][0]))

    def test_observed_connection_prevents_false_miss_when_a_redirect_skips_dial_callback(self):
        now=int(time.time());record={'call_sid':SID,'recipient_id':'alice','session_id':'team','created_at':now-100,
            'ended_at':now-60,'direction':'incoming','status':'completed','authenticated':True,'connected':False}
        self.history.document=lambda recipient:{'data':{'calls':{SID:record} if recipient=='alice' else {}}}
        self.history.merge=Mock()
        e=CallEvents(self.team)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'observed-call',
            'peeruri':'sip:voip-in-alice-'+SID+'@test.invalid',
            'accountaor':f"sip:{self.team.sip['username']}@{self.team.sip['domain']}"})
        self.history.poll()
        self.history.merge.assert_called_once_with('alice',SID,{'connected':True,'authenticated':True})
        self.assertFalse(any(event['kind']=='missed' for event in e.load()['events']))
        self.assertTrue(self.history.list('team','alice')[0]['connected'])

    def test_missed_notice_does_not_interrupt_another_caller(self):
        self.test_missed_notice_is_durable_scoped_and_not_repeated()
        events=CallEvents(self.team);state=events.load();event=state['events'][0];event['deliveries']={}
        state['calls']['active']={'connected':True,'recipient_id':'bob','seen_at':time.time()}
        atomic_write(events.path,json.dumps(state))
        self.assertIsNone(events.pending(self.team.thread_id(),kinds={'missed'}))

    def test_incoming_and_transfer_identity_bind_to_phone_parent(self):
        e=CallEvents(self.project);peer='sip:voip-in-alice-'+SID+'@test.invalid'
        self.assertEqual(e.recipient(peer),'alice');self.assertEqual(e.phone_sid(peer),SID)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'local1','peeruri':peer,
            'accountaor':f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"})
        self.assertEqual(e.load()['calls'][call_key('local1')]['phone_call_sid'],SID)

    def test_callback_authentication_is_distinct_from_a_missed_attempt(self):
        now=int(time.time())
        old={'call_sid':'CA'+'2'*32,'session_id':'default','recipient_id':'alice','created_at':now-90,
             'ended_at':now-60,'direction':'outgoing','status':'completed',
             'authenticated':False,'connected':False}
        atomic_write(self.project.state/'call-history.json',json.dumps({'calls':{'past':old}}))
        e=CallEvents(self.project)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'callback',
                 'peeruri':'sip:voip-in-alice-'+SID+'@test.invalid',
                 'accountaor':f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"})
        event=e.pending(self.project.thread_id());text=e.message(event)
        self.assertIn('current call has already passed telephone keypad PIN verification',text)
        self.assertIn('pin_verified_on_that_past_attempt',text)
        self.assertIn('do not describe authentication of the current call',text)
        self.assertNotIn('"authenticated": false',text)
        self.assertIn('Never ask the caller to say or provide a PIN',text)
        self.project.profile['prompts']['connection']='Welcome to the project.'
        self.assertIn('current call has already passed telephone keypad PIN verification',e.message(event))
        self.assertIn('Never ask the caller to say or provide a PIN',e.message(event))

    def test_unknown_call_does_not_claim_pin_verification_or_request_a_secret(self):
        e=CallEvents(self.project)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'unknown',
                 'peeruri':'sip:unknown@test.invalid',
                 'accountaor':f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"})
        text=e.message(e.pending(self.project.thread_id()))
        self.assertNotIn('has already passed telephone keypad PIN verification',text)
        self.assertIn('Never ask the caller to say or provide a PIN',text)

    def test_arriving_transfer_names_destination_and_preserves_handoff_context(self):
        identity='11111111-1111-4111-8111-111111111111'
        record={'id':identity,'target_session':'team','source_session':'default',
                'target_thread':self.team.thread_id(),'recipient_id':'alice','call_sid':SID,
                'created':time.time(),'note':'Discuss silver compass.'}
        atomic_write(self.project.provider_state/'call-transfers.json',json.dumps({'test-transfer':record}))
        self.team.workspace=Path('/srv/helios')
        e=CallEvents(self.team)
        e.event({'event':True,'class':'call','type':'CALL_ESTABLISHED','id':'arriving',
                 'peeruri':'sip:voip-'+identity+'@test.invalid',
                 'accountaor':f"sip:{self.team.sip['username']}@{self.team.sip['domain']}"})
        event=e.pending(self.team.thread_id());text=e.message(event)
        self.assertIn("Transfer complete. You're now with the helios directory operator.",text)
        self.assertIn('Discuss silver compass.',text)
        self.assertIn('Caller transferred from default to session team',text)
        self.assertIn('current call has already passed telephone keypad PIN verification',text)
        self.assertIn('restriction applies only to the opening',text)
        self.assertIn('immediately delegate that request',text)
        self.assertIn('call transfer --call-id '+event['call']+' --to-home',text)
        self.team.workspace=Path.home().resolve()
        self.assertEqual(e.prepared_opening(event)['brief']['opening'],
                         "Transfer complete. You're now with the home directory operator.")
        e.delivery(event['id'],self.team.thread_id(),'submitted','voice-one')
        refreshed=e.load()['events'][0]
        self.assertIn('do not repeat it unless asked',e.message(refreshed))
        self.assertNotIn('Transfer complete.',e.message(refreshed))

    def control(self):
        control=Control(self.project,self.api);now=time.time()
        parent={'sid':SID,'account_sid':ACCOUNT,'from':'+12025550101','to':'+12025550100','direction':'inbound','status':'in-progress'}
        key=call_key('local1');events=CallEvents(self.project)
        atomic_write(events.path,json.dumps({'calls':{key:{'connected':True,'recipient_id':'alice','seen_at':now,'phone_call_sid':SID}},
            'events':[],'observer_online':True,'observed_at':now}))
        CallEvents(self.team).snapshot({})
        child={'sid':'CA'+'2'*32,'parent_call_sid':SID,'to':f"sip:{self.project.sip['username']}@{self.project.sip['domain']}",'status':'in-progress'}
        self.api.items.side_effect=lambda url,kind:[] if '/Calls.json?' in url and 'Status=' in url and not 'in-progress' in url else [child]
        self.api.request.return_value=parent
        return control,key,parent,child

    def test_control_refuses_new_or_unbound_phone_call_and_unknown_local_identity(self):
        c,key,parent,child=self.control()
        self.assertEqual(c.current(key)[1]['sid'],SID)
        child['parent_call_sid']='CA'+'3'*32
        with self.assertRaises(SetupError):c.current(key)
        with self.assertRaises(SetupError):c.current('other')
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_transfer_rechecks_source_and_never_repeats_unknown_provider_post(self):
        c,key,parent,child=self.control()
        self.api.request.side_effect=lambda url,fields=None:parent if fields is None else (_ for _ in ()).throw(SetupError('timeout'))
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('outgoing_calls.Calls.provider_busy',return_value=False):
            with self.assertRaisesRegex(SetupError,'Transfer unknown'):c.transfer(key,'team','move1','Caller wants the team project')
            self.assertEqual(transfers(self.project)['move1']['state'],'unknown')
            count=sum(len(call.args)>1 for call in self.api.request.call_args_list)
            self.assertEqual(count,1)
            c.transfer(key,'team','move1','Caller wants the team project')
            self.assertEqual(sum(len(call.args)>1 for call in self.api.request.call_args_list),1)
            with self.assertRaises(SetupError):c.transfer(key,'team','move2','Caller wants the team project')
        record=transfers(self.project)['move1'];events=CallEvents(self.team)
        self.assertEqual(events.recipient('sip:voip-'+record['id']+'@test.invalid'),'alice')
        self.assertEqual(events.phone_sid('sip:voip-'+record['id']+'@test.invalid'),SID)
        self.assertTrue(events.prepared_opening({'outgoing_request_id':record['id'],'occurred_at':time.time()})['transfer'])

    def test_successful_transfer_preserves_parent_and_can_be_confirmed(self):
        c,key,parent,child=self.control()
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('outgoing_calls.Calls.provider_busy',return_value=False):
            result=c.transfer(key,'team','move1','Discuss amber river')
            self.assertEqual(result['state'],'submitted');self.assertEqual(result['call_sid'],SID)
            update=next(call for call in self.api.request.call_args_list if len(call.args)>1)
            self.assertTrue(update.args[0].endswith(SID+'.json'));self.assertIn('/handoff?ticket=',update.args[1]['Url'])
            self.assertNotIn('ticket',result)
            with self.assertRaises(SetupError):c.transfer(key,'team','move2','Discuss amber river')
            child['to']=f"sip:{self.team.sip['username']}@{self.team.sip['domain']}"
            self.assertEqual(c.status('move1')['observed_state'],'target_connected')
            self.assertEqual(transfers(self.project)['move1']['state'],'connected')

    def test_default_alias_transfers_to_home_and_retry_keeps_original_target(self):
        from directory_service import ensure_project
        from voip import Project
        c,key,parent,child=self.control()
        home_dir=self.root/'home';home_dir.mkdir()
        config=self.root/'server.toml'
        config.write_text(config.read_text()+'\n[directory_extensions]\nenabled=true\nroot='+
                          json.dumps(str(home_dir))+'\nrecipients=["alice"]\n')
        provider=Project(self.root);home=ensure_project(provider,str(home_dir))
        atomic_write(home.state/'operator-thread-id',self.team.thread_id())
        CallEvents(home).snapshot({})
        route=provider.state/'directory-status.json'
        atomic_write(route,json.dumps({'observed_at':time.time(),'plan':{'default':str(home_dir)},
            'targets':{home.session_id:{'session':home.session_id,'username':home.sip['username']}}}))
        from directory_service import policy
        cloud={'v':1,'policy_id':policy(provider)['id'],'home':home.session_id,'generation':'a'*64,
               'expires_at':int(time.time())+90,'routes':{},
               'targets':{home.session_id:{'session':home.session_id,'username':home.sip['username']}}}
        self.api.request.side_effect=lambda url,fields=None: {'data':cloud} if '/Documents/directory-routes' in url else parent
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('outgoing_calls.Calls.provider_busy',return_value=False):
            result=c.transfer(key,'default','go-home','Caller requested home')
            self.assertEqual(result['target_session'],home.session_id)
            self.assertEqual(result['requested_target'],'default')
            self.assertEqual(result['call_sid'],SID)
            child['to']=f"sip:{home.sip['username']}@{home.sip['domain']}"
            route.unlink()  # A later routing change must not reinterpret a submitted request.
            self.assertEqual(c.transfer(key,'default','go-home','Caller requested home')['observed_state'],'target_connected')
            self.assertEqual(c.transfer(key,home.session_id,'go-home','Caller requested home')['observed_state'],'target_connected')
            with self.assertRaisesRegex(SetupError,'different details'):
                c.transfer(key,'team','go-home','Caller requested home')
            self.assertEqual(sum(len(call.args)>1 for call in self.api.request.call_args_list),1)

    def generated_target(self):
        from directory_service import ensure_project,policy
        from voip import Project
        work=self.root/'work';work.mkdir()
        config=self.root/'server.toml'
        config.write_text(config.read_text()+'\n[directory_extensions]\nenabled=true\nroot='+
                          json.dumps(str(work))+'\nrecipients=["alice"]\n')
        provider=Project(self.root);target=ensure_project(provider,str(work/'project'))
        atomic_write(target.state/'operator-thread-id',self.team.thread_id())
        CallEvents(target).snapshot({})
        rule=policy(provider)
        cloud={'v':1,'policy_id':rule['id'],'home':rule['home'],'generation':'a'*64,
               'expires_at':int(time.time())+90,'routes':{},
               'targets':{target.session_id:{'session':target.session_id,'username':target.sip['username']}}}
        return target,cloud

    def test_warmed_generated_target_waits_for_cloud_publication_before_redirect(self):
        c,key,parent,child=self.control();target,cloud=self.generated_target()
        missing={**cloud,'targets':{}};responses=iter([missing,cloud]);order=[]
        def api(url,fields=None):
            if '/Documents/directory-routes' in url:
                value=next(responses);order.append('published' if value['targets'] else 'missing')
                return {'data':value}
            if fields is not None:order.append('redirect')
            return parent
        self.api.request.side_effect=api
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm',side_effect=lambda *a:order.append('warm')),\
             patch('outgoing_calls.Calls.provider_busy',return_value=False),patch('call_control.time.sleep'):
            result=c.transfer(key,target.session_id,'generated','Go to project')
        self.assertEqual(order,['warm','missing','published','redirect'])
        self.assertEqual(result['route_check']['reason'],'target_published')
        self.assertEqual(result['state'],'submitted')

    def test_missing_cloud_route_keeps_source_connected_and_releases_warmup(self):
        c,key,parent,child=self.control();target,cloud=self.generated_target()
        self.api.request.side_effect=lambda url,fields=None: {'data':{**cloud,'targets':{}}} if '/Documents/' in url else parent
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('voice_lifecycle.release') as release,patch('outgoing_calls.Calls.provider_busy',return_value=False),\
             patch.object(c,'wait_for_route',side_effect=lambda t,r,record:Control.wait_for_route(c,t,r,record,timeout=0)):
            with self.assertRaisesRegex(SetupError,'source call was not redirected'):
                c.transfer(key,target.session_id,'missing','Go to project')
        release.assert_called_once()
        self.assertTrue(CallEvents(self.project).load()['calls'][key]['connected'])
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))
        self.assertEqual(transfers(self.project)['missing']['state'],'rejected')
        self.assertEqual(transfers(self.project)['missing']['route_check']['reason'],'target_not_published')

    def test_cloud_route_requires_current_policy_expiry_and_exact_endpoint(self):
        c,key,parent,child=self.control();target,cloud=self.generated_target()
        invalid=[None,{**cloud,'expires_at':0},{**cloud,'expires_at':int(time.time())+1000},
                 {**cloud,'policy_id':'b'*64},{**cloud,'home':'dir_'+'b'*24},
                 {**cloud,'generation':'untrusted'},
                 {**cloud,'targets':{target.session_id:{'session':target.session_id,'username':'other'}}}]
        for value in invalid:
            with self.subTest(value=value):
                self.api.request.return_value={'data':value}
                with self.assertRaises(SetupError):c.wait_for_route(target,'alice',{},timeout=0)
        self.api.request.side_effect=SetupError('Directory unavailable')
        with self.assertRaisesRegex(SetupError,'source call was not redirected'):
            c.wait_for_route(target,'alice',{},timeout=0)
        self.api.request.assert_called()

    def test_source_change_while_waiting_for_directory_prevents_redirect(self):
        c,key,parent,child=self.control();target,cloud=self.generated_target()
        def api(url,fields=None):
            if '/Documents/directory-routes' in url:
                child['parent_call_sid']='CA'+'3'*32
                return {'data':cloud}
            return parent
        self.api.request.side_effect=api
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('voice_lifecycle.release') as release,patch('outgoing_calls.Calls.provider_busy',return_value=False):
            with self.assertRaises(SetupError):c.transfer(key,target.session_id,'changed','Go to project')
        release.assert_called_once()
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_target_occupied_during_publication_wait_keeps_original_call(self):
        c,key,parent,child=self.control();target,cloud=self.generated_target()
        def api(url,fields=None):
            if '/Documents/directory-routes' in url:
                events=CallEvents(target);state=events.load()
                state['calls']['other']={'connected':True,'recipient_id':'alice'}
                atomic_write(events.path,json.dumps(state))
                return {'data':cloud}
            return parent
        self.api.request.side_effect=api
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('voice_lifecycle.release') as release,patch('outgoing_calls.Calls.provider_busy',return_value=False):
            with self.assertRaisesRegex(SetupError,'became occupied'):
                c.transfer(key,target.session_id,'occupied','Go to project')
        release.assert_called_once()
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_target_voice_loss_after_warmup_prevents_redirect(self):
        c,key,parent,child=self.control()
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'standby'}),patch('voice_lifecycle.warm'),\
             patch('voice_lifecycle.release') as release,patch('outgoing_calls.Calls.provider_busy',return_value=False):
            with self.assertRaisesRegex(SetupError,'readiness changed'):
                c.transfer(key,'team','voice-lost','Go to team')
        release.assert_called_once()
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_generated_source_snapshot_is_bound_to_verified_call_and_current_policy(self):
        import base64,urllib.parse
        c,key,parent,child=self.control();source,cloud=self.generated_target()
        c=Control(source,self.api)
        state=CallEvents(self.project).load();atomic_write(CallEvents(source).path,json.dumps(state))
        child['to']=f"sip:{source.sip['username']}@{source.sip['domain']}"
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm'),\
             patch('outgoing_calls.Calls.provider_busy',return_value=False):
            result=c.transfer(key,'team','returnable','Go to team')
        update=next(call for call in self.api.request.call_args_list if len(call.args)>1)
        encoded=urllib.parse.parse_qs(urllib.parse.urlsplit(update.args[1]['Url']).query)['ticket'][0].split('.')[0]
        ticket=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        self.assertEqual(ticket['call_sid'],SID)
        self.assertEqual(ticket['source_session'],source.session_id)
        self.assertEqual(ticket['source_route'],{'session':source.session_id,'username':source.sip['username'],
                                                'policy_id':cloud['policy_id']})
        self.assertNotIn('source_route',result)

    def test_historical_default_transfer_is_not_reinterpreted_as_home(self):
        c,key,parent,child=self.control()
        record={'id':'old','key':'old-key','source_session':'default','target_session':'default',
                'local_call':key,'note':'Historical test','call_sid':SID,'state':'connected'}
        atomic_write(c.path,json.dumps({'old-key':record}))
        with patch('directory_service.transfer_target',side_effect=AssertionError('Do not re-resolve an old key')):
            result=c.transfer(key,'default','old-key','Historical test')
        self.assertEqual(result['target_session'],'default')
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_source_change_during_warmup_releases_demand_without_redirect(self):
        c,key,parent,child=self.control()
        def change(*args):child['parent_call_sid']='CA'+'3'*32
        with patch('endpoint_health.voice_health',return_value={'ok':True}),patch('endpoint_health.sip_health',return_value={'ok':True}),\
             patch('call_control.operator_status',return_value={'voice':'connected'}),patch('voice_lifecycle.warm',side_effect=change),\
             patch('voice_lifecycle.release') as release,patch('outgoing_calls.Calls.provider_busy',return_value=False):
            with self.assertRaises(SetupError):c.transfer(key,'team','move1','Context')
            release.assert_called_once()
        self.assertEqual(transfers(self.project)['move1']['state'],'rejected')
        self.assertFalse(any(len(call.args)>1 for call in self.api.request.call_args_list))

    def test_deployed_bundles_parse_without_external_local_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ('incoming','authorize','outgoing','pin-delivery','call-status','handoff'):
                path=Path(temp)/(name+'.js');path.write_bytes(function_source(ROOT,name))
                self.assertNotIn("require('./",path.read_text())
                subprocess.run(['node','--check',str(path)],check=True,capture_output=True)
