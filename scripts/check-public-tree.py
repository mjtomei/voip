#!/usr/bin/env python3
"""Inspect tracked/indexed public files; print findings by path, never secret values."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
result = subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True)
if result.returncode:
    print('Run this check in the prepared Git source tree before publishing.',file=sys.stderr)
    raise SystemExit(1)
paths = [Path(p) for p in result.stdout.decode().split('\0') if p]
if not paths:
    print('No staged/tracked public files; stage the intended source tree first.',file=sys.stderr)
    raise SystemExit(1)
bad=[]
private_roots={'state','sessions','.tools','.build','baresip','node_modules'}
private_files={'server.toml','recipients.toml','schedules.toml','config.toml','.env','LICENSE'}
public_templates={'state/twilio.env.example', 'sessions/default.toml.example',
                  'sessions/daily.toml.example'}
for p in paths:
    if ((p.parts[0] in private_roots and p.as_posix() not in public_templates)
            or p.name.endswith(('.wav','.pcm','.sqlite','.jsonl'))
            or (p.name.startswith('.env') and p.name != '.env.example')
            or (len(p.parts)==1 and p.name in private_files)):
        bad.append((str(p),'private/runtime file or owner-excluded license'))
        continue
    content=subprocess.run(['git','-C',str(root),'show',':'+p.as_posix()],capture_output=True,check=True).stdout
    if re.search(rb'(?:sk-proj-[A-Za-z0-9_-]{25,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)',content):
        bad.append((str(p),'possible credential'))
    if re.search(rb'/home/(?!operator\b|voice\b|voipapp\b)[A-Za-z0-9_-]+/|/Users/[A-Za-z0-9_-]+/',content):
        bad.append((str(p),'personal absolute home path'))
for p in ['server.toml.example','server.container.toml.example','recipients.toml.example',
          'schedules.toml.example','sessions/default.toml.example','sessions/daily.toml.example',
          'state/twilio.env.example','compose.local.yaml.example','.env.example']:
    if Path(p) not in paths: bad.append((p,'required example missing from index'))
for p,reason in bad: print(p+': '+reason)
print(f'Checked {len(paths)} public files; {len(bad)} finding(s). This is not a full credential scan.')
sys.exit(bool(bad))
