import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from call_preparation import Preparations
from call_events import CallEvents
from operator_mailbox import Mailbox
from voip import Project, ROOT, SetupError, atomic_write
from service_fixture import single_recipient


class PreparationTest(unittest.TestCase):
    def setUp(self):
        warm = patch("voice_lifecycle.warm")
        self.warm = warm.start(); self.addCleanup(warm.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'config.toml').write_text((ROOT / 'config.example.toml').read_text())
        single_recipient(root)
        self.project = Project(root)
        atomic_write(self.project.state / 'operator-thread-id', '11111111-1111-1111-1111-111111111111')
        CallEvents(self.project).snapshot({})
        self.box = Mailbox(self.project)
        self.box.post('worker', 'Review decision', 'The build passed. Ask the caller about deploying.', 'agenda')
        self.brief = {'ready': True, 'opening': 'The build passed. Should we deploy?',
                      'context': 'A deployment decision is pending; no deployment authorized yet.'}
        self.calls = []
        self.turn_status = 'completed'
        self.backend_status = 'idle'
        self.fail_submission = False
        outer = self
        class FakeRpc:
            def __init__(self, project): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, params):
                outer.calls.append((method, params))
                if method == 'thread/read':
                    return {'thread': {'status': {'type': outer.backend_status}}}
                if method == 'turn/start':
                    assert outer.prep.load()['review']['state'] == 'starting'
                    if outer.fail_submission:
                        raise SetupError('Transport lost')
                    return {'turn': {'id': 'turn-one', 'status': 'inProgress'}}
                if method == 'thread/turns/list':
                    return {'data': [{'id': 'turn-one', 'status': outer.turn_status, 'items': []}]}
                if method == 'thread/items/list':
                    return {'data': [{'turnId': 'turn-one', 'item': {'type': 'agentMessage',
                        'phase': 'final_answer', 'text': json.dumps(outer.brief)}}]}
                return {}
        self.prep = Preparations(self.project, FakeRpc)

    def prepare(self, **kwargs):
        return self.prep.prepare('review', 'Discuss the review', ['agenda'], **kwargs)

    def test_preparation_reads_agenda_without_addressing_then_preloads_voice(self):
        record = self.prepare()
        self.assertEqual(record['state'], 'ready')
        prompt = next(p for m,p in self.calls if m == 'turn/start')['input'][0]['text']
        self.assertIn('The build passed. Ask the caller about deploying.', prompt)
        self.assertEqual(self.box.count(), 1)
        self.assertEqual(self.prep.path.stat().st_mode & 0o777, 0o600)
        self.prep.preload(record)
        method, params = self.calls[-1]
        self.assertEqual(method, 'thread/realtime/appendText')
        self.assertEqual(params['role'], 'developer')
        self.assertIn(self.brief['opening'], params['text'])
        self.assertEqual(params['threadId'], self.project.thread_id())

    def test_retries_reuse_ready_preparation(self):
        self.prepare(); self.prepare()
        self.assertEqual(sum(m == 'turn/start' for m,p in self.calls), 1)

    def test_timeout_resumes_same_turn_without_resubmission(self):
        self.turn_status = 'inProgress'
        self.assertEqual(self.prepare(timeout=0)['state'], 'preparing')
        with self.assertRaises(SetupError): self.prep.ready('review', 'Discuss the review')
        self.turn_status = 'completed'
        self.assertEqual(self.prepare()['state'], 'ready')
        self.assertEqual(sum(m == 'turn/start' for m,p in self.calls), 1)

    def test_unknown_submission_does_not_submit_duplicate_turn(self):
        self.fail_submission = True
        with self.assertRaises(SetupError): self.prepare()
        with self.assertRaisesRegex(SetupError, 'unresolved'): self.prepare()
        self.assertEqual(sum(m == 'turn/start' for m,p in self.calls), 1)

    def test_busy_operator_and_connected_caller_are_not_interrupted(self):
        self.backend_status = 'active'
        with self.assertRaisesRegex(SetupError, 'busy'): self.prepare()
        self.backend_status = 'idle'
        CallEvents(self.project).snapshot({'call': True})
        with self.assertRaisesRegex(SetupError, 'no connected caller'): self.prepare()
        self.assertFalse(any(m == 'turn/start' for m,p in self.calls))

    def test_failed_or_unready_backend_result_never_becomes_ready(self):
        self.turn_status = 'failed'
        with self.assertRaises(SetupError): self.prepare()
        self.assertEqual(self.prep.load()['review']['state'], 'failed')
        self.turn_status = 'completed'; self.brief['ready'] = False
        with self.assertRaises(SetupError): self.prepare()
        with self.assertRaises(SetupError): self.prep.ready('review', 'Discuss the review')

    def test_message_resolution_or_thread_change_invalidates_ready_brief(self):
        record = self.prepare()
        self.box.address('agenda', 'Handled separately')
        with self.assertRaisesRegex(SetupError, 'changed'): self.prep.ready('review', record['reason'])
        atomic_write(self.project.state / 'operator-thread-id', '22222222-2222-2222-2222-222222222222')
        with self.assertRaisesRegex(SetupError, 'conversation changed'): self.prep.ready('review', record['reason'])

    def test_expired_preparation_can_be_refreshed_using_same_key(self):
        record = self.prepare(); record['prepared_at'] = time.time() - 601
        self.prep.put('review', record)
        with self.assertRaisesRegex(SetupError, 'expired'): self.prep.ready('review', record['reason'])
        self.assertEqual(self.prepare()['state'], 'ready')
        self.assertEqual(sum(m == 'turn/start' for m,p in self.calls), 2)

    def test_operator_supplied_brief_never_submits_a_turn_to_itself(self):
        self.backend_status = 'active'
        record = self.prepare(brief=self.brief)
        self.assertEqual(record['source'], 'operator-supplied')
        self.assertFalse(any(m == 'turn/start' for m,p in self.calls))
        self.assertEqual(self.box.count(), 1)

    def test_same_key_with_changed_reason_or_messages_rejected(self):
        self.prepare()
        with self.assertRaises(SetupError): self.prep.prepare('review', 'Other topic')
        with self.assertRaises(SetupError): self.prep.prepare('review', 'Discuss the review', [])


if __name__ == '__main__':
    unittest.main()
