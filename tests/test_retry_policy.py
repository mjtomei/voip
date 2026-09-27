import argparse
import unittest
from voice_config import redial_policy, retry_delay, parse_retry_delays, request_lifetime
from voip import cli_redial, SetupError

class RetryTests(unittest.TestCase):
    def test_delay_schedule_default_attempt_count_and_lifetime(self):
        p=redial_policy({'delays_seconds':[60,300,480,3600]},redial_policy({'max_attempts':2}))
        self.assertEqual(p['max_attempts'],5)
        self.assertEqual([retry_delay(p,n) for n in range(1,5)],[60,300,480,3600])
        self.assertGreater(request_lifetime(p),sum(p['delays_seconds']))
    def test_explicit_attempt_cap_can_shorten_or_repeat_last_interval(self):
        p=redial_policy({'delays_seconds':[60,300],'max_attempts':5})
        self.assertEqual([retry_delay(p,n) for n in range(1,5)],[60,300,300,300])
    def test_legacy_backoff_still_capped(self):
        p=redial_policy({'delay_seconds':60,'backoff_multiplier':3,'max_delay_seconds':500})
        self.assertEqual([retry_delay(p,n) for n in range(1,5)],[60,180,500,500])
        self.assertEqual(request_lifetime(p),3600)
    def test_cli_duration_parse_and_mutually_exclusive_options(self):
        self.assertEqual(parse_retry_delays('1m, 5m,8m,60m'),[60,300,480,3600])
        self.assertEqual(parse_retry_delays('1s,2h,3d'),[1,7200,259200])
        with self.assertRaises(SetupError): cli_redial(argparse.Namespace(max_attempts=None,retry_delay_seconds=10,retry_delays='1m'))
    def test_invalid_delays_fail_without_silent_fallback(self):
        for value in ['','0m','-1','1.5m','1w','1m,','99999999999h']:
            with self.assertRaises(SetupError): parse_retry_delays(value)
        for value in [[True],[0],['60'],None]:
            with self.assertRaises(SetupError): redial_policy({'delays_seconds':value})
