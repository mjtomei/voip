#!/usr/bin/env python3
"""Extract the Ubuntu eSpeak NG executable locally for offline voice probes."""
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    for library in ("espeak-ng", "pcaudio", "sonic"):
        if not ctypes.util.find_library(library):
            raise SystemExit("Missing TTS runtime. Install espeak-ng (including its data and libraries) on the host.")
    directory = ROOT / ".build" / "tts"
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["apt-get", "download", "espeak-ng"], cwd=directory, check=True)
    packages = list(directory.glob("espeak-ng_*.deb"))
    if len(packages) != 1:
        raise SystemExit("Expected one espeak-ng package; remove obsolete copies from .build/tts")
    extracted = directory / "extracted"
    subprocess.run(["dpkg-deb", "-x", str(packages[0]), str(extracted)], check=True)
    destination = ROOT / ".tools" / "bin" / "espeak-ng"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(extracted / "usr/bin/espeak-ng", destination)
    env = os.environ.copy()
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("PULSE_SERVER", f"unix:{runtime}/pulse/native")
    subprocess.run([str(destination), "--version"], check=True, env=env)
    print("Installed local TTS executable; no system packages changed.")


if __name__ == "__main__":
    main()
