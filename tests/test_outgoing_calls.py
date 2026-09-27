import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import Mock
from email.utils import formatdate
from urllib.parse import parse_qs, urlsplit

import voip
from outgoing_calls import Calls

ACCOUNT = "AC" + "a" * 32
SID = "CA" + "b" * 32
CHILD = "CA" + "c" * 32
FROM = "+12025550100"
TO = "+12025550101"


class FakeApi:
    def __init__(self, path):
        self.path = path
        self.creates = []
        self.updates = []
        self.failure = None
        self.events = []
        self.busy = False
        self.provider_calls = []
        self.children = []
        self.phone = {"sid": SID, "account_sid": ACCOUNT, "from": FROM, "to": TO,
                      "direction": "outbound-api", "parent_call_sid": None,
                      "status": "ringing", "duration": "0", "date_created": formatdate(time.time(), usegmt=True)}

    def request(self, url, fields=None):
        if fields is not None and url.endswith("/Calls.json"):
            saved = json.loads(self.path.read_text())
            assert any(r["state"] == "submitting" for r in saved.values())
            self.creates.append(dict(fields))
            if self.failure:
                raise self.failure
            return dict(self.phone)
        if "Status=" in url:
            return {"calls": [self.phone] if self.busy else []}
        if url.endswith("/Events.json"):
            return {"events": self.events}
        call = self.phone if url.endswith(self.phone['sid'] + ".json") else next(c for c in self.children if url.endswith(c["sid"] + ".json"))
        if fields:
            self.updates.append((call["sid"], fields["Status"]))
            call["status"] = fields["Status"]
        return dict(call)

    def items(self, url, key):
        if 'Status=' in url:
            calls = ([dict(self.phone)] if self.busy else []) + self.provider_calls
            status = parse_qs(urlsplit(url).query)['Status'][0]
            return [dict(c) for c in calls if c['status'] == status]
        if "ParentCallSid=" in url:
            return [dict(c) for c in self.children]
        return [dict(self.phone)]


class OutgoingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.root / "config.toml").write_text((voip.ROOT / "config.example.toml").read_text())
        (self.state / "operator-thread-id").write_text("11111111-1111-1111-1111-111111111111\n")
        (self.state / "twilio-provisioning.json").write_text(json.dumps({"account_sid": ACCOUNT, "function_domain": "example.twil.io"}))
        (self.state / "twilio-call-settings.json").write_text(json.dumps({"phone_number": FROM, "allowed_callers": [TO]}))
        (self.state / "twilio-call-request-secret").write_text("ab" * 32)
        self.api = FakeApi(self.state / "outgoing-calls.json")
        self.ready = Mock()
        self.preparations = Mock()
        self.preparations.ready.return_value = {"state": "ready", "messages": {},
            "brief": {"ready": True, "opening": "The review is ready.", "context": "Discuss the review."}}
        self.calls = Calls(voip.Project(self.root), self.api, self.ready, self.preparations)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, key="review-1"):
        return self.calls.start(key, "Discuss the review")

    def test_durable_request_redacted_output_and_retries_never_redial(self):
        first = self.start()
        second = self.start()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.api.creates), 1)
        self.preparations.prepare.assert_called_once()
        self.preparations.preload.assert_called_once()
        self.assertNotIn("ticket", first)
        self.assertEqual(self.calls.path.stat().st_mode & 0o777, 0o600)
        data = self.api.creates[0]
        self.assertEqual((data["From"], data["To"]), (FROM, TO))
        self.assertEqual(data["Record"], "false")
        self.assertNotIn("Discuss the review", json.dumps(data))
        self.assertTrue(parse_qs(urlsplit(data["Url"]).query)["ticket"])
        self.api.phone["status"] = "completed"
        self.start()
        self.assertEqual(len(self.api.creates), 1)

    def test_python_ticket_is_accepted_by_actual_javascript_handler(self):
        self.start()
        value = parse_qs(urlsplit(self.api.creates[0]["Url"]).query)["ticket"][0]
        data = {"context": {"VOICE_ACCOUNT_SID": ACCOUNT, "TWILIO_NUMBER": FROM,
                 "OUTBOUND_ALLOWED_RECIPIENTS": TO, "CALL_PIN": "123456",
                 "CALL_REQUEST_SECRET": "ab" * 32, "SIP_DOMAIN": "test.sip.twilio.com",
                 "SIP_USERNAME": "operator"},
                "event": {"AccountSid": ACCOUNT, "CallSid": SID, "From": FROM, "To": TO,
                 "Direction": "outbound-api", "stage": "authorize", "Digits": "123456", "ticket": value}}
        script = """
global.Twilio = require('./twilio/node_modules/twilio');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
require('./twilio/outgoing').handler(data.context, data.event, (error, response) => {
  if (error) process.exit(1);
  process.stdout.write(response.toString());
});
"""
        result = subprocess.run(["node", "-e", script], input=json.dumps(data), text=True,
                                capture_output=True, cwd=voip.ROOT, timeout=10, check=True)
        self.assertIn("<Sip>sip:operator@test.sip.twilio.com;transport=tls;secure=true</Sip>", result.stdout)

    def test_changed_details_cannot_reuse_key(self):
        self.start()
        with self.assertRaises(voip.SetupError):
            self.calls.start("review-1", "Something different")
        self.assertEqual(len(self.api.creates), 1)

    def test_unapproved_recipient_rejected_before_dial(self):
        with self.assertRaises(voip.SetupError):
            self.calls.start("review-1", "Test", "+12025550199")
        self.assertFalse(self.api.creates)

    def test_busy_session_endpoint_and_local_pending_request_block_new_dial(self):
        endpoint = 'sip:' + self.calls.project.sip['username'] + '@' + self.calls.project.sip['domain']
        self.api.provider_calls = [{'to': endpoint + ';transport=tls;secure=true', 'status': 'in-progress'}]
        with self.assertRaises(voip.SetupError): self.start()
        self.assertFalse(self.api.creates)
        self.api.provider_calls = []
        self.start()
        with self.assertRaises(voip.SetupError): self.start("review-2")
        self.assertEqual(len(self.api.creates), 1)

    def team_calls(self):
        (self.root / 'sessions').mkdir(exist_ok=True)
        (self.root / 'sessions/team.toml').write_text('reset_on_outgoing_call=false\n')
        (self.root / 'recipients.toml').write_text(
            '[recipients.default]\nnumber="' + TO + '"\npin="123456"\n'
            'sessions=["default", "team"]\nincoming_session="default"\n')
        project = voip.Project(self.root, 'team')
        voip.atomic_write(project.state / 'operator-thread-id', '22222222-2222-2222-2222-222222222222')
        api = FakeApi(self.calls.path)
        api.phone['sid'] = 'CA' + 'd' * 32
        return Calls(project, api, Mock(), self.preparations), api

    def test_same_number_calls_in_distinct_sessions_have_independent_attempts_and_cancellation(self):
        first = self.start()
        other, api = self.team_calls()
        api.provider_calls = [dict(self.api.phone), {
            'to': 'sip:operator@' + self.calls.project.sip['domain'] + ';transport=tls',
            'status': 'in-progress'}]
        second = other.start('team-review', 'Discuss team review')
        self.assertEqual(first['to'], second['to'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['phone_call_sid'], second['phone_call_sid'])
        self.assertNotEqual(first['operator_thread_id'], second['operator_thread_id'])
        self.assertEqual(len(api.creates), 1)
        other.start('team-review', 'Discuss team review')
        self.assertEqual(len(api.creates), 1)
        other.cancel('team-review')
        self.assertEqual(api.updates, [(second['phone_call_sid'], 'canceled')])
        self.assertEqual(self.calls.load()['review-1']['state'], 'active')
        self.assertEqual(self.api.updates, [])

    def test_ambiguous_attempt_blocks_its_session_but_not_another_to_same_number(self):
        self.api.failure = TimeoutError()
        with self.assertRaises(voip.SetupError): self.start()
        other, api = self.team_calls()
        result = other.start('team-review', 'Discuss team review')
        self.assertEqual(result['state'], 'active')
        self.assertEqual(len(api.creates), 1)
        with self.assertRaises(voip.SetupError): self.start('duplicate')
        self.assertEqual(len(self.api.creates), 1)

    def test_provider_pstn_calls_do_not_reserve_destination_number(self):
        self.api.provider_calls = [dict(self.api.phone),
            {**self.api.phone, 'from': TO, 'to': FROM, 'direction': 'inbound', 'status': 'in-progress'}]
        self.assertEqual(self.start()['state'], 'active')

    def test_operator_not_ready_never_dials(self):
        self.ready.side_effect = voip.SetupError("Not connected")
        with self.assertRaises(voip.SetupError): self.start()
        self.assertFalse(self.api.creates)

    def test_ambiguous_create_survives_restart_and_blocks_redial(self):
        self.api.failure = TimeoutError("Do not echo secret")
        with self.assertRaises(voip.SetupError) as error: self.start()
        self.assertNotIn("secret", str(error.exception))
        restarted = Calls(voip.Project(self.root), self.api, self.ready, self.preparations)
        self.assertEqual(restarted.start("review-1", "Discuss the review")["state"], "unknown")
        with self.assertRaises(voip.SetupError): restarted.start("review-2", "Discuss the review")
        self.assertEqual(len(self.api.creates), 1)

    def test_process_death_during_create_leaves_blocking_durable_record(self):
        self.api.failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.start()
        self.assertEqual(self.start()["state"], "unknown")
        self.assertEqual(len(self.api.creates), 1)

    def test_cancel_ends_parent_and_active_sip_leg_without_stopping_operator(self):
        self.start()
        self.api.phone["status"] = "in-progress"
        self.api.children = [{"sid": CHILD, "status": "in-progress", "duration": "12"}]
        result = self.calls.cancel("review-1")
        self.assertEqual(self.api.updates, [(SID, "completed"), (CHILD, "completed")])
        self.assertEqual(result["state"], "ended")
        self.assertEqual(result["outcome"], "connected_call_ended")

    def test_ringing_call_can_be_cancelled_before_pin_or_sip(self):
        self.start()
        result = self.calls.cancel("review-1")
        self.assertEqual(self.api.updates, [(SID, "canceled")])
        self.assertEqual(result["state"], "ended")
        self.assertFalse(result["connected"])

    def test_answered_phone_without_sip_is_not_reported_as_conversation(self):
        self.start()
        self.api.phone.update(status="completed", duration="15")
        result = self.calls.status("review-1")[0]
        self.assertFalse(result["connected"])
        self.assertEqual(result["outcome"], "ended_without_sip_connection")

    def test_ambiguous_create_requires_exact_ticket_match_to_reconcile(self):
        self.api.failure = TimeoutError()
        with self.assertRaises(voip.SetupError): self.start()
        with self.assertRaises(voip.SetupError): self.calls.reconcile("review-1")
        self.api.events = [{"request": {"url": self.api.creates[0]["Url"], "parameters": {}}}]
        result = self.calls.reconcile("review-1")
        self.assertEqual(result["phone_call_sid"], SID)
        self.assertEqual(len(self.api.creates), 1)

    def test_explicit_rejection_allows_a_new_request_but_never_retries_original(self):
        self.api.failure = voip.SetupError("Twilio request failed: HTTP 400, provider code 21211")
        with self.assertRaises(voip.SetupError): self.start()
        self.api.failure = None
        self.assertEqual(self.start()["state"], "rejected")
        self.start("review-2")
        self.assertEqual(len(self.api.creates), 2)

    def test_unready_preparation_never_creates_phone_attempt(self):
        self.preparations.ready.side_effect = voip.SetupError("Preparation still running")
        with self.assertRaises(voip.SetupError): self.start()
        self.assertFalse(self.api.creates)
        self.assertFalse(self.calls.load())

    def test_preparation_status_is_visible_without_a_phone_request(self):
        self.preparations.load.return_value = {'review-1': {'state': 'preparing', 'turn_id': 'turn-one'}}
        result = self.calls.status('review-1', local=True)[0]
        self.assertEqual(result['state'], 'preparing')
        self.assertIsNone(result['phone_call_sid'])
        self.assertFalse(self.api.creates)

    def test_failed_voice_preload_never_dials(self):
        self.preparations.preload.side_effect = voip.SetupError("Voice disconnected")
        with self.assertRaises(voip.SetupError): self.start()
        self.assertFalse(self.api.creates)
        self.assertFalse(self.calls.load())

    def test_preparation_precedes_dial_and_is_saved_before_provider_request(self):
        original = self.api.request
        def request(url, fields=None):
            if fields is not None and url.endswith("/Calls.json"):
                self.preparations.preload.assert_called_once()
                saved = self.calls.load()["review-1"]
                self.assertEqual(saved["preparation"]["brief"]["opening"], "The review is ready.")
            return original(url, fields)
        self.api.request = request
        self.start()


if __name__ == "__main__":
    unittest.main()
