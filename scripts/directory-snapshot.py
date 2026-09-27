#!/usr/bin/env python3
"""Read host tmux for a container. Writes only a private shared snapshot file."""
import argparse
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from directory_service import snapshot
from voip import Project, atomic_write

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', required=True, type=Path)
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--watch', action='store_true')
args = parser.parse_args()
while True:
    project = Project(args.root)
    data = snapshot(project, use_file=False)
    atomic_write(args.output, json.dumps(data)+'\n')
    if not args.watch: break
    time.sleep(15)
