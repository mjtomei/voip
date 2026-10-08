"""Numeric five-field cron schedules produce ordinary durable call requests."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from operator_mailbox import Mailbox, save_json
from requests_queue import Requests
from voice_config import configured_path, name, read_toml, resolve_recipient, redial_policy
from voip import Project, SetupError, lock


class Cron:
    def __init__(self, expression):
        if not isinstance(expression, str) or len(expression.split()) != 5:
            raise SetupError("Cron needs five numeric fields: minute hour day month weekday")
        self.parts = expression.split()
        self.fields = [self.field(part, low, high) for part, (low, high) in zip(
            self.parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)))]
        if 7 in self.fields[4]:
            self.fields[4].add(0)

    @staticmethod
    def field(text, low, high):
        values = set()
        try:
            for term in text.split(','):
                split = term.split('/')
                if len(split) > 2:
                    raise ValueError()
                base = split[0]; step = int(split[1]) if len(split) == 2 else 1
                if step < 1:
                    raise ValueError()
                if base == '*':
                    first, last = low, high
                elif '-' in base:
                    first, last = map(int, base.split('-'))
                else:
                    first = int(base); last = high if len(split) == 2 else first
                if not low <= first <= last <= high:
                    raise ValueError()
                values.update(range(first, last + 1, step))
        except ValueError:
            raise SetupError("Invalid numeric cron field") from None
        return values

    def matches(self, local):
        minute, hour, dom, month, dow = self.fields
        if local.minute not in minute or local.hour not in hour or local.month not in month:
            return False
        day_match = local.day in dom
        week_match = (local.weekday() + 1) % 7 in dow
        # Traditional cron: restricted day-of-month and weekday are alternatives.
        return ((day_match and week_match) if self.parts[2].startswith('*') or self.parts[4].startswith('*')
                else (day_match or week_match))


class Scheduler:
    def __init__(self, project, clock=time.time):
        self.project, self.clock = project, clock
        self.path = project.shared_state / 'schedule-state.json'

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'last_minute': None, 'occurrences': {}}

    @staticmethod
    def record_missed(state, now):
        """Recover gaps using schedules observed before downtime, without dialing.

        Keep occurrence identities so restarts and repeated DST minutes cannot
        double-count. New schedules do not invent a history before first observation.
        """
        missed = []
        # One-off replacements exist independently of the recurring template.
        # Count downtime at their new time, never again at the original minute.
        for key, old in state['occurrences'].items():
            if old['state'] != 'rescheduled' or old['rescheduled_at'] >= now:
                continue
            saved = old['spec']
            if saved['misfire'] == 'enqueue' and old['rescheduled_at'] >= now - saved['max_lateness_seconds']:
                continue
            old.update(state='skipped', outcome='scheduler_offline')
            missed.append(key)
        observed = state.get('tracking_minute')
        if observed is None:
            return missed
        # last_minute advances only after all due occurrences are materialized.
        # Include an unfinished minute after a crash, without inventing occurrences
        # before a schedule was enabled or its configuration was first observed.
        prior = state.get('last_minute')
        prior = observed - 60 if prior is None else prior
        if prior >= now - 60:
            return missed
        for saved in state.get('tracked_schedules', {}).values():
            cron, zone = Cron(saved['cron']), ZoneInfo(saved['timezone'])
            first = max(prior + 60, saved.get('observed_from', observed))
            for stamp in range(first, now, 60):
                if saved['misfire'] == 'enqueue' and stamp >= now - saved['max_lateness_seconds']:
                    continue
                local = datetime.fromtimestamp(stamp, zone)
                if not cron.matches(local):
                    continue
                key = 'schedule:' + saved['id'] + ':' + local.strftime('%Y%m%dT%H%M')
                if key in state['occurrences']:
                    continue
                state['occurrences'][key] = {
                    'state': 'skipped', 'outcome': 'scheduler_offline', 'scheduled_at': stamp,
                    'created_at': now, 'spec': dict(saved)}
                missed.append(key)
        return missed

    def missed(self, recipient):
        """Connection notices may recover skips before the dispatcher starts.

        This only records/counts omissions; it never enqueues calls or reads agendas.
        Call-event receipts determine which of these were already reported.
        """
        if not recipient or not self.path.exists():
            return []
        with lock(self.project.shared_state / 'schedule.lock'):
            state = self.load()
            if self.record_missed(state, int(self.clock() // 60) * 60):
                save_json(self.path, state)
            return [key for key, value in state['occurrences'].items()
                    if value['state'] == 'skipped' and value.get('outcome') == 'scheduler_offline'
                    and value['spec']['session'] == self.project.session_id
                    and value['spec']['recipient'] == recipient]

    def definitions(self):
        path = configured_path(self.project, 'schedules_file', 'schedules.toml')
        if not path.exists():
            return []
        config = read_toml(path)
        if set(config) - {'schedules'} or not isinstance(config.get('schedules', []), list):
            raise SetupError('Schedules file must contain [[schedules]] entries')
        result, seen = [], set()
        for raw in config.get('schedules', []):
            allowed = {'id', 'enabled', 'cron', 'timezone', 'session', 'recipient', 'reason',
                       'agenda_file', 'mode', 'ttl_seconds', 'misfire', 'max_lateness_seconds', 'redial'}
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise SetupError('Unknown schedule setting')
            identity = name(raw.get('id'))
            if identity in seen:
                raise SetupError('Schedule IDs must be unique')
            seen.add(identity)
            if type(raw.get('enabled', True)) is not bool:
                raise SetupError('Schedule enabled must be true or false')
            if not raw.get('enabled', True):
                continue
            profile = Project(self.project.root, raw.get('session', 'default'))
            recipient = resolve_recipient(profile, raw.get('recipient'))
            try:
                zone = ZoneInfo(raw['timezone'])
                cron = Cron(raw['cron'])
            except (KeyError, TypeError, ZoneInfoNotFoundError):
                raise SetupError('Schedules require a cron expression and an IANA timezone') from None
            reason = raw.get('reason', '')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise SetupError('A schedule needs a brief reason')
            mode = raw.get('mode', 'non_preemptive')
            if mode not in {'preemptive', 'non_preemptive'}:
                raise SetupError('Unsupported schedule preemption mode')
            misfire = raw.get('misfire', 'skip')
            if misfire not in {'skip', 'enqueue'}:
                raise SetupError('misfire must be skip or enqueue')
            from voice_config import request_lifetime
            policy = redial_policy(raw.get('redial'), profile.profile['redial'])
            ttl = raw.get('ttl_seconds', request_lifetime(policy))
            lateness = raw.get('max_lateness_seconds', 3600)
            if (type(ttl) is not int or not 1 <= ttl <= 31536000 or
                    type(lateness) is not int or not 1 <= lateness <= 604800):
                raise SetupError('Schedule TTL must be 1–31536000 seconds; maximum lateness must be 1–604800 seconds')
            agenda = raw.get('agenda_file')
            if not isinstance(agenda, str) or not (path.parent / agenda).is_file():
                raise SetupError('Schedule agenda_file must identify an existing text file')
            result.append({**raw, 'id': identity, 'session': profile.session_id, 'recipient': recipient['id'],
                           'mode': mode, 'misfire': misfire, 'ttl_seconds': ttl, 'max_lateness_seconds': lateness,
                           'redial': policy, 'agenda_path': str((path.parent / agenda).resolve()),
                           '_cron': cron, '_zone': zone})
        return result

    def next(self, count=5):
        if not 1 <= count <= 20:
            raise SetupError('Select 1–20 upcoming occurrences')
        definitions = self.definitions()
        results, seen = [], set()
        occurrences = self.load()['occurrences']
        first = int(self.clock() // 60) * 60 + 60
        for key, old in occurrences.items():
            if old['state'] == 'rescheduled' and old['rescheduled_at'] >= first:
                results.append(self.upcoming(key, old['spec'], old['rescheduled_at'], old['scheduled_at']))
        if not definitions:
            return sorted(results, key=lambda r: (r['utc_time'], r['occurrence_key']))[:count]
        for stamp in range(first, first + 370 * 86400, 60):
            for schedule in definitions:
                local = datetime.fromtimestamp(stamp, schedule['_zone'])
                key = 'schedule:' + schedule['id'] + ':' + local.strftime('%Y%m%dT%H%M')
                old = occurrences.get(key, {})
                if 'rescheduled_at' in old or old.get('state') in {'skipped', 'early_pending'}:
                    continue
                if schedule['_cron'].matches(local) and key not in seen:
                    seen.add(key)
                    results.append(self.upcoming(key, schedule, stamp))
            if len(seen) >= count:
                break
        return sorted(results, key=lambda r: (r['utc_time'], r['occurrence_key']))[:count]

    @staticmethod
    def upcoming(key, spec, stamp, original=None):
        result = {'occurrence_key': key, 'schedule': spec['id'], 'session': spec['session'],
                  'recipient': spec['recipient'],
                  'local_time': datetime.fromtimestamp(stamp, ZoneInfo(spec['timezone'])).isoformat(),
                  'utc_time': datetime.fromtimestamp(stamp, timezone.utc).isoformat()}
        if original is not None:
            result.update(rescheduled=True,
                original_local_time=datetime.fromtimestamp(original, ZoneInfo(spec['timezone'])).isoformat(),
                original_utc_time=datetime.fromtimestamp(original, timezone.utc).isoformat())
        return result

    def tick(self, dry_run=False):
        definitions = self.definitions()
        with lock(self.project.shared_state / 'schedule.lock'):
            state = self.load()
            now = int(self.clock() // 60) * 60
            prior = state['last_minute']
            results = [{'key': key, 'state': 'skipped'} for key in self.record_missed(state, now)]
            tracked = {s['id']: {k: s[k] for k in (
                'id', 'cron', 'timezone', 'session', 'recipient', 'misfire', 'max_lateness_seconds')}
                for s in definitions}
            for identity, saved in tracked.items():
                previous = state.get('tracked_schedules', {}).get(identity, {})
                unchanged = saved == {k: v for k, v in previous.items() if k != 'observed_from'}
                saved['observed_from'] = previous.get('observed_from', state.get('tracking_minute', now)) if unchanged else now
            # Persist the observed configuration before agenda/queue writes, which
            # may be interrupted. Editing a schedule does not rewrite its old gaps.
            if not dry_run and (state.get('tracked_schedules') != tracked or state.get('tracking_minute') != now or results):
                state['tracked_schedules'] = tracked
                state['tracking_minute'] = max(state.get('tracking_minute', now), now)
                save_json(self.path, state)
            def materialize(key, old):
                saved = old['spec']
                due = old.get('rescheduled_at', old['created_at'])
                if self.clock() >= due + saved['ttl_seconds']:
                    old['state'] = 'expired'
                else:
                    project = Project(self.project.root, saved['session'])
                    if ('recipient_number' in old and
                            resolve_recipient(project, saved['recipient'])['number'] != old['recipient_number']):
                        raise SetupError('Scheduled recipient number changed; inspect the saved occurrence')
                    Mailbox(project, saved['recipient']).post('schedule:' + saved['id'], saved['reason'][:240], old['body'], key)
                    record = Requests(project).enqueue(old.get('request_key', key), 'call', saved['reason'], [key],
                        recipient=saved['recipient'], mode=saved['mode'], ttl_seconds=saved['ttl_seconds'],
                        redial=saved['redial'], source='schedule:' + saved['id'])
                    Requests(project).update(record['key'], expires_at=min(record['expires_at'], due + saved['ttl_seconds']))
                    old.update(state='enqueued', request_key=record['key'])
                save_json(self.path, state)
                results.append({'key': key, 'state': old['state']})
            # A crash between mailbox and request writes must recover even after the
            # matching minute, independently of the offline misfire policy.
            if not dry_run:
                for key, old in state['occurrences'].items():
                    if old['state'] == 'early_pending':
                        from schedule_occurrences import materialize_early
                        materialize_early(self.project, state, key)
                    elif old['state'] == 'pending':
                        materialize(key, old)
            for key, old in state['occurrences'].items():
                if old['state'] == 'rescheduled' and old['rescheduled_at'] <= now:
                    if dry_run:
                        results.append({'key': key, 'session': old['spec']['session'], 'recipient': old['spec']['recipient']})
                    else:
                        old['state'] = 'pending'
                        save_json(self.path, state)
                        materialize(key, old)
            for schedule in definitions:
                first = now if prior is None or schedule['misfire'] == 'skip' else max(prior + 60, now - schedule['max_lateness_seconds'])
                for stamp in range(int(first // 60) * 60, now + 1, 60):
                    local = datetime.fromtimestamp(stamp, schedule['_zone'])
                    if not schedule['_cron'].matches(local):
                        continue
                    # Repeated fall-back wall-clock minutes produce one occurrence.
                    key = 'schedule:' + schedule['id'] + ':' + local.strftime('%Y%m%dT%H%M')
                    old = state['occurrences'].get(key)
                    if old and ('rescheduled_at' in old or old['state'] in {'enqueued', 'expired', 'skipped', 'early_pending'}):
                        continue
                    if dry_run:
                        results.append({'key': key, 'session': schedule['session'], 'recipient': schedule['recipient']})
                        continue
                    if not old:
                        body = Path(schedule['agenda_path']).read_text()
                        if not body.strip() or len(body) > 65536:
                            raise SetupError('Schedule agenda must contain 1–65536 characters')
                        old = {'state': 'pending', 'scheduled_at': stamp, 'created_at': self.clock(), 'body': body,
                               'spec': {k: v for k, v in schedule.items() if not k.startswith('_')}}
                        state['occurrences'][key] = old
                        save_json(self.path, state)
                    materialize(key, old)
            if not dry_run and prior != now:
                state['last_minute'] = now if prior is None else max(prior, now)
                save_json(self.path, state)
            return results
