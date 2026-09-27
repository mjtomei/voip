#!/usr/bin/env python3
"""Install the managed CLI; does not deploy, start services, or place calls."""
import argparse
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
parser.add_argument('--audio', action='store_true', help='Build the pinned Baresip and SIP keepalive patch')
parser.add_argument('--test-tools', action='store_true', help='Install local TTS and STT tools')
args = parser.parse_args()
environment = root / '.tools/voice-venv'
subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
subprocess.run([str(environment / 'bin/pip'), 'install', '-r', str(root / 'requirements-voice.txt')], check=True)
target = args.bin_dir.expanduser() / 'voip'
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists() or target.is_symlink():
    if target.resolve() != root / 'bin/voip':
        raise SystemExit('Refusing to overwrite another voip executable; choose --bin-dir')
else: target.symlink_to(root / 'bin/voip')
if args.audio:
    subprocess.run([sys.executable, str(root / 'scripts/build-baresip.py')], check=True)
if args.test_tools:
    for script in ('install-tts.py', 'install-stt.py'):
        subprocess.run([sys.executable, str(root / 'scripts' / script)], check=True)
print('Installed ' + str(target) + '; configuration and service startup are explicit next steps.')
