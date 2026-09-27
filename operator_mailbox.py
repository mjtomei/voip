"""Durable text messages for the phone operator; reading never resolves a message."""
import json
import hashlib
import os
import re
import time
import uuid

from voip import SetupError, atomic_write, lock


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Mailbox:
    def __init__(self, project, recipient=None, number=None):
        from voice_config import resolve_recipient
        self.project = project
        self.recipient = resolve_recipient(project, recipient, number)
        # The workspace owns the project. Every session in it shares this person's
        # mailbox, while a changed phone number receives a separate empty mailbox.
        identity = self.recipient['id'] + '-' + hashlib.sha256(self.recipient['number'].encode()).hexdigest()[:16]
        self.path = project.shared_state / 'mailboxes' / (identity + '.json')
        self.mutex = self.path.with_suffix('.lock')

    def load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def post(self, sender, subject, body, key=None):
        key = key or str(uuid.uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key):
            raise SetupError("Invalid message key; use 1–120 letters, digits, '.', '_', ':', or '-'")
        for label, value, limit in (("sender", sender, 120), ("subject", subject, 240), ("body", body, 65536)):
            if not value.strip() or len(value) > limit or "\x00" in value:
                raise SetupError(f"Message {label} must contain 1–{limit} characters")
        if any(c in sender + subject for c in "\r\n"):
            raise SetupError("Sender and subject must each fit on one line")
        with lock(self.mutex):
            messages = self.load()
            if key in messages:
                old = messages[key]
                if (old["sender"], old["subject"], old["body"]) != (sender, subject, body):
                    raise SetupError("Message key already exists with different content")
                return old
            message = {"id": key, "sender": sender, "subject": subject, "body": body,
                       "recipient_id": self.recipient['id'], "recipient_number": self.recipient['number'],
                       "origin_session": self.project.session_id,
                       "created_at": time.time(), "status": "unaddressed", "addressed_at": None,
                       "resolution": ""}
            messages[key] = message
            save_json(self.path, messages)
            return message

    def migrate(self):
        """Explicitly assign a legacy session mailbox; never guess its audience."""
        source = self.project.state / 'mailbox.json'
        archive = source.with_suffix('.json.migrated')
        with lock(self.project.state / 'mailbox.lock', blocking=True), lock(self.mutex, blocking=True):
            if not source.exists():
                return {'migrated': 0, 'recipient_id': self.recipient['id']}
            if archive.exists():
                raise SetupError('A legacy mailbox archive already exists; inspect before migrating')
            legacy = json.loads(source.read_text())
            messages = self.load()
            for key, old in legacy.items():
                candidate = {**old, 'recipient_id': self.recipient['id'],
                             'recipient_number': self.recipient['number'], 'origin_session': self.project.session_id}
                if key in messages:
                    # A crash after the destination write must not undo a later
                    # address/reopen or duplicate a migrated item.
                    immutable = ('id','sender','subject','body','created_at','recipient_id','recipient_number','origin_session')
                    if any(messages[key].get(k) != candidate.get(k) for k in immutable):
                        raise SetupError('Legacy message ID conflicts with this project mailbox: ' + key)
                else:
                    messages[key] = candidate
            save_json(self.path, messages)
            os.replace(source, archive)
            return {'migrated': len(legacy), 'recipient_id': self.recipient['id'], 'archive': str(archive)}

    def list(self, all_messages=False):
        # Atomic replacement provides a complete snapshot without a writer lock.
        return sorted((m for m in self.load().values() if all_messages or m["status"] == "unaddressed"),
                      key=lambda m: (m["created_at"], m["id"]))

    def count(self):
        return len(self.list())

    def read(self, key):
        try:
            return self.load()[key]
        except KeyError:
            raise SetupError("Unknown mailbox message") from None

    def address(self, key, note="", reopen=False):
        if len(note) > 4000 or "\x00" in note:
            raise SetupError("Resolution note must be at most 4000 characters")
        with lock(self.mutex):
            messages = self.load()
            if key not in messages:
                raise SetupError("Unknown mailbox message")
            message = messages[key]
            status = "unaddressed" if reopen else "addressed"
            if message["status"] != status:
                message.update(status=status, addressed_at=None if reopen else time.time(), resolution=note)
                save_json(self.path, messages)
            return message


def render(message):
    return (f"Message: {message['id']}\nFrom: {message['sender']}\nSubject: {message['subject']}\n"
            f"Recipient: {message.get('recipient_id', 'legacy')}\nStatus: {message['status']}\n\n{message['body']}\n" +
            (f"\nResolution: {message['resolution']}\n" if message['resolution'] else ""))
