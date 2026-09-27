#!/usr/bin/env python3
"""Install pinned voice transport dependencies inside the project."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
environment = ROOT / ".tools/voice-venv"
subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True)
subprocess.run([sys.executable, "-m", "pip", "--python", str(environment / "bin/python"),
    "install", "--only-binary=:all:", "--cache-dir", str(ROOT / ".build/pip-cache"),
    "-r", str(ROOT / "requirements-voice.txt")], check=True)
print("Voice dependencies installed locally; no system packages changed.")
