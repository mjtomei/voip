"""One durable request queue for messages, requested calls, and schedule occurrences."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import time

from operator_mailbox import Mailbox, save_json
from voice_config import redial_policy, resolve_recipient
from voip import Project, SetupError, lock

FINISHED = {"completed", "prompt_submitted", "cancelled", "expired", "failed"}


class Deferred(SetupError):
    """Admission changed before any prompt or call was submitted."""


class Requests:
    def __init__(self, project):
        self.project = project
        self.path = project.shared_state / "requests.json"
        self.mutex = project.shared_state / "requests.lock"

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def read(self, key):
        try:
            return self.load()[key]
        except KeyError:
            raise SetupError("Unknown request key") from None

    def update(self, key, **values):
        with lock(self.mutex, blocking=True):
            records = self.load()
            record = records[key]
            # A worker completing after cancellation must not resurrect the request.
            if record["state"] in {"cancelled", "expired"}:
                values.pop("state", None)
            record.update(values, updated_at=time.time())
            save_json(self.path, records)
            return record

    def enqueue(self, key, kind, reason, message_ids=None, recipient=None, to=None,
                mode="non_preemptive", ttl_seconds=None, redial=None, source="cli"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key):
            raise SetupError("Invalid request key")
        if kind not in {"message", "call"} or mode not in {"preemptive", "non_preemptive"}:
            raise SetupError("Invalid request kind or preemption mode")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise SetupError("Request reason must contain 1–2000 characters")
        from voice_config import request_lifetime
        policy = redial_policy(redial, self.project.profile['redial'])
        if ttl_seconds is None:
            ttl_seconds = request_lifetime(policy)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 31536000:
            raise SetupError("Request lifetime must be 1–31536000 seconds")
        contact = resolve_recipient(self.project, recipient, to)
        box = Mailbox(self.project, contact['id'])
        messages = list(dict.fromkeys(message_ids if message_ids is not None else
                                     [m["id"] for m in box.list()]))
        for identity in messages:
            box.read(identity)
        spec = {"session_id": self.project.session_id, "kind": kind, "reason": reason,
                "message_ids": messages, "recipient_id": contact["id"],
                "to": contact["number"], "mode": mode,
                "ttl_seconds": ttl_seconds, "redial": policy,
                "source": source}
        with lock(self.mutex, blocking=True):
            records = self.load()
            if key in records:
                old = records[key]
                # Omitted message selection on retry refers to the original snapshot.
                if message_ids is None:
                    spec["message_ids"] = old["spec"]["message_ids"]
                old_spec = {**old['spec'], 'redial':redial_policy(old['spec']['redial'])}
                if old_spec != spec:
                    raise SetupError("Request key already exists with different details")
                return old
            old_calls = self.project.shared_state / 'outgoing-calls.json'
            if old_calls.exists() and key in json.loads(old_calls.read_text()):
                raise SetupError('This key belongs to an existing phone attempt; inspect call status, never redial with a new key')
            now = time.time()
            record = {"key": key, "spec": spec, "state": "queued", "created_at": now,
                      "expires_at": now + ttl_seconds, "updated_at": now,
                      "prompt_state": None, "attempts": [], "next_attempt_at": now}
            records[key] = record
            save_json(self.path, records)
            return record

    def cancel(self, key):
        record = self.read(key)
        if record["state"] in FINISHED - {'cancelled'}:
            return record
        # The daemon owns credentials and reconciles phone cancellation. Mark the
        # request first so preparation and new dialing stop even if the API is down.
        return self.update(key, state="cancelled", outcome="cancelled_by_request",
                           cancel_settled=not record['attempts'])

    def resume(self, key, confirm_no_prompt=False):
        record = self.read(key)
        if record['state'] != 'blocked':
            raise SetupError('Only blocked requests can be resumed')
        from request_runtime import Runtime
        attempt = Runtime(self.project).local_attempt(record)
        if attempt and attempt['state'] in {'unknown', 'submitting'}:
            raise SetupError('Reconcile the existing phone attempt before resuming this request')
        if record.get('prompt_state') in {'unknown', 'submitting'} and not confirm_no_prompt:
            raise SetupError('Inspect session history first; --confirm-no-prompt asserts no prompt was accepted')
        return self.update(key, state='queued', prompt_state=None, waiting_for='resumed_after_inspection')


def decision(record, state, now):
    """Pure admission policy, shared by every request source."""
    if record["state"] in FINISHED or record["state"] == "blocked":
        return "done"
    if now >= record["expires_at"] and record["state"] != "active":
        return "expire"
    if record["state"] == "active":
        return "refresh"
    if not state.get("ready"):
        return "wait"
    spec = record["spec"]
    eligible = spec["mode"] == "preemptive" or (not state["backend_busy"] and state["quiet"])
    if spec["kind"] == "message":
        if state['connected'] and not state['same_recipient']:
            return 'wait'
        return "prompt" if eligible else "wait"
    # This session keeps its current call; other sessions are admitted independently.
    if state["connected"]:
        return "reuse" if state["same_recipient"] and eligible else "wait"
    if (spec["mode"] == "preemptive" and state["backend_busy"] and
            record.get("prompt_state") is None):
        return "notify"
    if state["backend_busy"] or state["phone_busy"] or now < record["next_attempt_at"]:
        return "wait"
    return "dial"


class Dispatcher:
    def __init__(self, project, runtime=None, clock=time.time):
        self.project = project
        self.queue = Requests(project)
        if runtime is None:
            from request_runtime import Runtime
            runtime = Runtime(project)
        self.runtime, self.clock = runtime, clock

    def process(self, key):
        record = self.queue.read(key)
        if record['state'] == 'cancelled' and record['attempts'] and not record.get('cancel_settled'):
            try:
                saved = self.runtime.local_attempt(record)
                if saved and saved['state'] in {'unknown', 'submitting'}:
                    return self.queue.update(key, waiting_for='reconcile_cancelled_phone_attempt')
                if saved and saved['state'] in {'active', 'cancelling'}:
                    saved = self.runtime.cancel(record, record['attempts'][-1]['key'])
                return self.queue.update(key, cancel_settled=not saved or saved['state'] not in {'active', 'cancelling'},
                                         last_attempt=saved,
                                         waiting_for='cancellation_checked')
            except (SetupError, OSError, ValueError):
                return self.queue.update(key, waiting_for='phone_cancellation_pending')
        if record["state"] in FINISHED or record["state"] == "blocked":
            return record
        try:
            if record['spec']['message_ids'] and not record['attempts']:
                box = Mailbox(Project(self.project.root, record['spec']['session_id']), record['spec']['recipient_id'])
                remaining = [k for k in record['spec']['message_ids'] if box.read(k)['status'] == 'unaddressed']
                if not remaining:
                    return self.queue.update(key, state='completed', outcome='agenda_already_addressed')
            if (record["state"] != "active" and record["attempts"] and
                    not record["attempts"][-1].get("finished")):
                saved = self.runtime.local_attempt(record)
                if saved:
                    return self.finish_attempt(record, saved)
            if record['state'] == 'active':
                return self.finish_attempt(record, self.runtime.status(record, record['attempts'][-1]['key']))
            state = self.runtime.state(record)
            action = decision(record, state, self.clock())
            if action == "expire":
                return self.queue.update(key, state="expired", outcome="expired_while_waiting")
            if action in {"wait", "done"}:
                return self.queue.update(key, waiting_for=state.get("waiting_for", "session_busy"))
            if action in {"prompt", "reuse", "notify"}:
                # Persist before submission: an ambiguous notification is inspectable,
                # never blindly repeated after a daemon restart.
                if record.get("prompt_state") in {"submitting", "unknown"}:
                    return self.queue.update(key, state="blocked", waiting_for="ambiguous_prompt_submission")
                if record.get('prompt_scope') != action and record.get('prompt_state') == 'submitted':
                    record = self.queue.update(key, prompt_state=None)
                if record.get("prompt_state") == "submitted":
                    if action != "notify":
                        return self.queue.update(key, state="prompt_submitted", outcome="context_already_submitted")
                    return record
                record = self.queue.update(key, prompt_state="submitting", prompt_scope=action)
                if record['state'] in FINISHED:
                    return record
                try:
                    result = self.runtime.prompt(record, state, reuse=action == "reuse")
                except Deferred as error:
                    return self.queue.update(key, prompt_state=None, waiting_for=str(error))
                except Exception:
                    self.queue.update(key, prompt_state="unknown", state="blocked", waiting_for="ambiguous_prompt_submission")
                    raise
                return self.queue.update(key, prompt_state="submitted", prompt_result=result,
                    state="queued" if action == "notify" else "prompt_submitted",
                    outcome="existing_call_kept_connected" if action == "reuse" else "prompt_accepted")
            if action == "refresh":
                attempt = record["attempts"][-1]
                result = self.runtime.status(record, attempt["key"])
                return self.finish_attempt(record, result)
            # The attempt key is durable before reset/preparation/provider submission.
            attempts = list(record["attempts"])
            if not attempts or attempts[-1].get("finished"):
                attempt_key = key if not attempts else "request-" + hashlib.sha256(key.encode()).hexdigest()[:32] + "-" + str(len(attempts) + 1)
                attempts.append({"key": attempt_key, "created_at": self.clock()})
                record = self.queue.update(key, attempts=attempts, state="dispatching")
            else:
                record = self.queue.update(key, state="dispatching")
            def cancelled():
                current = self.queue.read(key)
                return current["state"] in {"cancelled", "expired"} or self.clock() >= current["expires_at"]
            result = self.runtime.dial(record, attempts[-1]["key"], cancelled)
            if cancelled():
                if result.get("phone_call_sid"):
                    self.runtime.cancel(record, attempts[-1]["key"])
                return self.queue.update(key, state="expired", outcome="cancelled_or_expired_during_preparation")
            return self.finish_attempt(record, result)
        except (SetupError, OSError, ValueError, KeyError) as error:
            # A known durable phone attempt always takes precedence over a worker error.
            current = self.queue.read(key)
            saved = self.runtime.local_attempt(current)
            if saved:
                if saved["state"] in {"unknown", "submitting"}:
                    return self.queue.update(key, state="blocked", waiting_for="ambiguous_phone_submission")
                if saved["state"] == "active":
                    return self.queue.update(key, state="active")
                return self.finish_attempt(current, saved)
            # Preserve the same preparation/attempt key. The next pass can resume it.
            return self.queue.update(key, waiting_for=str(error) if isinstance(error, SetupError) else type(error).__name__)

    def finish_attempt(self, record, result):
        key = record["key"]
        if result["state"] in {"unknown", "submitting"}:
            return self.queue.update(key, state="blocked", waiting_for="ambiguous_phone_submission")
        if result["state"] in {"active", "cancelling"}:
            return self.queue.update(key, state="active", last_attempt=result)
        attempts = list(record["attempts"])
        attempts[-1] = {**attempts[-1], "finished": True, "outcome": result.get("outcome", result["state"])}
        if result['state'] == 'skipped':
            return self.queue.update(key, state='completed', attempts=attempts, last_attempt=result,
                                     outcome=result['outcome'])
        if result.get("connected"):
            return self.queue.update(key, state="completed", attempts=attempts, last_attempt=result,
                                     outcome="connected_call_ended")
        policy = record["spec"]["redial"]
        outcome = result.get("phone_status", result["state"])
        retry = outcome in policy["retry_on"] or (outcome == "completed" and
                  "ended_without_sip_connection" in policy["retry_on"])
        if retry and len(attempts) < policy["max_attempts"] and self.clock() < record["expires_at"]:
            from voice_config import retry_delay
            delay = retry_delay(policy, len(attempts))
            return self.queue.update(key, state="retry_wait", attempts=attempts, last_attempt=result,
                                     next_attempt_at=self.clock() + delay, outcome=outcome)
        return self.queue.update(key, state="failed", attempts=attempts, last_attempt=result, outcome=outcome)

    def run(self, stop):
        from call_schedule import Scheduler
        from pin_delivery import Deliveries
        scheduler = Scheduler(self.project)
        deliveries = Deliveries(self.project)
        delivery_future = None
        history_future = None
        next_history_check = 0
        pending, last = {}, {}
        next_schedule_check = 0
        with lock(self.project.shared_state / "requests-worker.lock"), ThreadPoolExecutor(max_workers=16) as executor:
            while not stop.is_set():
                if self.project.root == self.project.provider_root and time.monotonic() >= next_history_check:
                    if history_future is None or history_future.done():
                        if history_future is not None:
                            try: history_future.result()
                            except Exception as error: print('Call history waiting: '+type(error).__name__,flush=True)
                        from call_history import History
                        history_future = executor.submit(History(self.project).poll)
                        next_history_check = time.monotonic()+15
                if time.monotonic() >= next_schedule_check:
                    next_schedule_check = time.monotonic() + 5
                    if delivery_future is None or delivery_future.done():
                        if delivery_future is not None:
                            try:
                                delivery_future.result()
                            except Exception as error:
                                print('PIN delivery waiting: ' + type(error).__name__, flush=True)
                        delivery_future = executor.submit(deliveries.tick)
                    try:
                        scheduler.tick()
                    except (SetupError, OSError, ValueError) as error:
                        print('Schedule check waiting: ' + type(error).__name__, flush=True)
                        next_schedule_check = time.monotonic() + 30
                try:
                    records = self.queue.load()
                    for key, future in list(pending.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception as error:
                                print("Request worker error: " + type(error).__name__, flush=True)
                            del pending[key]
                    ordered = sorted(records.values(), key=lambda r: (r["spec"]["mode"] != "preemptive", r["created_at"]))
                    occupied = {records[k]['spec']['session_id'] for k in pending}
                    for record in ordered:
                        key = record["key"]
                        cancel_pending = record['state'] == 'cancelled' and record['attempts'] and not record.get('cancel_settled')
                        if (record["state"] in FINISHED | {"blocked"} and not cancel_pending) or key in pending or len(pending) >= 16:
                            continue
                        if time.monotonic() - last.get(key, 0) < 3:
                            continue
                        identity = record['spec']['session_id']
                        if identity in occupied and record['spec']['mode'] != 'preemptive':
                            continue
                        last[key] = time.monotonic()
                        pending[key] = executor.submit(self.process, key)
                        occupied.add(identity)
                except (SetupError, OSError, ValueError) as error:
                    print("Request queue waiting: " + type(error).__name__, flush=True)
                stop.wait(0.5)
