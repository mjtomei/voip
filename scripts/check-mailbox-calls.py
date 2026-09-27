#!/usr/bin/env python3
"""Test empty/nonempty mailbox notices with two short calls to our own SIP endpoint.

Never rings a handset. Requires Twilio credentials and incurs ordinary SIP call
charges. Also asks the operator, via local TTS, to read and address one test message.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import CallEvents
from operator_mailbox import Mailbox, save_json
from outgoing_calls import Calls
from voip import Project, SetupError, operator_status
from voice_acceptance import ask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-telephony', action='store_true', help='run the arranged billable SIP test')
    args = parser.parse_args()
    if not args.run_telephony:
        parser.error('Arrange this telephony test first, then pass --run-telephony')
    p = Project()
    mailbox = Mailbox(p)
    events = CallEvents(p)
    Calls(p).check_ready()
    if mailbox.count():
        raise SetupError("This acceptance test requires an empty unaddressed mailbox; leave real messages intact")
    results = []
    key = "mailbox-test-" + uuid.uuid4().hex
    challenge = str(100000 + secrets.randbelow(900000))
    marker = p.state / "voice/probe/mailbox-read.txt"
    try:
        for count in (0, 1):
            if count:
                mailbox.post("service-test", "Local mailbox acceptance test",
                    "This is the local mailbox test. Use your shell to write " + challenge +
                    " (digits only) to state/voice/probe/mailbox-read.txt in this project. "
                    "Then mark this message addressed with a short outcome note. Do not call anyone.", key)
            before = {e["id"] for e in events.load()["events"]}
            subprocess.run([sys.executable, str(ROOT / "scripts/check-twilio-sip.py")], check=True, timeout=50)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                new = [e for e in events.load()["events"] if e["id"] not in before]
                if len(new) == 2 and all(e["deliveries"].get(p.thread_id(), {}).get("state") == "submitted" for e in new):
                    break
                time.sleep(0.25)
            if [e["kind"] for e in new] != ["connected", "ended"] or new[0]["call"] != new[1]["call"]:
                raise SetupError("Expected one established/ended pair for the SIP probe")
            text = new[0]["deliveries"].get(p.thread_id(), {}).get("text", "")
            if not text or (count == 0 and "mailbox" in text.lower()) or (count == 1 and "1 unaddressed mailbox message." not in text):
                raise SetupError("Connection notice did not contain the expected mailbox count")
            results.append({"unaddressed": count, "connected_notice_submitted": True,
                            "ended_notice_submitted": new[1]["deliveries"].get(p.thread_id(), {}).get("state") == "submitted",
                            "mailbox_mentioned": "mailbox" in text.lower()})
            print(f"PASS: real SIP call with {count} unaddressed messages", flush=True)
        # The test request arrives as audio; the challenge exists only in the mailbox.
        prompt = ("This is a local audio test. Please check your unaddressed mailbox messages, "
                  "perform the local mailbox acceptance test, and mark that message addressed "
                  "after completing it. Then say: mailbox action verified.")
        speech = ask(p, p.state / 'voice/probe/mailbox-stt', prompt, ['mail ?box action verified'],
                     predicate=lambda: marker.exists() and marker.read_text().strip() == challenge
                        and mailbox.read(key)['status'] == 'addressed', timeout=150)
        result = {"checked_at": time.time(), "operator_thread_id": p.thread_id(), "call_cases": results,
                  "audio_request_read_mailbox_and_used_shell": True, "speech_verification": speech, "operator_addressed_message": True,
                  "unaddressed_after_test": mailbox.count(), "voice": operator_status(p)["voice"]}
        save_json(p.state / "mailbox-acceptance.json", result)
        print(json.dumps(result, indent=2))
    finally:
        if key in mailbox.load() and mailbox.read(key)["status"] == "unaddressed":
            mailbox.address(key, "Test harness cleanup; acceptance did not complete")


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, subprocess.SubprocessError) as error:
        print("Mailbox check failed: " + str(error), file=sys.stderr)
        sys.exit(1)
