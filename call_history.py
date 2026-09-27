"""Provider call journal: recent callback routes and attributed missed-call notices."""
import json
import re
import time
from pathlib import Path
from operator_mailbox import save_json
from voip import Project, SetupError, lock

TERMINAL = {'completed','busy','failed','no-answer','canceled'}

class History:
    def __init__(self, project, api=None):
        self.project=Project(project.provider_root)
        self.path=self.project.state/'call-history.json'
        path=self.project.state/'twilio-provisioning.json'
        self.deployment=json.loads(path.read_text()) if path.exists() else {}
        self._api=api
        self.base='https://sync.twilio.com/v1/Services/'+self.deployment.get('pin_sync_service_sid','')+'/Documents'

    @property
    def api(self):
        if self._api is None:
            from outgoing_calls import api_client
            self._api=api_client()
        return self._api

    def enabled(self):
        return 'call-status' in self.deployment.get('functions',{}) and bool(self.deployment.get('pin_sync_service_sid'))

    def document(self,recipient):
        from voice_config import name
        name(recipient)
        try:return self.api.request(self.base+'/call-history-'+recipient)
        except SetupError as error:
            if 'HTTP 404,' in str(error):return {'data':{'v':1,'calls':{}},'revision':None}
            raise

    def merge(self,recipient,sid,patch):
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid):raise SetupError('Invalid call identity')
        for _ in range(4):
            doc=self.document(recipient);data=doc['data'];old=data['calls'].get(sid,{})
            now=int(time.time());record={**old,**patch,'call_sid':sid,'recipient_id':recipient,'updated_at':now,
                'authenticated':bool(old.get('authenticated') or patch.get('authenticated')),
                'connected':bool(old.get('connected') or patch.get('connected'))}
            if old.get('ended_at'):record.update(ended_at=old['ended_at'],status=old['status'])
            if old.get('callback_consumed_at'):
                record.update(callback_consumed_at=old['callback_consumed_at'],callback_call_sid=old.get('callback_call_sid'))
            data['calls'][sid]=record
            values=sorted(data['calls'].items(),key=lambda x:(x[1]['created_at'],x[0]),reverse=True)
            data['calls']={k:v for k,v in values if v['created_at']>=now-7*86400}
            data['calls']=dict(list(data['calls'].items())[:100])
            fields={'Data':json.dumps(data),'Ttl':'691200'}
            try:
                if doc.get('revision'):self.api.request(self.base+'/call-history-'+recipient,fields,revision=doc['revision'])
                else:self.api.request(self.base,{**fields,'UniqueName':'call-history-'+recipient})
                return
            except SetupError as error:
                if not any('HTTP '+code+',' in str(error) for code in ['409','412']):raise
        raise SetupError('Call history changed concurrently; retry its journal update')

    def outgoing(self,record):
        if not self.enabled() or not record.get('phone_call_sid'):return
        status=record.get('phone_status','queued')
        patch={'created_at':record['created'],'placed_at':record.get('placed_at',record['created']),'direction':'outgoing','accepted':True,
               'session_id':record['session_id'],'status':status,'connected':bool(record.get('connected'))}
        if status in TERMINAL:patch['ended_at']=record.get('updated',int(time.time()))
        signature=json.dumps(patch,sort_keys=True)
        if record.get('history_signature')==signature:return
        self.merge(record['recipient_id'],record['phone_call_sid'],patch)
        record['history_signature']=signature

    def consume_callback(self, record):
        original_sid = record.get('callback_of')
        if (record.get('direction') != 'incoming' or not record.get('authenticated') or
                not record.get('connected') or not re.fullmatch(r'CA[0-9a-fA-F]{32}', original_sid or '')):
            return None
        person = record['recipient_id']
        original = self.document(person)['data']['calls'].get(original_sid, {})
        if (original.get('direction') != 'outgoing' or not original.get('accepted') or
                original.get('recipient_id') != person or original.get('session_id') != record.get('session_id')):
            return None
        if original.get('callback_consumed_at'):
            return {'callback_consumed_at':original['callback_consumed_at'],'callback_call_sid':original.get('callback_call_sid')}
        consumed = {'callback_consumed_at':int(time.time()), 'callback_call_sid':record['call_sid']}
        self.merge(person, original_sid, consumed)
        return consumed

    def poll(self):
        if not self.enabled():return
        from voice_config import recipients
        from call_events import CallEvents,call_key
        fetched={}
        for person in recipients(self.project)[0]:
            doc=self.document(person)
            for sid,record in doc['data'].get('calls',{}).items():
                if record.get('recipient_id')!=person or not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid):continue
                fetched[sid]=record
                try:p=Project(self.project.root,record['session_id'])
                except (SetupError,OSError):continue
                events=CallEvents(p);key=call_key(sid);identity=key+':missed'
                # Redirecting a parent call can abandon the original Dial action.
                # A locally established SIP leg is independent connection evidence.
                observed=events.load()
                if not record.get('connected') and any(c.get('phone_call_sid')==sid for c in observed['calls'].values()):
                    self.merge(person,sid,{'connected':True,'authenticated':True})
                    record.update(connected=True,authenticated=True)
                if record.get('connected'):
                    consumed = self.consume_callback(record)
                    if consumed:
                        # Update both views regardless of the document's iteration order.
                        doc['data']['calls'][record['callback_of']].update(consumed)
                        if record['callback_of'] in fetched:fetched[record['callback_of']].update(consumed)
                    with lock(events.mutex,blocking=True):
                        state=events.load()
                        for event in state['events']:
                            if event['id']==identity:
                                event['history']=record
                                event['deliveries'].setdefault(p.thread_id(),{})['state']='superseded'
                                save_json(events.path,state)
                    continue
                if not record.get('ended_at'):continue
                # Status callbacks may arrive out of order. Give the Dial result time to arrive.
                if time.time()-record['ended_at']<30:continue
                with lock(events.mutex,blocking=True):
                    state=events.load()
                    if any(e['id']==identity for e in state['events']):continue
                    state['calls'].setdefault(key,{'connected':False,'seen_at':record['ended_at']})
                    state['events'].append({'id':identity,'call':key,'kind':'missed','occurred_at':record['ended_at'],
                        'recipient_id':person,'recovered':False,'outgoing_request_id':None,'deliveries':{},
                        'history':record})
                    save_json(events.path,state)
        with lock(self.project.state/'call-history.lock',blocking=True):save_json(self.path,{'observed_at':time.time(),'calls':fetched})

    def list(self,session,recipient=None):
        state=json.loads(self.path.read_text()) if self.path.exists() else {'calls':{}}
        rows=[v for v in state['calls'].values() if v['session_id']==session and (not recipient or v['recipient_id']==recipient)]
        return sorted(rows,key=lambda r:(r['created_at'],r['call_sid']),reverse=True)
