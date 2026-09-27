#!/usr/bin/env python3
"""Start the on-demand voice controller from the user's normal host shell."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voip import Project, SetupError, audio_up, operator_ensure, operator_init, operator_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Send the complete test request as TTS once WebRTC connects")
    args = parser.parse_args()
    project = Project()
    if project.operator.get("frontend", "text") != "voice":
        raise SetupError('Set frontend = "voice" in the [operator] section of config.toml')
    if (project.state / "operator-paused").exists():
        raise SetupError("Supervision is paused; unpause it when you want to start the operator")
    if not (project.root / ".tools/voice-venv/bin/python").exists():
        raise SetupError("Run python3 scripts/install-voice.py first")
    subprocess.run([project.executable(project.operator["codex"]), "app-server", "daemon", "start"],
                   check=True, timeout=45)
    audio_up(project)
    if not (project.state / "operator-thread-id").exists():
        operator_init(project)
    operator_ensure(project)
    print("Waiting for the voice controller; inspect tmux session " + project.operator["session"] + " for progress.", flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = operator_status(project)
        if status["voice"] in {"connected", "standby"}:
            print(json.dumps(status, indent=2), flush=True)
            if args.test:
                subprocess.run([sys.executable, str(project.root / "scripts/test-voice.py"), "run",
                                "--response-seconds", "150"], check=True)
            print("Controller remains in tmux; realtime audio opens only for a call or bounded test.")
            return
        if time.monotonic() > deadline - 115 and not status["process_lock_held"]:
            raise SetupError("Voice frontend exited; inspect its tmux pane before retrying")
        time.sleep(0.25)
    raise SetupError("Timed out connecting voice; inspect the operator tmux pane")


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, subprocess.SubprocessError) as error:
        print("Connection error: " + str(error), file=sys.stderr)
        sys.exit(1)
