"""Inspect, move, skip or use one meeting agenda on an already connected call."""
from contextlib import contextmanager
from datetime import datetime, timezone
import re
import time
from zoneinfo import ZoneInfo

from call_schedule import Scheduler
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests
from voice_config import resolve_recipient
from voip import Project, SetupError, lock


def materialize_early(project, state, key):
    """Recover the durable intent; caller holds schedule.lock. Never enqueue/dial."""
    old = state['occurrences'][key]
    spec = old['spec']
    scoped = Project(project.root, spec['session'])
    person = resolve_recipient(scoped, spec['recipient'])
    if person['number'] != old['recipient_number']:
        raise SetupError('Early meeting recipient changed; inspect before recovery')
    Mailbox(scoped, spec['recipient']).post('schedule:' + spec['id'],
        spec['reason'][:240], old['body'], key)
    old.update(state='skipped', outcome='started_early', message_id=key)
    save_json(Scheduler(project).path, state)


class Occurrences:
    def __init__(self, project, clock=time.time):
        self.project, self.clock = project, clock
        self.scheduler = Scheduler(project, clock)

    def resolve(self, key, recipient, state):
        match = re.fullmatch(r'schedule:([a-z][a-z0-9_-]{0,31}):([0-9]{8}T[0-9]{4})', key)
        if not match:
            raise SetupError('Use an occurrence_key from schedule next, including its schedule ID and local minute')
        person = resolve_recipient(self.project, recipient)
        old = state['occurrences'].get(key)
        if old:
            spec = old['spec']
            stamp = old['scheduled_at']
        else:
            spec = next((s for s in self.scheduler.definitions() if s['id'] == match[1]), None)
            if spec is None:
                raise SetupError('Unknown or disabled schedule; inspect schedule list')
            try:
                naive = datetime.strptime(match[2], '%Y%m%dT%H%M')
                local = naive.replace(tzinfo=spec['_zone'], fold=0)
                stamp = local.timestamp()
                if datetime.fromtimestamp(stamp, spec['_zone']).replace(tzinfo=None) != naive:
                    raise ValueError()
                if not spec['_cron'].matches(local):
                    raise ValueError()
                # Both fall-back instants share one occurrence key. If its first
                # instant has passed without materialization, next may show fold 1.
                candidates = sorted({naive.replace(tzinfo=spec['_zone'], fold=fold).timestamp()
                                     for fold in (0, 1)})
                stamp = next((s for s in candidates if s >= int(self.clock()//60)*60), candidates[0])
            except (ValueError, OverflowError):
                raise SetupError('That local minute is not a real occurrence of this schedule') from None
            spec = {k: v for k, v in spec.items() if not k.startswith('_')}
        if (spec['id'] != match[1] or spec['session'] != self.project.session_id
                or spec['recipient'] != person['id']):
            raise SetupError('Occurrence does not belong to the selected session and recipient')
        record = Requests(self.project).load().get((old or {}).get('request_key', key))
        if (old and old.get('recipient_number', person['number']) != person['number']) or (
                record and record['spec']['to'] != person['number']):
            raise SetupError('Occurrence recipient number changed; inspect the original meeting')
        return spec, stamp, old, person

    @staticmethod
    def body(spec, old):
        if old and 'body' in old:
            text = old['body']
        else:
            from pathlib import Path
            text = Path(spec['agenda_path']).read_text()
        if not text.strip() or len(text) > 65536 or '\x00' in text:
            raise SetupError('Schedule agenda must contain 1–65536 characters')
        return text

    def agenda(self, key, recipient):
        """Read-only: expose the actual saved agenda, or the future agenda template."""
        spec, stamp, old, person = self.resolve(key, recipient, self.scheduler.load())
        # Offline skip records predate agenda materialization and only save routing.
        if 'body' not in (old or {}) and 'agenda_path' not in spec:
            current = next((s for s in self.scheduler.definitions() if s['id'] == spec['id']
                           and s['session'] == spec['session'] and s['recipient'] == spec['recipient']), None)
            if current is None:
                raise SetupError('No saved agenda or matching enabled schedule is available')
            spec = {**spec, 'agenda_path': current['agenda_path']}
        effective = (old or {}).get('rescheduled_at', stamp)
        result = {**Scheduler.upcoming(key, spec, effective, stamp if old and 'rescheduled_at' in old else None),
                  'state': old['state'] if old else 'scheduled', 'outcome': (old or {}).get('outcome'),
                  'reason': spec.get('reason'), 'agenda': self.body(spec, old),
                  'agenda_source': 'saved_occurrence' if old and 'body' in old else 'current_agenda_file',
                  'message_id': (old or {}).get('message_id'), 'prepared_brief': None,
                  'request_key': (old or {}).get('request_key')}
        message = Mailbox(self.project, person['id']).load().get(key)
        if message:
            result.update(message_id=key, message_status=message['status'])
        from call_preparation import Preparations
        briefs = [v for v in Preparations(self.project).load().values()
                  if v.get('state') == 'ready' and key in v.get('messages', {})
                  and v.get('recipient_id') == person['id']
                  and v.get('recipient_number') == person['number']]
        if briefs:
            saved = max(briefs, key=lambda v: v['prepared_at'])
            result['prepared_brief'] = {'prepared_at': saved['prepared_at'], **saved['brief'],
                                        'historical_snapshot': True}
        return result

    def connected(self, call_id, recipient):
        from call_events import CallEvents
        events = CallEvents(self.project).load()
        call = events.get('calls', {}).get(call_id, {})
        if (not events.get('observer_online') or not 0 <= self.clock()-events.get('observed_at', 0) <= 10
                or not call.get('connected') or call.get('recipient_id') != recipient):
            raise SetupError('Start early requires a current authenticated call for this session and recipient')

    @contextmanager
    def edit(self, key, recipient):
        # The schedule lock stabilizes the revision; the request lock excludes
        # the dispatcher even when the occurrence has a replacement request key.
        queue = Requests(self.project)
        with lock(self.project.shared_state / 'schedule.lock'):
            state = self.scheduler.load()
            spec, stamp, old, person = self.resolve(key, recipient, state)
            with queue.process_lock((old or {}).get('request_key', key)):
                yield state, spec, stamp, old, person, queue

    @staticmethod
    def validate_note(note):
        if not isinstance(note, str) or not note.strip() or len(note) > 2000 or '\x00' in note:
            raise SetupError('Give a brief reason of 1–2000 characters for this occurrence change')

    @staticmethod
    def editable_request(queue, key, spec, old, person):
        record = queue.load().get((old or {}).get('request_key', key))
        if record:
            if (record['spec'].get('source') != 'schedule:' + spec['id']
                    or record['spec']['session_id'] != spec['session']
                    or record['spec']['recipient_id'] != person['id']):
                raise SetupError('Request identity conflicts with this occurrence')
            settled_retry = (record['state'] == 'retry_wait' and record['attempts']
                             and all(a.get('finished') for a in record['attempts']))
            if (record['state'] not in {'queued', 'retry_wait', 'cancelled'}
                    or record.get('prompt_state') is not None
                    or (record['attempts'] and not settled_retry
                        and not (record['state'] == 'cancelled' and record.get('cancel_settled')))):
                raise SetupError('Occurrence is already dispatching, delivered or connected; inspect request/call status first')
        elif old and old['state'] in {'enqueued', 'expired'}:
            raise SetupError('Occurrence request is missing or expired; inspect saved state')
        return record

    @staticmethod
    def replacement_time(value):
        try:
            if not isinstance(value, str) or not re.fullmatch(
                    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::00)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value):
                raise ValueError()
            target = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return target.timestamp()
        except (ValueError, OverflowError):
            raise SetupError('Use a minute-aligned ISO timestamp with explicit UTC offset, e.g. 2026-10-08T15:30:00-05:00') from None

    def reschedule(self, key, recipient, at, note):
        """Reserve a replacement time; the ordinary scheduler owns its dispatch."""
        self.validate_note(note)
        target = self.replacement_time(at)
        with self.edit(key, recipient) as (state, spec, stamp, old, person, queue):
            if old and old.get('outcome') == 'rescheduled' and old['rescheduled_at'] == target:
                return self.result(key, old)
            if target <= self.clock():
                raise SetupError('The replacement call time must be in the future')
            if old and old.get('outcome') in {'started_early', 'scheduler_offline'}:
                raise SetupError('This occurrence already started or was missed; inspect its agenda first')
            if old is None and stamp < int(self.clock()//60)*60:
                raise SetupError('Cannot move an unrecorded past occurrence')
            if old is None and stamp == target:
                raise SetupError('The meeting is already scheduled at that time')
            record = self.editable_request(queue, key, spec, old, person)
            body = self.body(spec, old)
            revision = (old or {}).get('reschedule_revision', 0) + 1
            request_key = key + ':move:' + str(revision)
            if request_key in queue.load():
                raise SetupError('Replacement request identity already exists; inspect before rescheduling')
            if record:
                queue.cancel(record['key'])
            history = list((old or {}).get('reschedule_history', []))
            history.append({'from': (old or {}).get('rescheduled_at', stamp), 'to': target,
                            'changed_at': self.clock(), 'note': note, 'request_key': request_key})
            changed = {**(old or {}), 'state': 'rescheduled', 'outcome': 'rescheduled',
                       'spec': spec, 'scheduled_at': stamp, 'rescheduled_at': target,
                       'reschedule_revision': revision, 'reschedule_history': history,
                       'request_key': request_key, 'body': body, 'recipient_number': person['number'],
                       'created_at': (old or {}).get('created_at', self.clock()),
                       'changed_at': self.clock(), 'note': note}
            state['occurrences'][key] = changed
            save_json(self.scheduler.path, state)
            return self.result(key, changed)

    def change(self, key, recipient, note, call_id=None):
        """Cancel only an unsubmitted/settled request, never an active phone call."""
        self.validate_note(note)
        if call_id is not None and (not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128):
            raise SetupError('Start early requires a nonempty current call ID')
        with self.edit(key, recipient) as (state, spec, stamp, old, person, queue):
            outcome = 'started_early' if call_id else 'caller_skipped'
            if old and old.get('outcome') == outcome:
                if call_id and old.get('call_id') != call_id:
                    raise SetupError('This meeting was already started on another call; read its mailbox agenda')
                if old['state'] == 'early_pending':
                    materialize_early(self.project, state, key)
                return self.result(key, state['occurrences'][key])
            if old and old.get('outcome') in {'started_early', 'caller_skipped'}:
                raise SetupError('Occurrence already has a different disposition; inspect schedule agenda')
            if call_id:
                self.connected(call_id, person['id'])
                if (old or {}).get('rescheduled_at', stamp) <= self.clock():
                    raise SetupError('Start early selects a future meeting; read an already-due agenda with schedule agenda')
            elif stamp < int(self.clock()//60)*60 and old is None:
                raise SetupError('Cannot skip an unrecorded past occurrence')
            record = self.editable_request(queue, key, spec, old, person)
            body = self.body(spec, old) if call_id else (old or {}).get('body')
            if record:
                # Cancel first. A crash before ledger persistence leaves the same
                # request cancelled; replay cannot resurrect it or dial another.
                queue.cancel(record['key'])
            changed = {**(old or {}), 'state': 'early_pending' if call_id else 'skipped',
                       'outcome': outcome, 'spec': spec, 'scheduled_at': stamp,
                       'created_at': (old or {}).get('created_at', self.clock()),
                       'changed_at': self.clock(), 'note': note, 'recipient_number': person['number']}
            if body is not None:
                changed['body'] = body
            if call_id:
                changed['call_id'] = call_id
            state['occurrences'][key] = changed
            save_json(self.scheduler.path, state)
            if call_id:
                materialize_early(self.project, state, key)
            return self.result(key, state['occurrences'][key])

    def result(self, key, old):
        result = {'occurrence_key': key, 'state': old['state'], 'outcome': old['outcome'],
                  'note': old['note'], 'original_dial_suppressed': True,
                  'recurring_schedule_changed': False}
        if 'rescheduled_at' in old:
            result.update(Scheduler.upcoming(key, old['spec'], old['rescheduled_at'], old['scheduled_at']),
                          request_key=old['request_key'], reschedule_revision=old['reschedule_revision'])
        if old['outcome'] == 'started_early':
            message = Mailbox(self.project, old['spec']['recipient']).read(key)
            result.update(call_id=old['call_id'], message_id=key, agenda=old['body'],
                          message_status=message['status'], reason=old['spec']['reason'],
                          instruction='Discuss this agenda on the current call; do not dial or reset. '
                          'Starting early is not completion. Record outcomes and address the mailbox '
                          'agenda only when its purpose is fulfilled. Plan from now to the next actual meeting.')
        return result
