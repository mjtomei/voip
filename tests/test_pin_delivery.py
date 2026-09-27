import copy
import json
from unittest.mock import patch
from service_fixture import ServiceFixture
from pin_delivery import Deliveries
from pin_security import migrate, master_key, unseal, verify
from voice_config import recipients
from voip import SetupError, atomic_write

class FakeAPI:
    def __init__(self):
        self.doc = None; self.calls = {}; self.dials = 0; self.lose_create = False; self.busy = False; self.offline = False
    def request(self, url, fields=None, revision=None):
        if self.offline: raise SetupError('Twilio request did not complete; inspect/reconcile before retrying')
        if '/Documents' in url:
            if fields is None:
                if self.doc is None: raise SetupError('Twilio request failed: HTTP 404, provider code 20404')
                return copy.deepcopy(self.doc)
            if revision is not None and revision != self.doc['revision']:
                raise SetupError('Twilio request failed: HTTP 412, provider code 54321')
            self.doc = {'data':json.loads(fields['Data']), 'revision':str(int(self.doc['revision'])+1) if self.doc else '1'}
            return copy.deepcopy(self.doc)
        if url.endswith('/Calls.json'):
            self.dials += 1
            sid = 'CA' + str(self.dials).zfill(32)
            self.calls[sid] = {'sid':sid,'account_sid':'AC'+'a'*32,'direction':'outbound-api','parent_call_sid':None,
                              'from':fields['From'],'to':fields['To'],'status':'ringing'}
            if self.lose_create: raise SetupError('Unknown call submission')
            return copy.deepcopy(self.calls[sid])
        sid = url.rsplit('/',1)[1][:-5]
        if fields: self.calls[sid]['status'] = fields['Status']
        return copy.deepcopy(self.calls[sid])
    def items(self, url, key):
        return [{'status':'in-progress'}] if self.busy else []
    def settle(self, state):
        self.doc['data'] = {k:v for k,v in self.doc['data'].items() if k != 'sealed'} | {'state':state,'settled_at':1234}
        self.doc['revision'] = str(int(self.doc['revision'])+1)

