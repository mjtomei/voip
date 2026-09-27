#!/usr/bin/env python3
"""Make one short Twilio-to-local-SIP test call; never dial a PSTN destination.

Requires the provisioning API credentials in the environment. This incurs
Twilio's ordinary SIP call charge. It tests signaling, not two-way audio.
"""
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("twilio_setup", ROOT / "scripts/twilio-setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def main():
    import voip
    project = voip.Project(ROOT)
    # Validate the configured destination rather than accepting an arbitrary To.
    domain, username = project.sip["domain"], project.sip["username"]
    import re
    if not re.fullmatch(r"[a-z0-9-]+\.sip\.twilio\.com", domain):
        raise SystemExit("Invalid local Twilio SIP domain")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", username):
        raise SystemExit("Invalid local SIP username")
    state = json.loads((ROOT / "state/twilio-provisioning.json").read_text())
    settings = json.loads((ROOT / "state/twilio-call-settings.json").read_text())
    api = setup.Api()
    base = "https://api.twilio.com/2010-04-01/Accounts/" + state["account_sid"]
    for status in ("queued", "ringing", "in-progress"):
        if api.request(base + "/Calls.json?Status=" + status + "&PageSize=1")["calls"]:
            raise SystemExit("A call is active; wait until it finishes")
    # No retry: an ambiguous response requires inspecting Twilio's call list.
    call = api.request(base + "/Calls.json", {
        "To": f"sip:{username}@{domain};transport=tls;secure=true",
        "From": settings["phone_number"],
        "Twiml": '<Response><Pause length="3"/><Hangup/></Response>',
        "Timeout": "15", "TimeLimit": "20", "Record": "false",
    })
    path = ROOT / "state/twilio-sip-probe.json"
    voip.atomic_write(path, json.dumps({"sid": call["sid"], "status": call["status"]}) + "\n")
    resource = base + "/Calls/" + call["sid"]
    deadline = time.monotonic() + 40
    while call["status"] in ("queued", "ringing", "in-progress") and time.monotonic() < deadline:
        time.sleep(1)
        call = api.request(resource + ".json")
    if call["status"] in ("queued", "ringing", "in-progress"):
        api.request(resource + ".json", {"Status": "completed"})
        raise SystemExit("SIP probe exceeded its deadline; ended test call")
    result = {key: call.get(key) for key in ("sid", "status", "duration", "start_time", "end_time")}
    result["notifications"] = [
        {"error_code": n["error_code"]}
        for n in api.request(resource + "/Notifications.json?PageSize=10")["notifications"]
    ]
    voip.atomic_write(path, json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "sid"}, indent=2))
    if call["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
