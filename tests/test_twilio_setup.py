import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import voip

spec = importlib.util.spec_from_file_location("twilio_setup", voip.ROOT / "scripts/twilio-setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ApiBoundaryTest(unittest.TestCase):
    def api(self):
        with patch.dict(os.environ, {"TWILIO_SID": "SK" + "0" * 32, "TWILIO_SECRET": "test-secret"}, clear=True):
            return setup.Api()

    def test_credentials_never_sent_to_other_hosts(self):
        api = self.api()
        api.opener = Mock()
        for url in ("http://api.twilio.com/test", "https://example.org/test",
                    "https://api.twilio.com@example.org/test"):
            with self.assertRaises(voip.SetupError):
                api.request(url)
        api.opener.open.assert_not_called()

    def test_provider_error_cannot_echo_secret(self):
        api = self.api()
        error = urllib.error.HTTPError("https://api.twilio.com/test", 400, "test-secret", {},
                                       io.BytesIO(b'{"code": 12345, "message": "test-secret"}'))
        api.opener = Mock()
        api.opener.open.side_effect = error
        with self.assertRaises(voip.SetupError) as caught:
            api.request("https://api.twilio.com/test")
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertIn("12345", str(caught.exception))

    def test_no_cross_host_redirect(self):
        self.assertIsNone(setup.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))

    def test_pagination_follows_both_api_formats(self):
        api = self.api()
        api.request = Mock(side_effect=[{"contents": [1], "next_page_uri": "/2010-04-01/next"},
                                       {"contents": [2], "meta": {"next_page_url": None}}])
        self.assertEqual(api.items("https://api.twilio.com/2010-04-01/first", "contents"), [1, 2])
        self.assertEqual(api.request.call_args.args[0], "https://api.twilio.com/2010-04-01/next")

    def test_existing_number_routing_is_not_overwritten(self):
        deployment = setup.Setup.__new__(setup.Setup)
        deployment.state = {"function_domain": "example.twil.io"}
        deployment.api = Mock()
        deployment.verify = lambda: {"voice_url": "https://existing.example.org/incoming"}
        with self.assertRaises(voip.SetupError):
            deployment.route()
        deployment.api.request.assert_not_called()


class SipCredentialTest(unittest.TestCase):
    def test_private_generated_config_and_delimiter_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = (voip.ROOT / "config.example.toml").read_text().replace("YOUR-DOMAIN", "test-domain")
            (root / "config.toml").write_text(config)
            binary = root / ".tools/bin/baresip"
            binary.parent.mkdir(parents=True)
            binary.touch()
            module = root / ".tools/lib/baresip/modules/pulse.so"
            module.parent.mkdir(parents=True)
            module.touch()
            (module.parent / "ctrl_tcp.so").touch()
            project = voip.Project(root)
            for password in ('bad;password', 'bad"password', 'bad\npassword'):
                with self.assertRaises(voip.SetupError):
                    voip.sip_configure(project, password=password)
                self.assertFalse((root / "baresip/accounts").exists())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                voip.sip_configure(project, password="PrivateTestPassword123")
            self.assertNotIn("PrivateTestPassword123", output.getvalue())
            self.assertEqual((root / "baresip/accounts").stat().st_mode & 0o777, 0o600)
            account = (root / "baresip/accounts").read_text()
            self.assertIn("mediaenc=srtp-mand", account)
            self.assertIn("check_origin=yes", account)
            self.assertIn("call_max_calls 1", (root / "baresip/config").read_text())
            self.assertIn("net_af ipv4", (root / "baresip/config").read_text())


if __name__ == "__main__":
    unittest.main()
