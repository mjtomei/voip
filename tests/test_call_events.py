import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from call_events import CallEvents, call_key, deliver, parse_calls, parse_request_ids, read_frame
from call_preparation import fingerprint
from operator_mailbox import Mailbox
from voip import Project, ROOT, SetupError, atomic_write
from service_fixture import single_recipient


class CallEventsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "config.toml").write_text((ROOT / "config.example.toml").read_text())
        single_recipient(root)
        self.project = Project(root)
        atomic_write(self.project.state / "operator-thread-id", "11111111-1111-1111-1111-111111111111")
        self.events = CallEvents(self.project)
        self.box = Mailbox(self.project)
        self.thread = self.project.thread_id()

    def event(self, kind="CALL_ESTABLISHED", identity="call-one", **overrides):
        value = {"event": True, "class": "call", "type": kind, "id": identity,
                 "accountaor": "sip:operator@YOUR-DOMAIN.sip.twilio.com",
                 'peeruri':'sip:voip-in-alice@twilio.example', **overrides}
        self.events.event(value)

    def test_empty_connection_notice_never_mentions_mailbox(self):
        self.event()
        text = self.events.message(self.events.pending(self.thread))
        self.assertIn("caller is connected", text)
        self.assertNotIn("mailbox", text.lower())
        self.assertNotIn('skipped', text)

    def test_configured_incoming_greeting_has_no_competing_hardcoded_opening(self):
        self.project.profile['prompts']['connection']='{{default}} Greet the caller with hello from the garden.'
        self.event()
        text=self.events.message(self.events.pending(self.thread))
        self.assertIn('hello from the garden',text)
        self.assertIn('Begin speaking to the caller NOW',text)
        self.assertIn('Follow the configured connection prompt',text)
        self.assertNotIn('How can I help?',text)
        self.assertIn('Never ask the caller to say or provide a PIN',text)
        self.project.profile['prompts']['connection']='{{default}}'
        self.assertIn('How can I help?',self.events.message(self.events.pending(self.thread)))

    def test_ended_review_points_to_only_that_calls_native_threads(self):
        first = self.alice_connects('first-call')
        old = '22222222-2222-2222-2222-222222222222'
        self.events.delivery(first['id'], old, 'submitted')
        self.events.delivery(first['id'], self.thread, 'submitted')
        self.event('CALL_CLOSED', identity='first-call')
        second = self.alice_connects('second-call')
        other = '33333333-3333-3333-3333-333333333333'
        self.events.delivery(second['id'], other, 'submitted')
        ended = next(e for e in self.events.load()['events']
                     if e['call'] == call_key('first-call') and e['kind'] == 'ended')
        text = self.events.message(ended)
        self.assertIn(old, text)
        self.assertIn(self.thread, text)
        self.assertNotIn(other, text)
        self.assertIn(str(first['occurred_at']), text)
        self.assertNotIn(str(second['occurred_at']), text)
        self.assertIn("caller's own words", text)
        self.assertIn('not audible timestamps', text)
        self.assertIn('leave the message pending', text)

    def skipped(self, key='missed-one', recipient='alice', session='default'):
        path = self.project.shared_state / 'schedule-state.json'
        state = json.loads(path.read_text()) if path.exists() else {'last_minute': None, 'occurrences': {}}
        state['occurrences'][key] = {'state': 'skipped', 'outcome': 'scheduler_offline',
            'scheduled_at': time.time() - 3600,
            'spec': {'session': session, 'recipient': recipient}}
        atomic_write(path, json.dumps(state))

    def alice_connects(self, identity='call-one'):
        self.event(identity=identity, peeruri='sip:voip-in-alice@twilio.example')
        return next(e for e in self.events.load()['events'] if e['id'] == call_key(identity) + ':connected')

    def test_connection_reports_only_matching_session_recipient_skips(self):
        self.skipped()
        self.skipped('other-person', recipient='bob')
        self.skipped('other-session', session='team')
        text = self.events.message(self.alice_connects())
        self.assertIn('1 scheduled call was skipped', text)
        self.assertNotIn('mailbox', text)
        self.assertIn('not a request to place catch-up calls', text)

    def test_unknown_recipient_does_not_consume_another_persons_count(self):
        self.skipped()
        self.event(peeruri='sip:unknown@twilio.example')
        event = self.events.pending(self.thread)
        self.assertNotIn('skipped', self.events.message(event))
        self.events.delivery(event['id'], self.thread, 'submitted')
        self.event('CALL_CLOSED')
        self.assertIn('1 scheduled call was skipped', self.events.message(self.alice_connects('next')))

    def test_count_snapshot_retains_skips_added_while_notice_is_in_flight(self):
        self.skipped('one'); self.skipped('two')
        event = self.alice_connects()
        self.assertIn('2 scheduled calls were skipped', self.events.message(event))
        self.skipped('three')
        self.assertIn('2 scheduled calls were skipped', self.events.message(event))
        self.events.delivery(event['id'], self.thread, 'submitted')
        self.event('CALL_CLOSED')
        self.assertIn('1 scheduled call was skipped', self.events.message(self.alice_connects('next')))

    def test_failed_notice_does_not_clear_count_on_next_call(self):
        self.skipped('one'); self.skipped('two')
        event = self.alice_connects()
        self.events.message(event)
        self.events.delivery(event['id'], self.thread, 'submitting')
        self.event('CALL_CLOSED')
        self.assertIn('2 scheduled calls were skipped', self.events.message(self.alice_connects('next')))

    def test_reconnect_and_fresh_thread_do_not_repeat_accepted_count(self):
        self.skipped()
        event = self.alice_connects()
        self.events.message(event)
        self.events.delivery(event['id'], self.thread, 'submitted', 'voice-one')
        retry = self.events.pending(self.thread, 'voice-two')
        self.assertNotIn('skipped', self.events.message(retry))
        self.events.delivery(event['id'], self.thread, 'submitting', 'voice-two')
        self.assertNotIn('skipped', self.events.message(self.events.pending(self.thread, 'voice-three')))
        new_thread = '22222222-2222-2222-2222-222222222222'
        atomic_write(self.project.state / 'operator-thread-id', new_thread)
        self.assertNotIn('skipped', self.events.message(self.events.pending(new_thread, 'voice-four')))
        self.event('CALL_CLOSED')
        self.assertNotIn('skipped', self.events.message(self.alice_connects('next')))

    def test_prepared_opening_precedes_skip_count(self):
        request_id = self.prepared_call()
        path = self.project.shared_state / 'outgoing-calls.json'
        records = json.loads(path.read_text()); records['request']['recipient_id'] = 'alice'
        atomic_write(path, json.dumps(records))
        self.skipped()
        self.event(peeruri=f'sip:voip-{request_id}@twilio.example')
        text = self.events.message(self.events.pending(self.thread))
        self.assertLess(text.index('The build passed. Shall we deploy?'), text.index('scheduled call was skipped'))
        self.assertNotIn('mailbox list', text)

    async def test_skip_count_acknowledgement_happens_only_after_rpc_acceptance(self):
        self.skipped()
        event = self.alice_connects()
        stop = asyncio.Event()
        outer = self
        class Rpc:
            async def request(self, method, params):
                outer.assertIn('1 scheduled call was skipped', params['text'])
                saved = outer.events.load()['events'][0]
                outer.assertNotIn('missed_schedule_reported_at', saved)
                outer.assertEqual(saved['missed_schedule_keys'], ['missed-one'])
                stop.set()
                return {}
        await deliver(self.project, Rpc(), stop)
        saved = self.events.load()['events'][0]
        self.assertIn('missed_schedule_reported_at', saved)
        self.event('CALL_CLOSED')
        self.assertNotIn('skipped', self.events.message(self.alice_connects('next')))

    def test_connection_counts_unaddressed_even_if_read_and_omits_after_addressing(self):
        self.box.post("worker", "Review", "Body", "one")
        self.box.read("one")
        self.event()
        event = self.events.pending(self.thread)
        self.assertIn("1 unaddressed mailbox message.", self.events.message(event))
        self.box.post("other", "Question", "Body", "two")
        self.assertIn("2 unaddressed mailbox messages.", self.events.message(event))
        for key in ("one", "two"):
            self.box.address(key)
        self.assertNotIn("mailbox", self.events.message(event))

    def test_ringing_unknown_accounts_and_duplicate_events_do_not_notify(self):
        self.event("CALL_INCOMING")
        self.event(accountaor="sip:stranger@other.example")
        self.assertIsNone(self.events.pending(self.thread))
        self.event()
        self.event()
        self.assertEqual(len(self.events.load()["events"]), 1)

    def test_old_connected_notice_is_suppressed_after_hangup_and_cannot_reopen(self):
        self.event()
        self.event("CALL_CLOSED")
        self.event()  # delayed/duplicate established
        event = self.events.pending(self.thread)
        self.assertEqual(event["kind"], "ended")
        self.assertIn("call has ended", self.events.message(event))
        self.events.delivery(event["id"], self.thread, "submitted")
        self.assertIsNone(self.events.pending(self.thread))
        self.assertFalse(self.events.load()["calls"][call_key("call-one")]["connected"])

    def test_restart_reconciliation_never_calls_transport_loss_a_hangup(self):
        self.event()
        self.events.offline()
        self.assertIsNone(self.events.pending(self.thread))
        self.assertTrue(self.events.load()["calls"][call_key("call-one")]["connected"])
        restored = CallEvents(self.project)
        restored.snapshot({})
        ended = restored.pending(self.thread)
        self.assertEqual(ended["kind"], "ended")
        self.assertIn("exact end time is unknown", restored.message(ended))

    def test_snapshot_recovers_an_existing_call_and_preserves_preanswer_calls(self):
        calls = parse_calls("\nUser-Agent: operator@example.com\n--- Active calls (2) ---\n"
                            "> [line 1, id one]  00:14  ESTABLISHED    sip:caller@example.com\n"
                            "  [line 2, id two]  00:00  INCOMING    sip:other@example.com\n")
        self.events.snapshot(calls)
        self.assertEqual(len(self.events.load()["events"]), 1)
        self.assertEqual(self.events.pending(self.thread)["call"], call_key("one"))
        for invalid in ("", "--- Active calls (1) ---\nno parseable call"):
            with self.assertRaises(SetupError):
                parse_calls(invalid)

    def test_reconnect_reasserts_current_call_but_never_replays_ended_connection(self):
        self.event()
        event = self.events.pending(self.thread, "voice-one")
        self.events.delivery(event["id"], self.thread, "submitted", "voice-one")
        self.assertIsNone(self.events.pending(self.thread, "voice-one"))
        self.assertEqual(self.events.pending(self.thread, "voice-two")["id"], event["id"])
        self.event("CALL_CLOSED")
        self.assertEqual(self.events.pending(self.thread, "voice-two")["kind"], "ended")

    def test_expired_observation_withholds_connection_notice(self):
        self.event()
        state = self.events.load()
        state["observed_at"] = time.time() - 20
        atomic_write(self.events.path, json.dumps(state))
        self.assertIsNone(self.events.pending(self.thread))

    def prepared_call(self):
        message = self.box.post('worker', 'Review', 'The build passed; decide on deployment.', 'agenda')
        request_id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        prepared = {'state': 'ready', 'reason': 'Review', 'thread_id': self.thread,
                    'messages': {'agenda': fingerprint(message)},
                    'brief': {'ready': True, 'opening': 'The build passed. Shall we deploy?', 'context': 'Decision pending.'}}
        record = {'id': request_id, 'created': int(time.time()), 'reason': 'Review', 'preparation': prepared}
        atomic_write(self.project.state / 'outgoing-calls.json', json.dumps({'request': record}))
        return request_id

    def test_matched_outgoing_call_has_prepared_opening_without_mailbox_lookup(self):
        request_id = self.prepared_call()
        self.event(peeruri=f'sip:voip-{request_id}@twilio.example')
        event = self.events.pending(self.thread)
        text = self.events.message(event)
        self.assertIn('The build passed. Shall we deploy?', text)
        self.assertIn('Begin speaking directly to the caller NOW', text)
        self.assertNotIn('mailbox list', text)
        self.assertEqual(self.box.count(), 1)
        self.events.delivery(event['id'], self.thread, 'submitted', 'old-voice')
        text = self.events.message(self.events.pending(self.thread, 'new-voice'))
        self.assertIn('do not repeat', text)
        self.assertNotIn('Begin speaking', text)

    def test_incoming_or_unmatched_call_cannot_receive_outgoing_agenda(self):
        self.prepared_call()
        self.event(peeruri='sip:+12025550100@twilio.example')
        self.assertNotIn('Shall we deploy', self.events.message(self.events.pending(self.thread)))

    def test_resolved_message_or_fresh_operator_withholds_stale_opening(self):
        request_id = self.prepared_call()
        self.event(peeruri=f'sip:voip-{request_id}@twilio.example')
        event = self.events.pending(self.thread)
        new_thread = '22222222-2222-2222-2222-222222222222'
        atomic_write(self.project.state / 'operator-thread-id', new_thread)
        self.assertNotIn('Shall we deploy', self.events.message(event))
        atomic_write(self.project.state / 'operator-thread-id', self.thread)
        self.box.address('agenda', 'Handled before answer')
        self.assertNotIn('Shall we deploy', self.events.message(event))

    def test_snapshot_recovers_exact_outgoing_identity(self):
        request_id = self.prepared_call()
        text = ('--- Active calls (1) ---\n'
                f'> [line 1, id one]  00:14  ESTABLISHED    sip:voip-{request_id}@twilio.example\n')
        self.events.snapshot(parse_calls(text), parse_request_ids(text))
        event = self.events.pending(self.thread)
        self.assertEqual(event['outgoing_request_id'], request_id)
        self.assertIn('Shall we deploy', self.events.message(event))

    async def test_submission_failure_keeps_event_pending(self):
        self.event()
        stop = asyncio.Event()
        class Rpc:
            async def request(self, method, params):
                stop.set()
                raise SetupError("transport unavailable")
        await deliver(self.project, Rpc(), stop)
        event = CallEvents(self.project).pending(self.thread)
        self.assertEqual(event["deliveries"][self.thread]["state"], "submitting")

    async def test_netstring_handles_fragmented_and_consecutive_frames(self):
        reader = asyncio.StreamReader()
        value = {"event": True, "type": "CALL_ESTABLISHED"}
        body = json.dumps(value).encode()
        frame = str(len(body)).encode() + b":" + body + b","
        async def feed():
            for b in frame + frame:
                reader.feed_data(bytes([b]))
                await asyncio.sleep(0)
        task = asyncio.create_task(feed())
        self.assertEqual(await read_frame(reader), value)
        self.assertEqual(await read_frame(reader), value)
        await task
        for data in (b"1048577:", b"x:", b"2:{}!"):
            bad = asyncio.StreamReader(); bad.feed_data(data)
            with self.assertRaises(SetupError):
                await read_frame(bad)

    async def test_delivery_uses_same_thread_and_only_marks_after_rpc_acceptance(self):
        self.event()
        stop = asyncio.Event()
        observed = []
        outer = self
        class Rpc:
            async def request(self, method, params):
                observed.append((method, params))
                event = outer.events.pending(outer.thread)
                outer.assertEqual(event["deliveries"][outer.thread]["state"], "submitting")
                stop.set()
                return {}
        await deliver(self.project, Rpc(), stop)
        self.assertEqual(observed[0][0], "thread/realtime/appendText")
        self.assertEqual(observed[0][1]["threadId"], self.thread)
        self.assertNotIn("mailbox", observed[0][1]["text"])
        self.assertIsNone(self.events.pending(self.thread))
