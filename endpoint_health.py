"""Local transport health; never infer model responsiveness from silence."""
import json
import re
import time

from voip import SetupError, atomic_write

VOICE_START_SECONDS = 120
VOICE_HEARTBEAT_SECONDS = 30
REGISTRATION_REFRESH_SECONDS = 60
REGISTRATION_FRESH_SECONDS = 150
REGISTRATION_WAIT_SECONDS = 20
REGISTRATION_RETRY_SECONDS = 15


def read_state(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fresh(timestamp, maximum, now):
    return (isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
            and 0 < timestamp <= now + 5 and now - timestamp <= maximum)


def voice_health(project, status=None, now=None):
    from voip import operator_status
    now = time.time() if now is None else now
    status = operator_status(project) if status is None else status
    result = {'ok': False, 'restart': False, 'reason': 'frontend_not_running'}
    if status.get('supervision_paused'):
        return {**result, 'reason': 'supervision_paused'}
    if project.operator.get('frontend') != 'voice':
        return {**result, 'ok': True, 'reason': 'text_frontend'}
    if not status.get('codex_process_alive'):
        return result
    runtime = read_state(project.state/'operator-process.json')
    voice = read_state(project.state/'voice.json')
    activity = read_state(project.state/'voice-activity.json')
    try:
        started = float((project.state/'last-start').read_text())
    except (OSError, ValueError):
        started = runtime.get('started_at', now)
    matching = (voice.get('pid') == runtime.get('pid') and runtime.get('starttime')
                and voice.get('starttime') == runtime['starttime']
                and voice.get('thread_id') == runtime.get('thread_id'))
    if matching and status.get('voice') == 'connecting':
        started = max(started, voice.get('updated_at', 0))
    if not matching or status.get('voice') not in {'connected', 'standby'}:
        expired = not fresh(started, VOICE_START_SECONDS, now)
        return {**result, 'restart': expired,
                'reason': 'voice_start_timeout' if expired else 'voice_starting'}
    # Legacy frontends already write the activity heartbeat, without PID fields.
    # Its timestamp must belong to this connected run; newer writers also bind PID.
    heartbeat = (activity.get('thread_id') == runtime.get('thread_id')
                 and activity.get('pid', runtime.get('pid')) == runtime.get('pid')
                 and activity.get('starttime', runtime.get('starttime')) == runtime.get('starttime')
                 and fresh(activity.get('observed_at'), VOICE_HEARTBEAT_SECONDS, now)
                 and activity['observed_at'] >= voice.get('updated_at', now))
    if heartbeat:
        return {**result, 'ok': True, 'reason': 'voice_standby_ready' if status.get('voice') == 'standby' else 'voice_transport_current'}
    grace = fresh(voice.get('updated_at'), VOICE_HEARTBEAT_SECONDS, now)
    return {**result, 'restart': not grace,
            'reason': 'voice_heartbeat_starting' if grace else 'voice_heartbeat_stale'}


def aor(project):
    return f"sip:{project.sip['username']}@{project.sip['domain']}"


def registration_info(text, expected):
    """Accept exactly our one account before sending any maintenance command."""
    plain = re.sub(r'\x1b\[[0-9;]*m', '', text)
    counts = re.findall(r'--- User Agents \((\d+)\) ---', plain)
    rows = re.findall(r'^\s*(\d+)\s+[-=>]+\s+(sip:\S+)\s+(.*)$', plain, re.M)
    if counts != ['1'] or len(rows) != 1 or rows[0][0] != '0' or rows[0][1].split(';')[0] != expected:
        raise SetupError('SIP control endpoint does not contain exactly the expected account')
    return bool(re.match(r'OK\b', rows[0][2]))


def sip_health(project, now=None):
    now = time.time() if now is None else now
    state = read_state(project.state/'sip-health.json')
    result = {'ok': False, 'reason': 'sip_observer_unavailable'}
    if (state.get('account') != aor(project) or not state.get('online')
            or not fresh(state.get('observed_at'), 10, now)):
        return result
    if not state.get('registered') or not state.get('cached_registered'):
        return {**result, 'reason': 'sip_registration_unconfirmed'}
    if not fresh(state.get('registrar_acked_at'), REGISTRATION_FRESH_SECONDS, now):
        return {**result, 'reason': 'sip_registration_check_deferred' if state.get('call_count')
                else 'sip_registration_stale'}
    return {**result, 'ok': True, 'reason': 'registrar_acknowledgement_current',
            'registrar_acked_at': state['registrar_acked_at']}


class RegistrationHealth:
    """Uses the existing observer's single control connection, never a second one."""
    def __init__(self, project):
        self.project = project
        self.path = project.state/'sip-health.json'
        previous = read_state(self.path)
        self.state = {'account': aor(project), 'online': False, 'registered': False,
                      'cached_registered': False, 'registrar_acked_at': 0,
                      'last_attempt_at': 0, 'pending_since': None, 'call_count': None}
        if previous.get('account') == aor(project):
            # Keep recent authoritative acknowledgements across observer reloads.
            for key in ('registered', 'registrar_acked_at', 'last_attempt_at'):
                self.state[key] = previous.get(key, self.state[key])
        self.verified_account = False

    def save(self):
        atomic_write(self.path, json.dumps(self.state)+'\n')

    def event(self, value, now=None):
        now = time.time() if now is None else now
        if not value.get('event') or value.get('accountaor', '').split(';')[0] != self.state['account']:
            return
        kind = value.get('type')
        if kind == 'REGISTER_OK' and re.match(r'^2\d\d\b', value.get('param', '')):
            self.state.update(registered=True, registrar_acked_at=now, pending_since=None,
                              last_result='registrar_accepted')
        elif kind in {'REGISTER_FAIL', 'UNREGISTERING'}:
            self.state.update(registered=False, pending_since=None, last_result='registration_failed')
        else:
            return
        self.save()

    def snapshot(self, registration, calls, now=None):
        now = time.time() if now is None else now
        cached = registration_info(registration, self.state['account'])
        self.verified_account = True
        self.state.update(online=True, observed_at=now, cached_registered=cached,
                          call_count=len(calls))
        pending = self.state['pending_since']
        if pending is not None and now - pending >= REGISTRATION_WAIT_SECONDS:
            self.state.update(pending_since=None, registered=False, last_result='registration_timeout')
        self.save()

    def renewal(self, now=None):
        now = time.time() if now is None else now
        if (not self.verified_account or not self.state['online'] or self.state['call_count'] != 0
                or self.state['pending_since'] is not None
                or now - self.state['last_attempt_at'] < REGISTRATION_RETRY_SECONDS):
            return None
        if (self.state['registered'] and self.state['cached_registered']
                and fresh(self.state['registrar_acked_at'], REGISTRATION_REFRESH_SECONDS, now)):
            return None
        self.state.update(last_attempt_at=now, pending_since=now, last_result='registration_requested')
        self.save()  # Persist attempt before sending; command acceptance is not a registrar ACK.
        return {'command': 'uarefresh', 'params': 'now 0', 'token': 'voip-registration-renew'}

    def command_result(self, value):
        if value.get('response') and value.get('token') == 'voip-registration-renew' and not value.get('ok'):
            self.state.update(registered=False, pending_since=None, last_result='registration_command_failed')
            self.save()

    def offline(self):
        self.verified_account = False
        self.state.update(online=False, observed_at=time.time(), pending_since=None)
        self.save()


def summary(project):
    return {'voice': voice_health(project), 'sip': sip_health(project),
            'limits': {'voice_start_seconds': VOICE_START_SECONDS,
                       'voice_heartbeat_seconds': VOICE_HEARTBEAT_SECONDS,
                       'registration_refresh_seconds': REGISTRATION_REFRESH_SECONDS},
            'scope': 'Transport checks; model responsiveness and audible speech are not measured'}
