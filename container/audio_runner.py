"""Container-local audio with owned-child cleanup; never use a host audio server."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

children = []
def stop(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    runtime = Path(os.environ['XDG_RUNTIME_DIR'])
    for command in ('pipewire', 'pipewire-pulse', 'wireplumber'):
        log = (runtime / (command + '.log')).open('a')
        children.append(subprocess.Popen([command], stdout=log, stderr=log))
        log.close()
    for _ in range(100):
        if subprocess.run(['pactl', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        if any(p.poll() is not None for p in children):
            raise RuntimeError('Audio service failed; inspect the private runtime logs')
        time.sleep(.1)
    else:
        raise RuntimeError('Container audio server did not become ready')
    children.append(subprocess.Popen(sys.argv[1:]))
    code = children[-1].wait()
finally:
    stop()
    for child in children:
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
sys.exit(code)
