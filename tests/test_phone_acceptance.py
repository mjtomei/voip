"""The handset runner must post its agenda into the requested person's mailbox."""
import contextlib
import importlib.util
import io
from unittest.mock import patch

from operator_mailbox import Mailbox
from requests_queue import Requests
from service_fixture import ServiceFixture
from voip import ROOT


class PhoneAcceptanceTests(ServiceFixture):
    def test_nondefault_recipient_agenda_and_request_have_the_same_scope(self):
        spec = importlib.util.spec_from_file_location('phone_acceptance', ROOT / 'scripts/check-phone-audio.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agenda = self.root / 'agenda.txt'
        agenda.write_text('Synthetic Bob agenda; no phone will be dialed by this unit test.')
        args = ['check-phone-audio.py', '--run-telephony', '--session', 'team', '--recipient', 'bob',
                '--request-key', 'synthetic-bob', '--agenda', str(agenda), '--expect', 'test phrase']
        with patch.object(module, 'Project', return_value=self.team), patch.object(module, 'Capture') as capture, \
             patch('sys.argv', args), contextlib.redirect_stdout(io.StringIO()):
            capture.return_value.__enter__.return_value.verify.return_value = {'spoken_words_verified': True}
            module.main()
        self.assertEqual(Mailbox(self.team, 'bob').read('synthetic-bob')['body'], agenda.read_text())
        self.assertEqual(Mailbox(self.team, 'alice').count(), 0)
        request = Requests(self.team).read('synthetic-bob')
        self.assertEqual(request['spec']['recipient_id'], 'bob')
        self.assertEqual(request['state'], 'queued')
        self.assertEqual(request['attempts'], [])
