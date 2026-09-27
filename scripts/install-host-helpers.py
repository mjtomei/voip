#!/usr/bin/env python3
"""Install host daemon recovery and tmux discovery for a container deployment.

Writes user units; starts/enables them only with --enable. It never stops the
shared Codex daemon, operates worker sessions, or starts the phone container.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from voip import Project, SetupError, atomic_write
from directory_service import settings


def render(project, codex, codex_home):
    quote=lambda value:json.dumps(str(value).replace('%','%%').replace('$','$$'))
    command=lambda values:' '.join(quote(v) for v in values)
    units={
        'voip-host-codex.service':
            '[Unit]\nDescription=Ensure the host Codex daemon is available\n'
            '[Service]\nType=oneshot\nKillMode=process\nEnvironment='+quote('CODEX_HOME='+str(codex_home))+'\n'
            'ExecStart='+command([codex,'app-server','daemon','start'])+'\n',
        'voip-host-codex.timer':
            '[Unit]\nDescription=Recover the shared host Codex daemon\n'
            '[Timer]\nOnBootSec=5\nOnUnitActiveSec=30\nUnit=voip-host-codex.service\n'
            '[Install]\nWantedBy=timers.target\n'}
    enabled=['voip-host-codex.timer']
    cfg=settings(project)
    if cfg['enabled']:
        if not cfg['snapshot_file']:raise SetupError('Configure directory_extensions.snapshot_file for container discovery')
        output=(project.root/cfg['snapshot_file']).resolve()
        name='voip-host-directories-'+hashlib.sha256(str(project.root).encode()).hexdigest()[:12]+'.service'
        units[name]=('[Unit]\nDescription=Publish host tmux directory discovery for voice\n'
            '[Service]\nExecStart='+command([sys.executable,ROOT/'scripts/directory-snapshot.py',
                '--root',project.root,'--output',output,'--watch'])+'\n'
            'Restart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n')
        enabled.append(name)
    return units,enabled


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--codex',default='codex')
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    parser.add_argument('--unit-dir',type=Path,default=Path.home()/'.config/systemd/user')
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    codex=shutil.which(args.codex)
    if not codex:raise SetupError('Host Codex executable is not available')
    units,enabled=render(Project(args.root),codex,args.codex_home.resolve())
    for name,text in units.items():atomic_write(args.unit_dir/name,text,0o644)
    if args.enable:
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','enable','--now',*enabled],check=True)
    print(json.dumps({'units':list(units),'startup_units':enabled,'enabled':args.enable,
                      'phone_container_started':False,'shared_daemon_stopped':False},indent=2))


if __name__=='__main__':
    try:main()
    except (SetupError,OSError,subprocess.CalledProcessError) as error:
        print(str(error) if isinstance(error,SetupError) else 'Host helper installation failed',file=sys.stderr)
        raise SystemExit(1)