class DeliveryTests(ServiceFixture):
    def setUp(self):
        super().setUp(); migrate(self.project)
        atomic_write(self.project.shared_state/'twilio-provisioning.json', json.dumps({
            'pin_sync_service_sid':'IS'+'a'*32,'account_sid':'AC'+'a'*32,'function_domain':'example.twil.io'}))
        self.now=1000; self.api=FakeAPI(); self.delivery=Deliveries(self.project,self.api,lambda:self.now)
        self.delivery.ready=lambda r: None
    def queue(self):
        return self.delivery.generate('alice','call','example',[60,300,480,3600])
    def test_call_queue_never_persists_plaintext_or_puts_pin_in_public_status(self):
        result=self.queue(); record=self.delivery.load()['example']
        pin=unseal(record['sealed'],master_key(self.project),record['id'])
        self.assertNotIn(pin,self.delivery.path.read_text()); self.assertNotIn('sealed',json.dumps(result))
        self.assertFalse((self.project.state/'mailbox.json').exists())
        self.assertTrue(verify(pin,recipients(self.project,True)[0]['alice']['pin_auth'],master_key(self.project)))
        with self.assertRaises(SetupError): self.queue()
    def test_print_mode_has_no_recoverable_copy_and_no_job(self):
        pin=self.delivery.generate('alice')
        self.assertEqual(len(pin),8);self.assertFalse(self.delivery.load())
        self.assertNotIn(pin,(self.root/'state/pins/alice.json').read_text())
    def test_indefinite_retry_uses_each_delay_after_failure_and_repeats_last(self):
        self.queue()
        for delay in [60,300,480,3600,3600,3600]:
            self.delivery.process('example');r=self.delivery.load()['example']
            self.assertEqual(r['state'],'active')
            self.api.calls[r['attempts'][-1]['sid']]['status']='completed' # Answering alone is NOT confirmation.
            r=self.delivery.process('example')
            self.assertEqual(r['state'],'retry_wait');self.assertEqual(r['next_attempt_at'],self.now+delay)
            dials=self.api.dials
            self.now+=delay-1;self.delivery.process('example');self.assertEqual(dials,self.api.dials)
            self.now+=1
        self.assertEqual(self.api.dials,6)
    def test_remote_opt_out_survives_restart_and_erases_local_and_cloud_prompt(self):
        self.queue();self.delivery.process('example');self.api.settle('cancelled')
        self.now+=100000
        restarted=Deliveries(self.project,self.api,lambda:self.now)
        r=restarted.process('example')
        self.assertEqual(r['state'],'cancelled');self.assertTrue(r['settled'])
        self.assertNotIn('sealed',self.api.doc['data']);self.assertNotIn('sealed',restarted.load()['example'])
        restarted.tick();self.assertEqual(self.api.dials,1)
    def test_confirmation_and_cancel_race_never_overwrite_confirmed_receipt(self):
        self.queue();self.delivery.process('example');self.api.settle('confirmed')
        r=self.delivery.cancel('example')
        self.assertEqual(r['state'],'confirmed');self.assertEqual(self.api.doc['data']['state'],'confirmed')
        self.assertNotIn('sealed',self.delivery.load()['example'])
    def test_cli_cancel_persists_while_offline_then_settles_without_redial(self):
        self.queue();self.delivery.process('example');self.api.offline=True
        self.assertEqual(self.delivery.cancel('example')['state'],'cancelling')
        self.api.offline=False;self.now+=100000;self.delivery.process('example')
        self.assertEqual(self.delivery.status('example')['state'],'cancelled');self.assertEqual(self.api.dials,1)
        self.assertNotIn('sealed',self.api.doc['data'])
    def test_ambiguous_create_blocks_until_hosted_webhook_binds_exact_call(self):
        self.queue();self.api.lose_create=True
        self.assertEqual(self.delivery.process('example')['state'],'blocked')
        self.now+=10000;self.delivery.process('example');self.assertEqual(self.api.dials,1)
        sid=next(iter(self.api.calls));self.api.doc['data']['call_sid']=sid
        self.assertEqual(self.delivery.process('example')['state'],'active');self.assertEqual(self.api.dials,1)
    def test_lost_receipt_after_publication_never_recreates_pending_prompt(self):
        self.queue();self.delivery.process('example');self.api.doc=None
        r=self.delivery.process('example')
        self.assertIn('waiting_for',r);self.assertIsNone(self.api.doc);self.assertEqual(self.api.dials,1)
    def test_mismatched_cloud_receipt_blocks_and_does_not_cancel_other_call(self):
        self.queue();self.delivery.process('example');self.api.doc['data']['to']='+12025550999'
        r=self.delivery.process('example');self.assertIn('does not match',r['waiting_for'])
        self.assertEqual(self.api.dials,1)
    def test_config_not_deployed_waits_without_call_and_recipient_busy_waits(self):
        self.queue()
        self.delivery.ready=lambda r: (_ for _ in ()).throw(SetupError('run config apply'))
        self.assertIn('config apply',self.delivery.process('example')['waiting_for']);self.assertEqual(self.api.dials,0)
        self.delivery.ready=lambda r: None;self.api.busy=True
        self.assertEqual(self.delivery.process('example')['waiting_for'],'recipient_has_active_call');self.assertEqual(self.api.dials,0)
    def test_cancel_unpublished_job_needs_no_network_or_deployment(self):
        self.queue();(self.project.shared_state/'twilio-provisioning.json').unlink();self.api.offline=True
        r=self.delivery.cancel('example')
        self.assertEqual(r['state'],'cancelled');self.assertTrue(r['settled']);self.assertEqual(self.api.dials,0)
        self.assertNotIn('sealed',self.delivery.load()['example'])
    def test_cancel_after_lost_document_create_cleans_cloud_without_dial(self):
        self.queue();r=self.delivery.load();r['example']['publication_started']=True;self.delivery.save(r)
        self.api.doc={'revision':'1','data':self.delivery.initial_data(r['example'])}
        result=self.delivery.cancel('example')
        self.assertEqual(result['state'],'cancelled');self.assertNotIn('sealed',self.api.doc['data']);self.assertEqual(self.api.dials,0)
    def test_stale_revision_cannot_overwrite_recipient_opt_out_before_dial(self):
        self.queue();original=self.delivery.update_doc
        def racing(record,doc,data):
            self.api.settle('cancelled')
            return original(record,doc,data)
        self.delivery.update_doc=racing
        result=self.delivery.process('example')
        self.assertEqual(self.api.dials,0)
        self.assertIn('HTTP 412',result['waiting_for'])
        self.delivery.update_doc=original
        self.assertEqual(self.delivery.process('example')['state'],'cancelled')
        self.assertEqual(self.api.dials,0)
    def test_new_recipient_pin_file_is_created_by_generator(self):
        path=self.root/'state/pins/alice.json';path.unlink()
        pin=self.delivery.generate('alice')
        self.assertTrue(path.is_file());self.assertNotIn(pin,path.read_text())
        self.assertTrue(verify(pin,recipients(self.project,True)[0]['alice']['pin_auth'],master_key(self.project)))
    def test_known_rejected_create_retries_but_ambiguous_network_error_does_not(self):
        self.queue();original=self.api.request
        def rejected(url,fields=None,revision=None):
            if url.endswith('/Calls.json') and fields is not None:
                raise SetupError('Twilio request failed: HTTP 429, provider code 20429')
            return original(url,fields,revision)
        self.api.request=rejected
        r=self.delivery.process('example')
        self.assertEqual(r['state'],'retry_wait');self.assertEqual(r['next_attempt_at'],self.now+60)
        self.now+=60;self.api.request=original
        self.assertEqual(self.delivery.process('example')['state'],'active');self.assertEqual(self.api.dials,1)
