"""Durable PIN delivery, separate from operator conversation and mailbox.

The existing request daemon drives this queue. Hosted receipts are authoritative
for confirmation/opt-out; neither call completion nor answer means delivery.
"""
import hashlib
import json
import re
import secrets
import time
import urllib.parse
import uuid

from outgoing_calls import api_client, TERMINAL
from pin_security import master_key, pin_path, seal, verifier
from voice_config import phone_number, recipients, redial_policy, retry_delay
from voip import ROOT, SetupError, atomic_write, lock

DONE = {'confirmed', 'cancelled'}


class Deliveries:
    def __init__(self, project, api=None, clock=time.time):
        self.project, self._api, self.clock = project, api, clock
        self.path = project.shared_state / 'pin-deliveries.json'
        self.mutex = project.shared_state / 'pin-deliveries.lock'

    @property
    def api(self):
        if self._api is None:
            self._api = api_client()
        return self._api

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, records):
        atomic_write(self.path, json.dumps(records, indent=2) + '\n')
        # Atomic rename must survive a crash before the external dial.
        import os
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k not in {'sealed', 'auth_tag'}}

    def status(self, key=None):
        records = self.load()
        if key is not None and key not in records:
            raise SetupError('Unknown PIN delivery key')
        return self.public(records[key]) if key else [self.public(r) for r in records.values()]

    def generate(self, identity, delivery='print', key=None, delays=None, digits=8):
        if delivery not in {'print', 'call'} or type(digits) is not int or not 6 <= digits <= 12:
            raise SetupError('Choose print/call delivery and a PIN length of 6–12 digits')
        if delivery == 'call' and (not key or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', key)):
            raise SetupError('Call delivery requires a stable --request-key of 1–120 letters, digits, or ._:-')
        policy = redial_policy({'delays_seconds': delays or [60, 300, 480, 3600]})
        # Delivery retries deliberately have no attempt cap or expiry; only
        # confirmation, cancellation, or an ambiguous call submission stops them.
        policy.pop('max_attempts')
        with lock(self.mutex):
            people, _ = recipients(self.project, secrets=True, allow_missing=identity)
            if identity not in people:
                raise SetupError('Unknown recipient')
            person = people[identity]
            location = pin_path(self.project, identity)
            records = self.load()
            if key and key in records:
                raise SetupError('PIN delivery key already exists; inspect pin status instead of generating again')
            if any(r['recipient_id'] == identity and (r['state'] not in DONE or not r.get('settled')) for r in records.values()):
                raise SetupError('Cancel or complete the existing PIN delivery before rotating this recipient')
            secret = master_key(self.project, create='pin_auth' not in person)
            pin = ''.join(secrets.choice('0123456789') for _ in range(digits))
            auth = verifier(pin, secret)
            from pin_security import authentication
            if any(authentication(self.project, p)['tag'] == auth['tag'] for p in people.values()
                   if 'pin' in p or 'pin_auth' in p):
                raise SetupError('Generated PIN collides with an existing PIN; generate again')
            if delivery == 'call':
                delivery_id = str(uuid.uuid4())
                record = {'key':key, 'id':delivery_id, 'recipient_id':identity, 'to':person['number'],
                          'from':phone_number(self.project), 'auth_tag':auth['tag'], 'state':'queued',
                          'created_at':int(self.clock()), 'next_attempt_at':int(self.clock()),
                          'redial':policy, 'sealed':seal(pin, secret, delivery_id), 'attempts':[]}
                # Save the encrypted job before replacing authentication. If the
                # second write fails, the worker refuses its mismatched auth tag.
                records[key] = record
                self.save(records)
            atomic_write(location, json.dumps(auth) + '\n')
            return pin if delivery == 'print' else self.public(record)

    def deployment(self):
        try:
            data = json.loads((self.project.provider_state / 'twilio-provisioning.json').read_text())
            if not re.fullmatch(r'IS[0-9a-fA-F]{32}', data.get('pin_sync_service_sid', '')):
                raise ValueError()
            if not re.fullmatch(r'AC[0-9a-fA-F]{32}', data.get('account_sid', '')):
                raise ValueError()
            if not re.fullmatch(r'[a-z0-9-]+\.twil\.io', data.get('function_domain', '')):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise SetupError('Deploy PIN delivery with config apply first') from None

    def doc_url(self, record):
        return ('https://sync.twilio.com/v1/Services/' + self.deployment()['pin_sync_service_sid'] +
                '/Documents/pin-' + record['id'])

    def read_doc(self, record):
        try:
            return self.api.request(self.doc_url(record))
        except SetupError as error:
            if 'HTTP 404,' not in str(error) or record.get('published'):
                raise
            return None

    def update_doc(self, record, doc, data):
        return self.api.request(self.doc_url(record), {'Data':json.dumps(data)}, revision=doc['revision'])

    def initial_data(self, record):
        return {k:record[k] for k in ('id', 'recipient_id', 'from', 'to', 'auth_tag', 'sealed')} | {'state':'pending'}

    def receipt(self, record, records=None):
        doc = self.read_doc(record)
        if doc is None:
            if records is not None:
                record['publication_started'] = True
                self.save(records)
            # A stable cloud name makes creation safe to retry after a lost reply.
            self.api.request(self.doc_url(record).rsplit('/', 1)[0],
                             {'UniqueName':'pin-' + record['id'], 'Data':json.dumps(self.initial_data(record))})
            doc = self.read_doc(record)
        if not doc or any(doc['data'].get(k) != record[k] for k in ('id', 'recipient_id', 'from', 'to', 'auth_tag')):
            raise SetupError('PIN delivery receipt does not match this request')
        record['published'] = True
        return doc

    def call_url(self, sid=None):
        base = 'https://api.twilio.com/2010-04-01/Accounts/' + self.deployment()['account_sid'] + '/Calls'
        if sid is not None and not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid):
            raise SetupError('Invalid PIN delivery phone call ID')
        return base + ('/' + sid if sid else '') + '.json'

    def validate_call(self, record, call):
        if (call.get('account_sid') != self.deployment()['account_sid'] or
                call.get('from') != record['from'] or call.get('to') != record['to'] or
                call.get('direction') != 'outbound-api' or call.get('parent_call_sid')):
            raise SetupError('Phone call does not match this PIN delivery')
        self.call_url(call['sid'])

    def ready(self, record):
        from service_config import access_bytes
        from voip import Project
        from twilio_source import function_source
        deployed = self.deployment()
        if (deployed.get('access_asset', {}).get('sha256') != hashlib.sha256(access_bytes(Project(self.project.provider_root))).hexdigest() or
                deployed.get('functions', {}).get('pin-delivery', {}).get('sha256') != hashlib.sha256(function_source(ROOT, 'pin-delivery')).hexdigest() or
                deployed.get('build_sid') != deployed.get('deployed_build_sid')):
            raise SetupError('PIN or phone configuration changed; run config apply before delivery')
        person = recipients(self.project, secrets=True)[0].get(record['recipient_id'], {})
        if person.get('number') != record['to'] or person.get('pin_auth', {}).get('tag') != record['auth_tag']:
            raise SetupError('PIN delivery configuration changed; cancel this request')
        if phone_number(self.project) != record['from']:
            raise SetupError('Server phone number changed; cancel this request')

    def settle_call(self, record, doc):
        """Clean up a known in-flight attempt without ever dialing another."""
        if not record['attempts']:
            record['settled'] = True
            return
        attempt = record['attempts'][-1]
        sid = attempt.get('sid') or doc.get('data', {}).get('call_sid')
        if not sid:
            # An ambiguous create still requires reconcile; disabling its cloud
            # prompt guarantees any late webhook hangs up without reading the PIN.
            record['settled'] = attempt['state'] not in {'submitting', 'unknown'}
            return
        call = self.api.request(self.call_url(sid))
        self.validate_call(record, call)
        if call['status'] not in TERMINAL:
            call = self.api.request(self.call_url(sid), {'Status':'completed'})
            self.validate_call(record, call)
        attempt.update(sid=sid, phone_status=call['status'])
        record['settled'] = call['status'] in TERMINAL

    def cancel(self, key):
        with lock(self.mutex):
            records = self.load()
            if key not in records:
                raise SetupError('Unknown PIN delivery key')
            record = records[key]
            # Persist the local stop before network I/O; worker retries receipt
            # cleanup on recovery, never another dial.
            if record['state'] not in DONE:
                record['cancel_requested'] = True
                record['state'] = 'cancelling'
                self.save(records)
        self.process(key)
        return self.status(key)

    def process(self, key):
        with lock(self.mutex):
            records = self.load()
            record = records[key]
            if record['state'] in DONE and record.get('settled'):
                return self.public(record)
            try:
                # Before publication a cancellation can settle entirely locally.
                if record.get('cancel_requested') and not record.get('publication_started') and not record.get('published') and not record['attempts']:
                    doc = None
                elif record.get('cancel_requested') and not record.get('published') and not record['attempts']:
                    # There may have been a lost cloud-create reply; check before
                    # discarding the only reference to its encrypted prompt.
                    doc = self.read_doc(record)
                else:
                    doc = self.receipt(record, records)
                if record.get('cancel_requested') and doc and doc['data']['state'] == 'pending':
                    data = {k:v for k,v in doc['data'].items() if k != 'sealed'}
                    data.update(state='cancelled', settled_by='cli', settled_at=int(self.clock()))
                    doc = self.update_doc(record, doc, data)
                remote = doc['data']['state'] if doc else 'cancelled'
                if remote in DONE:
                    record.update(state=remote, settled_at=(doc['data'].get('settled_at') if doc else int(self.clock())))
                    record.pop('sealed', None)
                    record.pop('waiting_for', None)
                    self.save(records)  # Erase local encrypted prompt before cleanup I/O.
                    self.settle_call(record, doc or {})
                elif remote != 'pending':
                    raise SetupError('Unexpected PIN receipt state; delivery blocked')
                else:
                    self.advance(record, doc, records)
            except (SetupError, OSError, ValueError, KeyError) as error:
                record['waiting_for'] = str(error) if isinstance(error, SetupError) else type(error).__name__
            self.save(records)
            return self.public(record)

    def advance(self, record, doc, records):
        if record['attempts'] and record['attempts'][-1]['state'] in {'submitting','unknown','active'}:
            attempt = record['attempts'][-1]
            sid = attempt.get('sid') or (doc['data'].get('call_sid') if doc['data'].get('attempt') == attempt['id'] else None)
            if not sid:
                record['state'] = 'blocked'
                record['waiting_for'] = 'ambiguous_phone_submission; use pin reconcile KEY'
                return
            call = self.api.request(self.call_url(sid))
            self.validate_call(record, call)
            attempt.update(sid=sid, phone_status=call['status'], state='active')
            if call['status'] not in TERMINAL:
                record['state'] = 'active'
                return
            attempt.update(state='ended', finished_at=int(self.clock()))
            record.update(state='retry_wait', next_attempt_at=self.clock() + retry_delay(record['redial'], len(record['attempts'])))
            return  # Always observe the cloud receipt again before the next dial.
        if self.clock() < record['next_attempt_at']:
            return
        self.ready(record)
        # Don't add a delivery call while this recipient is already on the phone
        # through this service. Other recipients can proceed independently.
        for state in ('queued','ringing','in-progress'):
            for direction in ('To', 'From'):
                if self.api.items(self.call_url() + '?' + urllib.parse.urlencode({'Status':state,'PageSize':100,direction:record['to']}), 'calls'):
                    record['waiting_for'] = 'recipient_has_active_call'
                    return
        attempt = {'id':str(uuid.uuid4()), 'state':'submitting', 'created_at':int(self.clock())}
        # Reserve the hosted attempt before persisting a dial intent. CAS means
        # a recipient opt-out arriving here wins over this stale pending receipt.
        self.update_doc(record, doc, {**doc['data'], 'attempt':attempt['id'], 'call_sid':None})
        record['attempts'].append(attempt)
        record['state'] = 'submitting'
        record.pop('waiting_for', None)
        self.save(records)
        try:
            domain = self.deployment()['function_domain']
            call = self.api.request(self.call_url(), {'From':record['from'], 'To':record['to'],
                'Url':f"https://{domain}/pin-delivery?delivery={record['id']}&attempt={attempt['id']}",
                'Method':'POST', 'Timeout':'30', 'TimeLimit':'180', 'Record':'false'})
            self.validate_call(record, call)
            attempt.update(sid=call['sid'], state='active', phone_status=call['status'])
            record['state'] = 'active'
        except Exception as error:
            rejected = isinstance(error, SetupError) and re.search(r'HTTP (400|401|403|404|405|422|429),', str(error))
            if rejected:
                attempt.update(state='ended', phone_status='rejected', finished_at=int(self.clock()))
                record.update(state='retry_wait', waiting_for='provider_rejected_call',
                              next_attempt_at=self.clock() + retry_delay(record['redial'], len(record['attempts'])))
            else:
                attempt['state'] = 'unknown'
                record.update(state='blocked', waiting_for='ambiguous_phone_submission; use pin reconcile KEY')
        self.save(records)

    def reconcile(self, key, sid):
        """Explicitly bind a provider call to an ambiguous create; never blind retry."""
        with lock(self.mutex):
            records = self.load()
            if key not in records or not records[key]['attempts']:
                raise SetupError('No PIN delivery attempt to reconcile')
            record = records[key]
            attempt = record['attempts'][-1]
            if attempt['state'] not in {'unknown','submitting'} or attempt.get('sid'):
                raise SetupError('Only an ambiguous PIN delivery can be reconciled')
            call = self.api.request(self.call_url(sid))
            self.validate_call(record, call)
            # Operator must select the exact call using the provider logs/URL;
            # reject calls created before this attempt or after its short window.
            from email.utils import parsedate_to_datetime
            stamp = parsedate_to_datetime(call['date_created']).timestamp()
            if not attempt['created_at'] - 5 <= stamp <= attempt['created_at'] + 180:
                raise SetupError('Provider call creation time does not match this attempt')
            doc = self.receipt(record)
            if doc['data'].get('call_sid') not in (None, sid):
                raise SetupError('Hosted receipt belongs to a different phone call')
            attempt.update(sid=sid, state='active')
            if record['state'] not in DONE and not record.get('cancel_requested'):
                record['state'] = 'active'
            self.save(records)
        return self.process(key)

    def tick(self):
        for key, record in self.load().items():
            if record['state'] not in DONE or not record.get('settled'):
                self.process(key)
