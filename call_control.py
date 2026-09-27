"""Explicit control of one authenticated local call, identified before any mutation."""
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import uuid
from operator_mailbox import save_json
from voip import Project, Rpc, SetupError, lock, operator_status
from call_events import CallEvents
from outgoing_calls import Calls


def transfers(project):
    path=project.provider_state/'call-transfers.json'
    return json.loads(path.read_text()) if path.exists() else {}


def transfer_record(project,identity):
    return next((r for r in transfers(project).values() if r['id']==identity and r['target_session']==project.session_id),None)


class Control:
    def __init__(self,project,api=None):
        self.project=project;self.calls=Calls(project,api=api);self.api=self.calls.api
        self.path=project.provider_state/'call-transfers.json'
        self.mutex=project.provider_state/'call-control.lock'

    def current(self,call_id):
        state=CallEvents(self.project).load();call=state['calls'].get(call_id)
        if not state.get('observer_online') or not 0<=time.time()-state['observed_at']<10:
            raise SetupError('Call observer is not current')
        if not call or not call.get('connected') or not call.get('recipient_id'):
            raise SetupError('Select an identified connected call from this session')
        from voice_config import resolve_recipient
        person=resolve_recipient(self.project,call['recipient_id'])
        endpoint=f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"
        expected=call.get('phone_call_sid')
        if not expected and call.get('outgoing_request_id'):
            expected=next((r.get('phone_call_sid') for r in self.calls.load().values()
                if r['id']==call['outgoing_request_id'] and r.get('session_id','default')==self.project.session_id),None)
        if not expected:raise SetupError('This legacy call lacks a bound phone identity; reconnect once before using transfer control')
        candidates=[]
        for child in self.api.items(self.calls.base+'/Calls.json?Status=in-progress&PageSize=100','calls'):
            if child.get('to','').split(';',1)[0]!=endpoint or child.get('parent_call_sid')!=expected:continue
            parent=self.api.request(self.calls.resource(child['parent_call_sid']))
            inbound=(parent.get('direction')=='inbound' and parent.get('from')==person['number'] and parent.get('to')==self.calls.settings['phone_number'])
            outbound=(parent.get('direction')=='outbound-api' and parent.get('to')==person['number'] and parent.get('from')==self.calls.settings['phone_number'])
            if parent.get('account_sid')==self.calls.deployment['account_sid'] and parent.get('status')=='in-progress' and (inbound or outbound):candidates.append(parent)
        if len(candidates)!=1:raise SetupError('Could not uniquely match this local caller to its active phone leg')
        latest=CallEvents(self.project).load()['calls'].get(call_id,{})
        if not latest.get('connected') or latest.get('recipient_id')!=person['id']:raise SetupError('The selected local call changed')
        return person,candidates[0]

    def status(self,key):
        with lock(self.mutex):return self._status(key)

    def _status(self,key):
        record=transfers(self.project).get(key)
        if not record or record['source_session']!=self.project.session_id:raise SetupError('Unknown transfer key for this session')
        parent=self.api.request(self.calls.resource(record['call_sid']))
        children=self.api.items(self.calls.base+'/Calls.json?'+urllib.parse.urlencode({'ParentCallSid':record['call_sid'],'PageSize':100}),'calls')
        active=[c.get('to','').split(';',1)[0] for c in children if c['status']=='in-progress']
        target=Project(self.project.provider_root,record['target_session'])
        dest=f"sip:{target.sip['username']}@{target.sip['domain']}"
        state='target_connected' if dest in active else 'phone_ended' if parent['status'] in {'completed','failed','busy','no-answer','canceled'} else 'pending_or_returned_to_source'
        if state in {'target_connected','phone_ended'}:
            latest=transfers(self.project)
            latest[key]['state']='connected' if state=='target_connected' else 'ended'
            save_json(self.path,latest);record=latest[key]
        return {**self.public(record),'observed_state':state}

    @staticmethod
    def public(record):return {k:v for k,v in record.items() if k not in {'ticket','from','to'}}

    def wait_for_route(self, target, recipient, record, timeout=None):
        """Do not release the caller while a generated route is being republished."""
        from directory_service import is_generated, policy, settings
        if not is_generated(target):return
        provider=Project(self.project.provider_root)
        admission=policy(provider)
        if not admission or recipient not in admission['recipients']:
            raise SetupError('Recipient cannot use generated directory routes')
        if timeout is None:timeout=min(60, max(20, settings(provider)['interval_seconds']*2+5))
        service=self.calls.deployment.get('pin_sync_service_sid','')
        if not re.fullmatch(r'IS[0-9a-fA-F]{32}',service):
            raise SetupError('Directory routing service is not configured')
        url='https://sync.twilio.com/v1/Services/'+service+'/Documents/directory-routes'
        deadline=time.monotonic()+timeout
        while True:
            check={'observed_at':time.time(),'reason':'directory_unavailable'}
            try:
                data=self.api.request(url).get('data')
                now=time.time()
                if (isinstance(data,dict) and data.get('v')==1 and data.get('policy_id')==admission['id']
                        and data.get('home')==admission['home']
                        and re.fullmatch(r'[0-9a-f]{64}',str(data.get('generation','')))
                        and type(data.get('expires_at')) is int and now<data['expires_at']<=now+240
                        and isinstance(data.get('routes'),dict) and isinstance(data.get('targets'),dict)):
                    route=data['targets'].get(target.session_id)
                    check.update(generation=data['generation'],expires_at=data['expires_at'],
                                 reason='target_not_published')
                    if route=={'session':target.session_id,'username':target.sip['username']}:
                        check['reason']='target_published'
                        record['route_check']=check
                        return
            except (SetupError, OSError, ValueError):
                pass  # Keep the live source call; do not expose provider response data.
            record['route_check']=check
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise SetupError('Target route is not published; source call was not redirected')
            time.sleep(min(1,remaining))

    def transfer(self,call_id,target_id,key,note):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',key):raise SetupError('Use a stable transfer request key')
        if not isinstance(note,str) or len(note)>2000:raise SetupError('Transfer context must be at most 2000 characters')
        # Serializes transfer/hangup admission across this server, never across backend work.
        with lock(self.mutex):
            saved=transfers(self.project)
            if key in saved:
                old=saved[key]
                targets={old['target_session'],old.get('requested_target',old['target_session'])}
                if (old['source_session'],old['local_call'],old['note'])!=(self.project.session_id,call_id,note) or target_id not in targets:raise SetupError('Transfer key already has different details')
                return self._status(key)  # Never repeat an ambiguous provider POST.
            from directory_service import transfer_target
            requested_target=target_id
            target_id=transfer_target(self.project,target_id)
            from twilio_source import function_source
            from voip import ROOT
            deployment=self.calls.deployment
            if deployment.get('functions',{}).get('handoff',{}).get('sha256')!=hashlib.sha256(function_source(ROOT,'handoff')).hexdigest() or deployment.get('build_sid')!=deployment.get('deployed_build_sid'):
                raise SetupError('Deploy the current protected transfer handler before transferring calls')
            person,parent=self.current(call_id)
            for old in saved.values():
                if old['call_sid']!=parent['sid']:continue
                if old['state'] in {'preparing','submitting','unknown'}:
                    raise SetupError('An unresolved transfer already owns this phone call; inspect its transfer-status first')
                if old['state']=='submitted' and old['source_session']==self.project.session_id and old['local_call']==call_id:
                    raise SetupError('The previous transfer is still leaving this session')
            target=Project(self.project.provider_root,target_id)
            if target.session_id==self.project.session_id:raise SetupError('Caller is already in this session')
            from voice_config import resolve_recipient
            resolve_recipient(target,person['id'])
            from endpoint_health import voice_health,sip_health
            status=operator_status(target);events=CallEvents(target).load()
            if not voice_health(target,status)['ok'] or not sip_health(target)['ok']:raise SetupError('Target session is not ready')
            if not events.get('observer_online') or time.time()-events.get('observed_at',0)>=10:raise SetupError('Target call observer is not current')
            if any(c.get('connected') for c in events['calls'].values()):raise SetupError('Target session already has a caller')
            target_calls=Calls(target,api=self.api)
            if target_calls.provider_busy() or any(r['state'] in {'submitting','unknown','active','cancelling'} and target_calls.conflicts(r) for r in target_calls.load().values()):raise SetupError('Target has another phone attempt')
            record={'id':str(uuid.uuid4()),'key':key,'source_session':self.project.session_id,'target_session':target_id,
                'requested_target':requested_target,
                'target_thread':target.thread_id(),'recipient_id':person['id'],'local_call':call_id,'call_sid':parent['sid'],
                'from':parent['from'],'to':parent['to'],'direction':parent['direction'],'created':int(time.time()),
                'state':'preparing','note':note}
            saved[key]=record;save_json(self.path,saved)
            from voice_lifecycle import warm,release,acquire
            lease='transfer-'+record['id']
            try:
                warm(target,lease)
                # Give directory publication its own bounded startup budget. The
                # first lease may have been mostly consumed by voice negotiation.
                acquire(target,lease)
                self.wait_for_route(target,person['id'],record)
                # Inspect the exact source again after voice and route startup.
                _,current=self.current(call_id)
                if current['sid']!=parent['sid']:raise SetupError('Phone call changed before transfer')
                events=CallEvents(target).load()
                if not events.get('observer_online') or not 0<=time.time()-events.get('observed_at',0)<10:
                    raise SetupError('Target call observer changed during preparation')
                if any(c.get('connected') for c in events['calls'].values()):raise SetupError('Target became occupied during preparation')
                if target_calls.provider_busy():raise SetupError('Target has another phone attempt')
                status=operator_status(target)
                if (status.get('voice')!='connected' or not voice_health(target,status)['ok']
                        or not sip_health(target)['ok']):
                    raise SetupError('Target readiness changed during preparation')
                ticket={k:record[k] for k in ('id','call_sid','recipient_id','source_session','target_session','from','to','direction')}
                # A return to the just-verified source must survive a transient
                # directory outage. This signed route is never used for targets.
                from directory_service import is_generated,policy
                if is_generated(self.project):
                    source_policy=policy(Project(self.project.provider_root))
                    if not source_policy or person['id'] not in source_policy['recipients']:
                        raise SetupError('Source directory permission changed')
                    ticket['source_route']={'session':self.project.session_id,
                        'username':self.project.sip['username'],'policy_id':source_policy['id']}
                ticket.update(purpose='handoff',expires=int(time.time())+120)
                encoded=base64.urlsafe_b64encode(json.dumps(ticket,separators=(',',':')).encode()).decode().rstrip('=')
                secret=bytes.fromhex((self.project.provider_state/'twilio-call-request-secret').read_text().strip())
                record['ticket']=encoded+'.'+hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest()
                record['state']='submitting';save_json(self.path,saved)
                try:
                    self.api.request(self.calls.resource(parent['sid']),{'Url':'https://'+self.calls.deployment['function_domain']+'/handoff?ticket='+record['ticket'],'Method':'POST'})
                except Exception as error:
                    rejected=isinstance(error,SetupError) and re.search(r'HTTP (400|401|403|404|405|422|429),',str(error))
                    record['state']='rejected' if rejected else 'unknown';save_json(self.path,saved)
                    if rejected:release(target,lease)
                    raise SetupError('Transfer '+record['state']+'; inspect transfer-status with the same key, do not retry under another key') from None
                record['state']='submitted';save_json(self.path,saved)
                # The source SIP reservation ends; the parent phone call continues elsewhere.
                with lock(self.calls.mutex,blocking=True):
                    outgoing=self.calls.load()
                    for r in outgoing.values():
                        if r.get('phone_call_sid')==parent['sid']:r['transferred']=True
                    self.calls.save(outgoing)
                return self.public(record)
            except BaseException:
                if record['state']=='preparing':
                    record['state']='rejected';save_json(self.path,saved);release(target,lease)
                raise

    def hangup(self,call_id):
        with lock(self.mutex):
            person,parent=self.current(call_id)
            result=self.api.request(self.calls.resource(parent['sid']),{'Status':'completed'})
            return {'call_id':call_id,'phone_call_sid':parent['sid'],'requested':True,'provider_status':result.get('status')}
