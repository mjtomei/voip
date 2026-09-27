"""Prevent false positives in independent spoken-response verification."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from voice_acceptance import matches, normalize, save_wav, transcribe, Capture, stt_paths
from voip import ROOT, SetupError


class SpeechVerificationTests(unittest.TestCase):
    def test_stt_can_use_image_tools_with_a_separate_source_mount(self):
        with patch.dict('os.environ', {'VOIP_TOOLS_DIR': '/opt/test-tools'}):
            binary, model = stt_paths()
        self.assertEqual(binary, Path('/opt/test-tools/bin/whisper-cli'))
        self.assertEqual(model, Path('/opt/test-tools/share/whisper/ggml-small.en.bin'))

    def test_word_boundaries_and_all_required_claims(self):
        self.assertTrue(matches('Done. VOICE input, and shell access verified!', ['voice input and shell access verified']))
        self.assertFalse(matches('I will verify voice input later.', ['voice input', 'shell access verified']))
        self.assertFalse(matches('unverified', ['verified']))
        self.assertFalse(matches('Silver meadow and copper forest', ['silver meadow'], ['copper forest']))
        self.assertTrue(matches('Request canceled.', ['request cancelled|request canceled']))

    def test_silence_cannot_be_transcribed_into_a_pass(self):
        with tempfile.TemporaryDirectory() as d:
            recording = Path(d) / 'silence.wav'
            save_wav(recording, bytes(32000))
            with patch('voice_acceptance.subprocess.run') as run:
                with self.assertRaises(SetupError):
                    transcribe(recording)
                run.assert_not_called()

    def test_stt_gets_only_audio_no_expected_words_or_native_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            recording = Path(d) / 'response.wav'
            save_wav(recording, b'\xff\x0f' * 16000)
            def run(argv, **kwargs):
                self.assertNotIn('--prompt', argv)
                self.assertNotIn('-p', argv)
                self.assertNotIn('--prompt-file', argv)
                self.assertEqual(argv[argv.index('-f') + 1], str(recording))
                (Path(d) / 'response.txt').write_text('actual spoken response')
                from subprocess import CompletedProcess
                return CompletedProcess(argv, 0)
            with patch('pathlib.Path.is_file', return_value=True), patch('voice_acceptance.subprocess.run', side_effect=run):
                result = transcribe(recording)
            self.assertEqual(result['text'], 'actual spoken response')
            self.assertFalse(result['expected_text_supplied_to_stt'])

    def test_speech_before_connection_cannot_satisfy_a_post_connection_check(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'early.wav'
            save_wav(path, b'\xff\x0f' * 16000 + bytes(64000))
            with patch('pathlib.Path.is_file', return_value=True), patch('voice_acceptance.subprocess.run') as run:
                with self.assertRaisesRegex(SetupError, 'No audible output'):
                    transcribe(path, start_seconds=1)
                run.assert_not_called()

    def test_wrong_audio_format_fails_before_stt(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'wrong.wav'
            save_wav(path, b'\xff\x0f' * 24000, 24000)
            with patch('pathlib.Path.is_file', return_value=True), patch('voice_acceptance.subprocess.run') as run:
                with self.assertRaisesRegex(SetupError, '16 kHz'):
                    transcribe(path)
                run.assert_not_called()

    def test_spoken_success_cannot_override_failed_tool_state(self):
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as d:
            project = SimpleNamespace(session_id='test', audio={'outgoing_sink': 'isolated'}, thread_id=lambda: 'test-thread')
            capture = Capture(project, Path(d))
            capture.raw.write_bytes(b'\xff\x0f' * 16000)
            with patch('voice_acceptance.transcribe', return_value={'text': 'task completed'}):
                with self.assertRaises(SetupError):
                    capture.verify(['task completed'], predicate=lambda: False, timeout=0)
            evidence = json.loads((Path(d) / 'result.json').read_text())
            self.assertFalse(evidence['passed'])
            self.assertFalse(evidence['state_assertion_passed'])
            self.assertFalse(evidence['spoken_words_verified'])

    def test_fixture_provider_never_falls_through_to_network(self):
        import sys
        sys.path.insert(0, str(ROOT / 'scripts'))
        from voice_e2e_fixture import Phone
        phone = Phone(None)
        with patch('outgoing_calls.api_client', side_effect=AssertionError('real provider forbidden')):
            with self.assertRaises(SetupError):
                phone.request('https://api.twilio.com/unexpected')
