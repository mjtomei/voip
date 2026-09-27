#!/usr/bin/env python3
"""Send *2# on one Twilio call to our SIP endpoint; never dial a handset.

Requires an idle operator and no active calls. Incurs ordinary SIP call charges.
Verifies native session reload with the same history/UUID while the call stays up.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import CallEvents
from operator_mailbox import save_json
from outgoing_calls import Calls
from voip import Project, Rpc, SetupError, operator_status
from voice_acceptance import ask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-telephony', action='store_true')
    parser.add_argument('--scope', choices=('frontend', 'session', 'fresh'), default='session')
    args = parser.parse_args()
    if not args.run_telephony:
        parser.error('Arrange this billable/resetting telephony test first, then pass --run-telephony')
    number = {'frontend': '1', 'session': '2', 'fresh': '3'}[args.scope]
    p = Project()
    controller = Calls(p)
    controller.check_ready()
    old = p.thread_id()
    with Rpc(p) as rpc:
        thread = rpc.request("thread/read", {"threadId": old, "includeTurns": False})["thread"]
        if thread.get("status", {}).get("type") != "idle":
            raise SetupError("Operator is busy; do not interrupt its work for a test")
    domain, username = p.sip["domain"], p.sip["username"]
    if not re.fullmatch(r"[a-z0-9-]+\.sip\.twilio\.com", domain) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", username):
        raise SetupError("Invalid configured SIP endpoint")
    settings = json.loads((p.state / "twilio-call-settings.json").read_text())
    last = p.state / "keypad-last-command.json"
    if last.exists() and time.time() - json.loads(last.read_text())["accepted_at"] < 15:
        raise SetupError("A keypad reset was just requested; wait for it to finish")
    reset_file = p.state / "operator-reset.json"
    before_reset = json.loads(reset_file.read_text()).get("id") if reset_file.exists() else None
    before_calls = {e["call"] for e in CallEvents(p).load()["events"]}
    old_pid = json.loads((p.state / "voice.json").read_text())["pid"]
    for status in ("queued", "ringing", "in-progress"):
        if controller.api.request(controller.base + "/Calls.json?Status=" + status + "&PageSize=1")["calls"]:
            raise SetupError("A call is active; wait until it finishes")
    call = controller.api.request(controller.base + "/Calls.json", {
        "To": f"sip:{username}@{domain};transport=tls;secure=true", "From": settings["phone_number"],
        "Twiml": '<Response><Pause length="3"/><Play digits="*w' + number + 'w#"/><Pause length="240"/><Hangup/></Response>',
        "Timeout": "15", "TimeLimit": "300", "Record": "false"})
    resource = controller.base + "/Calls/" + call["sid"] + ".json"
    save_json(p.state / "keypad-acceptance-call.json", {"sid": call["sid"], "created_at": time.time()})
    print("Sent one SIP-only test call with reset scope " + args.scope, flush=True)
    try:
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            record = json.loads(reset_file.read_text()) if reset_file.exists() else {}
            if record.get("id") != before_reset and record.get("status") == "completed":
                break
            time.sleep(.5)
        else:
            raise SetupError("No completed keypad reset observed")
        accepted = json.loads(last.read_text())
        new_calls = {e["call"] for e in CallEvents(p).load()["events"]} - before_calls
        call = controller.api.request(resource)
        result = {"checked_at": time.time(), "command": "*" + number + "#", "scope": record.get("scope"),
            "keypad_received_on_test_call": accepted["call"] in new_calls and accepted["scope"] == args.scope,
            "conversation_identity_correct": (p.thread_id() != old) if args.scope == "fresh" else (p.thread_id() == old),
            "frontend_replaced": json.loads((p.state / "voice.json").read_text())["pid"] != old_pid,
            "voice_connected": operator_status(p)["voice"] == "connected",
            "phone_call_stayed_connected": call["status"] == "in-progress",
            "history_backup_exists_if_required": args.scope == "frontend" or Path(record.get("backup", "/missing")).is_file()}
        speech = ask(p, p.state / 'voice/probe' / ('keypad-' + args.scope),
            'This is the keypad recovery audio test. Please say: voice recovery verified.',
            ['voice recovery verified'], timeout=150)
        result['spoken_words_verified'] = speech['spoken_words_verified']
        result['speech_verification'] = speech
        result['phone_call_stayed_connected'] = controller.api.request(resource)['status'] == 'in-progress'
        save_json(p.state / "keypad-acceptance.json", result)
        print(json.dumps(result, indent=2))
        if result["scope"] != args.scope or not all(value for key, value in result.items() if isinstance(value, bool)):
            raise SetupError("Keypad reset acceptance was incomplete")
    finally:
        call = controller.api.request(resource)
        if call["status"] in ("queued", "ringing", "in-progress"):
            controller.api.request(resource, {"Status": "completed"})


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError) as error:
        print("Keypad check failed: " + str(error), file=sys.stderr)
        sys.exit(1)
