import json
from unittest.mock import patch

from service_fixture import ServiceFixture
from endpoint_health import (RegistrationHealth, aor, registration_info, sip_health, voice_health)
from voip import atomic_write, SetupError


class EndpointHealthTest(ServiceFixture):
    def running_voice(self, state='connected', started=800, connected=900, heartbeat=995):
        p = self.project
        runtime = {'pid': 123, 'starttime': '1000', 'thread_id': p.thread_id(), 'started_at': started}
        atomic_write(p.state/'last-start', str(started))
        atomic_write(p.state/'operator-process.json', json.dumps(runtime))
        atomic_write(p.state/'voice.json', json.dumps({**runtime, 'state': state, 'updated_at': connected}))
        atomic_write(p.state/'voice-activity.json', json.dumps({**runtime, 'observed_at': heartbeat,
                                                            'input_at': 0, 'output_at': 0}))
        return {'voice': state, 'codex_process_alive': True, 'supervision_paused': False}

    def registration(self):
        r = RegistrationHealth(self.project)
        text = f'--- User Agents (1) ---\n0 - {aor(self.project)} \x1b[32mOK \x1b[;m Twilio Expires 600s\n'
        return r, text

    def event(self, r, kind='REGISTER_OK', now=1000):
        r.event({'event': True, 'type': kind, 'accountaor': aor(self.project), 'param': '200 OK'}, now)

    def test_silence_is_healthy_with_current_transport_heartbeat(self):
        result = voice_health(self.project, self.running_voice(), now=1000)
        self.assertTrue(result['ok'])
        self.assertFalse(result['restart'])

    def test_live_stuck_voice_and_startup_deadline(self):
        for state, start, expected in [('connected', 800, 'voice_heartbeat_stale'),
                                       ('connecting', 800, 'voice_start_timeout')]:
            status = self.running_voice(state, started=start, connected=800, heartbeat=950)
            health = voice_health(self.project, status, now=1000)
            self.assertTrue(health['restart'])
            self.assertEqual(health['reason'], expected)
        status = self.running_voice('connecting', started=990)
        self.assertFalse(voice_health(self.project, status, now=1000)['restart'])

    def test_standby_is_healthy_without_audio_but_stale_heartbeat_recovers(self):
        status = self.running_voice('standby')
        result = voice_health(self.project, status, now=1000)
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'], 'voice_standby_ready')
        status = self.running_voice('standby', heartbeat=900)
        self.assertTrue(voice_health(self.project, status, now=1000)['restart'])

    def test_paused_operator_is_not_recovered(self):
        status = self.running_voice(heartbeat=900)
        status['supervision_paused'] = True
        self.assertFalse(voice_health(self.project, status, now=1000)['restart'])

    def test_previous_process_and_future_heartbeat_cannot_mask_stall(self):
        status = self.running_voice()
        path = self.project.state/'voice-activity.json'
        for change in [{'pid': 999}, {'starttime': '999'}, {'observed_at': 2000},
                       {'thread_id': self.team.thread_id()}]:
            self.running_voice()
            body = json.loads(path.read_text()); body.update(change)
            atomic_write(path, json.dumps(body))
            self.assertTrue(voice_health(self.project, status, now=1000)['restart'])

    def test_legacy_heartbeat_works_and_new_connection_gets_grace(self):
        status = self.running_voice()
        path = self.project.state/'voice-activity.json'
        body = json.loads(path.read_text());body.pop('pid');body.pop('starttime')
        atomic_write(path, json.dumps(body))
        self.assertTrue(voice_health(self.project, status, now=1000)['ok'])
        status = self.running_voice(connected=998, heartbeat=950)
        self.assertFalse(voice_health(self.project, status, now=1000)['restart'])

    def test_cached_registration_and_command_acceptance_are_not_remote_ack(self):
        r, info = self.registration()
        r.snapshot(info, {}, now=1000)
        self.assertFalse(sip_health(self.project, now=1000)['ok'])
        self.assertEqual(r.renewal(now=1000)['params'], 'now 0')
        r.command_result({'response': True, 'token': 'voip-registration-renew', 'ok': True})
        self.assertFalse(sip_health(self.project, now=1001)['ok'])
        self.event(r, now=1001)
        self.assertTrue(sip_health(self.project, now=1001)['ok'])

    def test_registration_loss_is_repaired_periodically_even_when_cache_says_ok(self):
        r, info = self.registration();r.snapshot(info, {}, now=1000);self.event(r)
        self.assertIsNone(r.renewal(now=1059))
        r.snapshot(info, {}, now=1061)
        self.assertEqual(r.renewal(now=1061)['command'], 'uarefresh')
        # Lack of a new registrar response must eventually remove readiness.
        r.snapshot(info, {}, now=1082)
        self.assertFalse(sip_health(self.project, now=1082)['ok'])
        self.assertIsNotNone(r.renewal(now=1082))
        self.event(r, now=1083)
        self.assertTrue(sip_health(self.project, now=1083)['ok'])

    def test_busy_and_ringing_calls_defer_registration_maintenance(self):
        for index, established in enumerate([True, False]):
            now = 1000 + 60 * index
            r, info = self.registration();r.snapshot(info, {'call': established}, now=now)
            self.assertIsNone(r.renewal(now=now))
            r.snapshot(info, {}, now=now+2)
            self.assertIsNotNone(r.renewal(now=now+2))

    def test_foreign_account_and_malformed_control_data_never_trigger_commands(self):
        r, info = self.registration()
        for text in [info.replace(aor(self.project), aor(self.team)), info.replace('(1)', '(2)'), '']:
            with self.assertRaises(SetupError):r.snapshot(text, {}, now=1000)
            self.assertIsNone(r.renewal(now=1000))
        r.event({'event': True, 'type': 'REGISTER_OK', 'accountaor': aor(self.team), 'param': '200 OK'},1000)
        self.assertFalse(r.state['registered'])

    def test_failure_backoff_and_observer_outage(self):
        r, info = self.registration();r.snapshot(info, {}, now=1000)
        r.renewal(now=1000);self.event(r,'REGISTER_FAIL',1001)
        self.assertIsNone(r.renewal(now=1002))
        self.assertIsNotNone(r.renewal(now=1016))
        self.event(r,now=1017);r.snapshot(info,{},now=1017)
        self.assertTrue(sip_health(self.project,now=1017)['ok'])
        r.offline()
        self.assertFalse(sip_health(self.project,now=1018)['ok'])
        self.assertIsNone(r.renewal(now=1018))

    def test_local_unregistered_status_overrides_an_earlier_ack(self):
        r, info = self.registration();self.event(r)
        r.snapshot(info.replace('OK ', 'ERR '), {}, now=1001)
        self.assertFalse(sip_health(self.project, now=1001)['ok'])
        self.assertIsNotNone(r.renewal(now=1001))

    def test_directory_readiness_requires_both_health_checks(self):
        from directory_service import Runtime
        runtime = object.__new__(Runtime)
        status = {'voice': 'connected', 'codex_process_alive': True}
        with patch('voip.operator_status', return_value=status), \
             patch('call_events.CallEvents.load', return_value={'observer_online': True, 'observed_at': 1000}), \
             patch('directory_service.time.time', return_value=1000):
            for voice, sip in [(False, True), (True, False), (True, True)]:
                with patch('endpoint_health.voice_health', return_value={'ok': voice}), \
                     patch('endpoint_health.sip_health', return_value={'ok': sip}):
                    self.assertEqual(runtime.ready(self.project), voice and sip)
