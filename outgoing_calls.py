"""Immediate phone calls to the existing operator; no scheduler or new agent."""
from __future__ import annotations

import base64
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import uuid

from voip import ROOT, SetupError, atomic_write, lock, operator_status, run
from voice_config import phone_number, recipients, resolve_recipient
from twilio_source import function_source

TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}
UNRESOLVED = {"submitting", "unknown", "active", "cancelling"}


def api_client():
    spec = importlib.util.spec_from_file_location("twilio_setup", ROOT / "scripts/twilio-setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Api()


def ticket(secret, attempt):
    payload = {"v": 1, "id": attempt["id"], "from": attempt["from"], "to": attempt["to"],
               "issued": attempt["created"], "expires": attempt["created"] + 600,
               "session": attempt.get("session_id", "default"), "recipient": attempt.get("recipient_id", "default"),
               "placed_at": attempt.get("placed_at", attempt["created"])}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return body + "." + hmac.new(bytes.fromhex(secret), body.encode(), hashlib.sha256).hexdigest()


class Calls:
    def __init__(self, project, api=None, readiness=None, preparations=None):
        from call_preparation import Preparations
        self.project = project
        self.preparations = preparations or Preparations(project)
        self._api = api
        self.readiness = readiness or self.check_ready
        self.path = project.shared_state / "outgoing-calls.json"
        self.mutex = project.shared_state / "outgoing-calls.lock"
        self.deployment = json.loads((project.provider_state / "twilio-provisioning.json").read_text())
        self.settings = {"phone_number": phone_number(project),
                         "allowed_callers": [r["number"] for r in recipients(project)[0].values()]}
        account = self.deployment["account_sid"]
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account):
            raise SetupError("Invalid configured Twilio account")
        self.base = "https://api.twilio.com/2010-04-01/Accounts/" + account

    @property
    def api(self):
        if self._api is None:
            self._api = api_client()
        return self._api

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, records):
        atomic_write(self.path, json.dumps(records, indent=2) + "\n")
        # Persist the directory entry before an external dial can happen.
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key not in {"ticket"}}

    def check_ready(self):
        state = operator_status(self.project)
        if not state["codex_process_alive"] or state["voice"] not in {"connected", "standby"}:
            raise SetupError("The saved voice operator is not connected")
        from call_events import CallEvents
        observer = CallEvents(self.project).load()
        if not observer.get('observer_online') or time.time() - observer.get('observed_at', 0) >= 10:
            raise SetupError("The SIP observer is not current")
        from endpoint_health import voice_health, sip_health
        if not voice_health(self.project, state)['ok']:
            raise SetupError('The voice transport health check is pending')
        if not sip_health(self.project)['ok']:
            raise SetupError('The SIP registrar has not recently acknowledged this endpoint')
        info = self.deployment.get("functions", {}).get("outgoing", {})
        digest = hashlib.sha256(function_source(ROOT, "outgoing")).hexdigest()
        if info.get("sha256") != digest or self.deployment.get("build_sid") != self.deployment.get("deployed_build_sid"):
            raise SetupError("Deploy and verify outgoing call support with scripts/twilio-setup.py first")
        from service_config import access_bytes
        from voip import Project
        if self.deployment.get('access_asset', {}).get('sha256') != hashlib.sha256(access_bytes(Project(self.project.provider_root))).hexdigest():
            raise SetupError('Recipient or session routes changed; run config apply before dialing')

    def resource(self, sid):
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            raise SetupError("Invalid Twilio call ID")
        return self.base + "/Calls/" + sid + ".json"

    def validate_phone(self, call, record):
        if (call.get("account_sid") != self.deployment["account_sid"] or
                call.get("from") != record["from"] or call.get("to") != record["to"] or
                call.get("direction") != "outbound-api" or call.get("parent_call_sid")):
            raise SetupError("Call does not match this outgoing request")
        self.resource(call["sid"])

    def refresh(self, record):
        if not record.get("phone_call_sid"):
            if record["state"] == "submitting":
                record["state"] = "unknown"
            return
        parent = self.api.request(self.resource(record["phone_call_sid"]))
        self.validate_phone(parent, record)
        children = self.api.items(self.base + "/Calls.json?" + urllib.parse.urlencode({
            "ParentCallSid": parent["sid"], "PageSize": 100}), "calls")
        record["phone_status"] = parent["status"]
        record["phone_duration_seconds"] = int(parent.get("duration") or 0)
        record["sip_legs"] = [{"sid": c["sid"], "status": c["status"],
                               "duration_seconds": int(c.get("duration") or 0)} for c in children]
        record["updated"] = int(time.time())
        ended = parent["status"] in TERMINAL and all(c["status"] in TERMINAL for c in children)
        record["state"] = "ended" if ended else "active"
        connected = record.get("connected", False) or any(
            c["status"] in {"in-progress", "completed"} for c in children)
        record["connected"] = connected
        if (ended or connected) and self.conflicts(record) and record.get("request_key"):
            from voice_lifecycle import release
            release(self.project, record["request_key"])
        record["outcome"] = (("connected_call_ended" if connected else "ended_without_sip_connection")
                             if ended else ("connected" if connected else parent["status"]))

        self.publish_history(record)

    def publish_history(self, record):
        from call_history import History
        try: History(self.project, self.api).outgoing(record)
        except (SetupError, OSError, ValueError):
            record['history_pending'] = True
        else: record.pop('history_pending', None)

    def conflicts(self, record):
        # A phone number may participate in several independent session calls.
        # Only this session's endpoint is reserved, including ambiguous attempts.
        return record.get("session_id", "default") == self.project.session_id and not record.get("transferred")

    def provider_busy(self):
        endpoint = f"sip:{self.project.sip['username']}@{self.project.sip['domain']}"
        for status in ("queued", "ringing", "in-progress"):
            for call in self.api.items(self.base + "/Calls.json?Status=" + status + "&PageSize=100", "calls"):
                if call.get("to", "").split(";", 1)[0] == endpoint:
                    return True
        return False

    def start(self, request_key, reason, to=None, message_ids=None, recipient=None, cancelled=None):
        try:
            return self._start(request_key, reason, to, message_ids, recipient, cancelled)
        finally:
            if self.load().get(request_key, {}).get('state') not in UNRESOLVED:
                from voice_lifecycle import release
                release(self.project, request_key)

    def _start(self, request_key, reason, to=None, message_ids=None, recipient=None, cancelled=None):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", request_key):
            raise SetupError("Use a request key of 1–120 letters, digits, '.', '_', ':', or '-'")
        if not reason.strip() or len(reason) > 2000:
            raise SetupError("Provide a brief local reason for the call (1–2000 characters)")
        contact = resolve_recipient(self.project, recipient, to)
        to = contact["number"]
        # Preparation may take minutes. Never hold the call-control lock while
        # waiting for the backend, so status/cancel remain available.
        prepared = None
        if request_key not in self.load():
            with lock(self.mutex):
                records = self.load()
                for previous in records.values():
                    if previous["state"] in UNRESOLVED and self.conflicts(previous):
                        self.refresh(previous)
                        self.save(records)
                        if previous["state"] in UNRESOLVED:
                            raise SetupError("An outgoing attempt is active or unresolved; inspect call status/reconcile first")
            self.readiness()
            self.preparations.prepare(request_key, reason, message_ids, recipient=contact['id'])
            prepared = self.preparations.ready(request_key, reason, recipient=contact['id'])
        with lock(self.mutex):
            records = self.load()
            if request_key in records:
                previous = records[request_key]
                if (previous["to"] != to or previous["reason"] != reason or
                        previous.get("session_id", "default") != self.project.session_id):
                    raise SetupError("Request key already exists with different call details")
                if message_ids is not None and set(message_ids) != set(previous.get("preparation", {}).get("messages", {})):
                    raise SetupError("Request key already exists with different mailbox messages")
                self.refresh(previous)
                self.save(records)
                return self.public(previous)
            for previous in records.values():
                if previous["state"] in UNRESOLVED and self.conflicts(previous):
                    self.refresh(previous)
                    self.save(records)
                    if previous["state"] in UNRESOLVED:
                        raise SetupError("An outgoing attempt is active or unresolved; inspect call status/reconcile first")
            self.readiness()
            if self.provider_busy():
                raise SetupError("This session already has an active call; wait until it ends")
            domain = self.deployment["function_domain"]
            if not re.fullmatch(r"[a-z0-9-]+\.twil\.io", domain):
                raise SetupError("Invalid configured Function host")
            secret = (self.project.provider_state / "twilio-call-request-secret").read_text().strip()
            if not re.fullmatch(r"[0-9a-f]{64}", secret):
                raise SetupError("Invalid outgoing call request secret")
            prepared = self.preparations.ready(request_key, reason, recipient=contact['id'])
            self.preparations.preload(prepared)
            if cancelled and cancelled():
                raise SetupError("Request was cancelled or expired before dialing")
            record = {"request_key": request_key, "id": str(uuid.uuid4()), "reason": reason,
                      "session_id": self.project.session_id, "recipient_id": contact["id"],
                      "from": self.settings["phone_number"], "to": to,
                      "operator_thread_id": self.project.thread_id(), "created": int(time.time()), "placed_at": time.time(),
                      "state": "submitting", "phone_call_sid": None, "sip_legs": [],
                      "preparation": prepared}
            record["ticket"] = ticket(secret, record)
            records[request_key] = record
            self.save(records)
            try:
                call = self.api.request(self.base + "/Calls.json", {
                    "To": to, "From": record["from"],
                    "Url": "https://" + domain + "/outgoing?ticket=" + record["ticket"],
                    "StatusCallback": "https://" + domain + "/call-status", "StatusCallbackMethod": "POST",
                    "Method": "POST", "Timeout": "30", "TimeLimit": "14400", "Record": "false",
                })
                self.validate_phone(call, record)
                record.update(phone_call_sid=call["sid"], phone_status=call["status"], state="active")
                self.save(records)
                self.publish_history(record)
                self.save(records)
            except Exception as error:
                # No POST retries. Crashes also leave the durable submitting
                # record, which blocks another dial until reconciliation.
                rejected = isinstance(error, SetupError) and re.search(
                    r"HTTP (400|401|403|404|405|422|429),", str(error))
                record["state"] = "rejected" if rejected else "unknown"
                self.save(records)
                raise SetupError("Call request " + record["state"] + "; use call status/reconcile with the same request key") from None
            return self.public(record)

    def status(self, key=None, local=False):
        with lock(self.mutex):
            records = self.load()
            if key and key not in records:
                prepared = self.preparations.load().get(key)
                if prepared:
                    return [{"request_key": key, "state": prepared["state"],
                             "phone_call_sid": None, "preparation": prepared}]
                raise SetupError("Unknown call request key")
            selected = [records[key]] if key else list(records.values())
            if not local:
                for record in selected:
                    self.refresh(record)
                self.save(records)
            return [self.public(record) for record in selected]

    def reconcile(self, key):
        with lock(self.mutex):
            records = self.load()
            if key not in records:
                raise SetupError("Unknown call request key")
            record = records[key]
            if not record.get("phone_call_sid") and record["state"] in UNRESOLVED:
                query = urllib.parse.urlencode({"From": record["from"], "To": record["to"], "PageSize": 100})
                candidates = self.api.items(self.base + "/Calls.json?" + query, "calls")
                matched = []
                for call in candidates:
                    created = parsedate_to_datetime(call["date_created"]).astimezone(timezone.utc).timestamp()
                    if not record["created"] - 30 <= created <= record["created"] + 120:
                        continue
                    self.validate_phone(call, record)
                    # Events may appear only after completion. A matching
                    # destination/time alone is insufficient to adopt a call.
                    events = self.api.request(self.resource(call["sid"])[:-5] + "/Events.json")["events"]
                    for event in events:
                        request = event.get("request", {})
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.get("url", "")).query)
                        if (record["ticket"] in query.get("ticket", []) or
                                request.get("parameters", {}).get("ticket") == record["ticket"]):
                            matched.append(call["sid"])
                            break
                if len(matched) != 1:
                    raise SetupError("No unique call matched the request ticket; keep it unresolved and inspect Twilio. Call events can be delayed.")
                record["phone_call_sid"] = matched[0]
            self.refresh(record)
            self.save(records)
            return self.public(record)

    def cancel(self, key):
        with lock(self.mutex):
            records = self.load()
            if key not in records:
                raise SetupError("Unknown call request key")
            record = records[key]
            if not record.get("phone_call_sid"):
                raise SetupError("No confirmed call ID; reconcile this attempt before cancelling")
            self.refresh(record)
            if record["state"] == "ended":
                self.save(records)
                return self.public(record)
            record["state"] = "cancelling"
            self.save(records)
            # End the parent first so it cannot continue creating SIP legs.
            targets = [(record["phone_call_sid"], record["phone_status"])]
            targets += [(leg["sid"], leg["status"]) for leg in record["sip_legs"]]
            for sid, status in targets:
                if status not in TERMINAL:
                    current = self.api.request(self.resource(sid))
                    if current["status"] not in TERMINAL:
                        self.api.request(self.resource(sid), {
                            "Status": "canceled" if current["status"] in {"queued", "ringing"} else "completed"})
            self.refresh(record)
            self.save(records)
            return self.public(record)
