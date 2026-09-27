#!/usr/bin/env python3
"""Credential-free container acceptance. No host daemon, account or phone calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
argparse.ArgumentParser(description=__doc__).parse_args()
os.chdir(root)
directory = root / 'state/acceptance' / time.strftime('%Y%m%d-%H%M%S')
directory.mkdir(parents=True, mode=0o700)
cases = [('python', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']),
         ('node', ['npm', 'test', '--prefix', 'twilio']),
         ('audio-routing', [sys.executable, 'scripts/check-audio.py']),
         ('pin-tts-stt', [sys.executable, 'scripts/test-pin-audio.py']),
         ('sip-keepalive', [sys.executable, 'scripts/check-sip-keepalive.py'])]
results = []
for name, command in cases:
    with (directory / (name + '.log')).open('w') as log:
        started = time.time()
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    results.append({'case': name, 'exit_code': result.returncode, 'seconds': round(time.time()-started, 2),
                    'log': str(directory / (name + '.log'))})
    print(name + ': ' + ('PASS' if result.returncode == 0 else 'FAIL'), flush=True)
(directory / 'results.json').write_text(json.dumps({'boundary': 'clean container; no host daemon or telephony', 'results': results}, indent=2)+'\n')
print('Evidence: ' + str(directory / 'results.json'))
sys.exit(0 if all(r['exit_code']==0 for r in results) else 1)
