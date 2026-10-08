"""Authenticated callback SIP identity through the actual connection notice."""
import copy
import json
import time

from service_fixture import ServiceFixture
from voip import atomic_write
from call_events import CallEvents, call_key, callback_identity
from call_preparation import fingerprint
from operator_mailbox import Mailbox

ORIGINAL = 'CA' + '1' * 32
CALLBACK = 'CA' + '2' * 32


class CallbackAgendaTest(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.events = CallEvents(self.project)
        self.box = Mailbox(self.project, 'alice')
        message = self.box.post('worker', 'Review', 'Discuss silver compass.', 'agenda')
        self.record = {'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'session_id': 'default', 'recipient_id': 'alice', 'phone_call_sid': ORIGINAL,
            'created': int(time.time()) - 1800, 'reason': 'Review', 'state': 'ended',
            'preparation': {'state': 'ready', 'reason': 'Review',
                'thread_id': self.project.thread_id(), 'recipient_id': 'alice',
                'recipient_number': '+12025550101', 'prepared_at': time.time() - 1810,
                'messages': {'agenda': fingerprint(message)},
                'brief': {'ready': True, 'opening': 'The reference is silver compass. Shall we review it?',
                          'context': 'The earlier review is ready; a decision remains pending.'}}}
        self.save()

    def save(self, extra=None):
        atomic_write(self.project.shared_state / 'outgoing-calls.json',
                     json.dumps({'original': self.record, **(extra or {})}))

    def connect(self, identity='callback-one', person='alice', original=ORIGINAL, sid=CALLBACK):
        peer = 'sip:voip-in-' + person + '-' + sid + ('-cb-' + original if original else '') + '@test.invalid'
        self.events.event({'event': True, 'class': 'call', 'type': 'CALL_ESTABLISHED',
            'id': identity, 'peeruri': peer,
            'accountaor': f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"})
        return next(e for e in self.events.load()['events'] if e['id'] == call_key(identity) + ':connected')

    def test_first_callback_carries_older_pending_agenda_without_history_poll_or_lookup(self):
        event = self.connect()
        self.assertEqual(event['phone_call_sid'], CALLBACK)
        self.assertEqual(event['callback_of'], ORIGINAL)
        text = self.events.message(event)
        self.assertIn('first connected callback', text)
        self.assertIn('historical snapshot', text)
        self.assertIn('silver compass', text)
        self.assertIn('Begin speaking directly to the caller NOW', text)
        self.assertIn('Do not delegate or run tools before giving this opening', text)
        self.assertNotIn('new incoming call', text)
        self.assertNotIn('mailbox --recipient alice list', text)
        self.assertEqual(self.box.read('agenda')['status'], 'unaddressed')
        self.assertFalse((self.project.provider_state / 'call-history.json').exists())

    def test_first_connection_claim_survives_restart_and_blocks_concurrent_or_later_replays(self):
        first = self.connect()
        second = self.connect('callback-two', sid='CA'+'3'*32)
        self.assertIsNotNone(self.events.prepared_opening(first))
        self.assertIsNone(self.events.prepared_opening(second))
        self.events.delivery(first['id'], self.project.thread_id(), 'submitted', 'voice-one')
        restored = CallEvents(self.project)
        event = next(e for e in restored.load()['events'] if e['id'] == first['id'])
        text = restored.message(event)
        self.assertIn('do not repeat', text)
        self.assertNotIn('silver compass', text)
        restored.snapshot({})
        third = self.connect('callback-three', sid='CA'+'4'*32)
        self.assertIsNone(restored.prepared_opening(third))

    def test_monitor_snapshot_recovers_callback_identity(self):
        key = call_key('restored')
        self.events.snapshot({key: True}, recipients={key: 'alice'},
                             phone_sids={key: CALLBACK}, callbacks={key: ORIGINAL})
        event = self.events.pending(self.project.thread_id())
        self.assertIn('silver compass', self.events.message(event))
        peer = 'sip:voip-in-alice-' + CALLBACK + '-cb-' + ORIGINAL + '@test.invalid'
        self.assertEqual(callback_identity(peer), ORIGINAL)
        self.assertEqual(self.events.phone_sid(peer), CALLBACK)
        self.assertEqual(self.events.recipient(peer), 'alice')
        self.assertIsNone(callback_identity(peer.replace(ORIGINAL, 'not-a-call')))

    def test_explicit_route_does_not_inherit_or_consume_callback_agenda(self):
        explicit = self.connect('extension', original=None)
        self.assertNotIn('silver compass', self.events.message(explicit))
        self.assertIn('silver compass', self.events.message(self.connect()))

    def test_newer_attempt_cannot_replace_agenda_bound_to_this_callback(self):
        newer = copy.deepcopy(self.record)
        newer.update(id='bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee', phone_call_sid='CA'+'3'*32,
                     created=int(time.time()))
        newer['preparation']['brief']['opening'] = 'Different agenda.'
        self.save({'newer': newer})
        text = self.events.message(self.connect())
        self.assertIn('silver compass', text)
        self.assertNotIn('Different agenda', text)
        retry = self.connect('retry', original=newer['phone_call_sid'], sid='CA'+'4'*32)
        self.assertIn('Different agenda', self.events.message(retry))

    def test_invalid_scope_changed_recipient_reset_or_unknown_attempt_never_replays(self):
        event = self.connect()
        original = copy.deepcopy(self.record)
        for change in [{'recipient_id': 'bob'}, {'session_id': 'team'}, {'phone_call_sid': 'CA'+'9'*32}]:
            with self.subTest(change=change):
                self.record = {**copy.deepcopy(original), **change}; self.save()
                self.assertIsNone(self.events.prepared_opening(event))
        self.record = original
        for field, value in [('recipient_id', 'bob'), ('recipient_number', '+12025550999'),
                             ('thread_id', '99999999-1111-1111-1111-111111111111')]:
            with self.subTest(field=field):
                saved = self.record['preparation'][field]
                self.record['preparation'][field] = value; self.save()
                self.assertIsNone(self.events.prepared_opening(event))
                self.record['preparation'][field] = saved
        self.save()
        self.assertIsNone(self.events.prepared_opening({**event, 'recipient_id': 'bob'}))

    def test_changed_or_addressed_mailbox_agenda_is_not_repeated(self):
        event = self.connect()
        self.box.address('agenda', 'Already discussed on another call')
        self.assertNotIn('silver compass', self.events.message(event))

    def test_callback_window_and_preparation_timestamp_are_checked(self):
        event = self.connect()
        for age, eligible in [(10799, True), (10800, False), (-1, False)]:
            with self.subTest(age=age):
                self.record['placed_at'] = event['occurred_at'] - age; self.save()
                self.assertEqual(self.events.prepared_opening(event) is not None, eligible)
        self.record['placed_at'] = event['occurred_at'] - 60
        self.record['preparation']['prepared_at'] = event['occurred_at'] + 1; self.save()
        self.assertIsNone(self.events.prepared_opening(event))
