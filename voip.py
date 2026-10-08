#!/usr/bin/env python3
"""Local audio setup and supervision for one persistent Codex operator.

Python 3.11+. PIN call delivery also uses cryptography for authenticated encryption.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parent
JSON_OUTPUT = False


class SetupError(Exception):
    pass


def run(args, *, check=True, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True,
                            text=True, timeout=30, **kwargs)
    if check and result.returncode:
        raise SetupError(result.stderr.strip() or result.stdout.strip() or
                         f"{args[0]} exited {result.returncode}")
    return result


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def lock(path, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise SetupError("Another operator/recovery process owns " + str(path))
        yield


def lock_busy(path):
    try:
        with lock(path):
            return False
    except SetupError:
        return True


class Project:
    def __init__(self, root=None, session_id="default"):
        self.root = Path(root or os.environ.get("VOIP_ROOT", ROOT)).resolve()
        self.shared_state = self.root / "state"
        self.session_id = session_id
        path = self.root / "server.toml"
        if not path.exists():
            path = self.root / "config.toml"
        if not path.exists(): path = self.root / "config.example.toml"
        self.server_path = path
        self.config = tomllib.loads(path.read_text())
        from release_config import validate_server
        validate_server(self.config, path)
        # A registration contains only a path. Project configuration, task data,
        # queues, and working directory remain owned by that external project.
        from voice_config import registered_projects
        external = registered_projects(self).get(session_id)
        if not external:
            from directory_service import generated_project
            external = generated_project(self.root, session_id)
        if external:
            child = Project(external, session_id)
            if child.provider_root != self.root:
                raise SetupError('Registered project must reference this provider_root')
            self.__dict__.update(child.__dict__)
            return
        self.provider_root = (self.root / self.config.get('service', {}).get('provider_root', '.')).resolve()
        self.provider_state = self.provider_root / 'state'
        if self.provider_root != self.root:
            provider = tomllib.loads((self.provider_root / 'server.toml').read_text())
            validate_server(provider, self.provider_root / 'server.toml')
            for section in ('audio', 'operator', 'sip'):
                self.config[section] = {**provider[section], **self.config.get(section, {})}
            # The originating number is shared server configuration, not a task override.
            self.config['twilio'] = provider['twilio']
        selected = self.config.get('service', {}).get('session_ids')
        if selected is not None and session_id not in selected:
            raise SetupError('Select a session enabled by this project with --session')
        from voice_config import session_profile
        self.profile = session_profile(self.root, self.config, session_id)
        self.state = self.shared_state if session_id == "default" else self.shared_state / "sessions" / session_id
        self.audio = dict(self.config["audio"])
        self.operator = dict(self.config["operator"])
        self.workspace = Path(self.operator.get('working_directory', self.root)).resolve()
        self.sip = dict(self.config["sip"])
        if session_id != "default":
            for key in ("incoming_sink", "outgoing_sink", "microphone"):
                self.audio[key] += "_" + session_id
            self.operator["session"] += "-" + session_id
            self.sip["username"] += "-" + session_id
            self.sip["control_port"] = self.profile["control_port"]
        self.sip_directory = self.root / "baresip" if session_id == "default" else self.state / "baresip"
        for name in (self.audio["incoming_sink"], self.audio["outgoing_sink"],
                     self.audio["microphone"], self.operator["session"]):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise SetupError("Device/session names must use letters, digits, '_' or '-'")
        if len({self.audio[k] for k in ("incoming_sink", "outgoing_sink", "microphone")}) != 3:
            raise SetupError("Audio names must be distinct")
        if self.audio["rate"] not in (16000, 24000, 48000):
            raise SetupError("Audio rate must be 16000, 24000, or 48000")
        if not isinstance(self.operator.get("extra_args", []), list) or not all(
                isinstance(x, str) for x in self.operator.get("extra_args", [])):
            raise SetupError("operator.extra_args must be an array of strings")
        if self.operator["restart_delay_seconds"] < 1:
            raise SetupError("restart_delay_seconds must be positive")
        if self.operator.get("frontend", "text") not in ("voice", "text"):
            raise SetupError("operator.frontend must be voice or text")
        if type(self.sip.get("control_port", 4444)) is not int or not 1024 <= self.sip.get("control_port", 4444) <= 65535:
            raise SetupError("sip.control_port must be an integer from 1024 to 65535")

    def permissions(self):
        return self.profile.get("permissions") or self.operator.get("permissions", "read-only")

    def permission_params(self):
        return {"approvalPolicy": "never", "sandbox": self.permissions()}

    def tools_dir(self):
        return Path(os.environ.get("VOIP_TOOLS_DIR", str(ROOT / ".tools")))

    def executable(self, name):
        # An explicit tools directory selects the tested installation (including
        # container binaries), even when the mounted workspace has host tools.
        local = (self.tools_dir() if os.environ.get('VOIP_TOOLS_DIR') else self.root / '.tools') / 'bin' / name
        if not local.is_file():
            local = self.tools_dir() / 'bin' / name
        result = str(local) if local.is_file() else shutil.which(name)
        if not result:
            raise SetupError(f"Missing {name}; see README.md installation steps")
        return result

    def cli(self):
        return [sys.executable, str(ROOT / "voip.py"), "--root", str(self.root), "--session", self.session_id]

    def mailbox_command(self):
        if self.operator.get("cli_command"):
            return shlex.join(self.operator["cli_command"] + ["--root", str(self.root), "--session", self.session_id])
        return shlex.join([sys.executable, str(ROOT / 'voip.py'), '--root', str(self.root), '--session', self.session_id])

    def instructions(self):
        from voice_config import prompt, operator_identity
        core = (ROOT / 'prompts/operator.md').read_text().replace('{{mailbox_command}}', self.mailbox_command())
        text = core + '\n' + prompt(self, 'initial', '')
        if self.session_id != "default":
            text = ("You are the voice operator for session '" + self.session_id + "'. "
                    "Use `" + self.mailbox_command() + "` for this session's mailbox and operator commands. "
                    "Sessions in this project share each recipient's mailbox but keep separate conversations.\n" + text)
        text += ("\nMailboxes belong to a recipient and this project. Use mailbox --recipient NAME before "
                 "the mailbox action; call/request notices identify the intended recipient. "
                 "Never read another recipient's messages into the current call. A delayed hangup review "
                 "must use that ended call's recipient explicitly, even if a new caller is connected.\n")
        identity = operator_identity(self)
        text += ('\nYour current service identity: ' + json.dumps(identity) +
                 '. Use the human-facing label when speaking, not the opaque session ID. '
                 'For a request to reach the home operator, use call transfer --to-home or '
                 '--to-session default; in directory mode, default is a transfer alias for home.\n')
        return {"developerInstructions": text} if text else {}

    def service_name(self, kind):
        return "voip-" + kind + ("@" + self.session_id if self.session_id != "default" else "")

    def tmux(self, *args, check=True):
        command = [self.executable("tmux")]
        if self.operator["tmux_socket"]:
            command += ["-S", self.operator["tmux_socket"]]
        return run(command + list(args), check=check)

    def pulse_env(self):
        env = os.environ.copy()
        # An explicit socket also works inside environments where libpulse's
        # default runtime-directory permission check cannot run.
        runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("PULSE_SERVER", f"unix:{runtime}/pulse/native")
        return env

    def pactl(self, *args):
        return run([self.executable("pactl"), *args], env=self.pulse_env()).stdout

    def pulse_list(self, kind):
        return json.loads(self.pactl("--format=json", "list", kind))

    def thread_id(self):
        try:
            value = (self.state / "operator-thread-id").read_text().strip()
            return str(uuid.UUID(value))
        except (OSError, ValueError):
            raise SetupError("No valid saved operator thread; run `operator init` or `operator adopt UUID`")

    def codex_command(self):
        command = [self.executable(self.operator["codex"]), "resume", self.thread_id(),
                   "--sandbox", self.permissions(), "--ask-for-approval", "never", "--no-alt-screen",
                   "--cd", str(self.workspace)]
        return command + self.operator.get("extra_args", [])

    def rpc_command(self):
        python = self.tools_dir() / "voice-venv/bin/python"
        if not python.is_file():
            raise SetupError("Run python3 scripts/install-voice.py to install the daemon transport")
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        socket = self.operator.get("daemon_socket") or str(
            codex_home / "app-server-control/app-server-control.sock")
        return [str(python), str(ROOT / "codex_rpc.py"), "--socket", socket]

    def operator_command(self):
        if self.operator.get("frontend", "text") == "text":
            return self.codex_command()
        self.thread_id()
        if self.operator.get("manage_daemon", True):
            self.executable(self.operator["codex"])
        python = self.tools_dir() / "voice-venv/bin/python"
        if not python.is_file():
            raise SetupError("Run python3 scripts/install-voice.py to install the local voice frontend")
        return [str(python), str(ROOT / "voice_frontend.py"), "--root", str(self.root), "--session", self.session_id]


def audio_modules(project):
    a = project.audio
    options = f"rate={a['rate']} channels=1 channel_map=mono"
    return [
        ("module-null-sink", f"sink_name={a['incoming_sink']} {options} "
         "sink_properties=device.description=Phone-Incoming"),
        ("module-null-sink", f"sink_name={a['outgoing_sink']} {options} "
         "sink_properties=device.description=Phone-Outgoing"),
        ("module-remap-source", f"master={a['incoming_sink']}.monitor "
         f"source_name={a['microphone']} {options} remix=no "
         "source_properties=device.description=Phone-Microphone"),
    ]


def argument_map(value):
    return dict(part.split("=", 1) for part in shlex.split(value) if "=" in part)


def audio_up(project):
    # Match live modules by configuration, never by a previously saved numeric ID.
    # Module IDs change when the audio server restarts.
    with lock(project.state / "audio.lock"):
        modules = project.pulse_list("modules")
        created = []
        try:
            for name, args in audio_modules(project):
                desired = argument_map(args)
                key = "sink_name" if name == "module-null-sink" else "source_name"
                candidates = [m for m in modules if
                              argument_map(m.get("argument", "")).get(key) == desired[key]]
                if candidates:
                    if len(candidates) != 1 or candidates[0]["name"] != name or any(
                            argument_map(candidates[0].get("argument", "")).get(k) != v
                            for k, v in desired.items()):
                        raise SetupError(f"Existing audio module conflicts with {desired[key]}")
                    continue
                # Refuse to collide with nodes created by a different mechanism.
                kind = "sinks" if key == "sink_name" else "sources"
                if any(x["name"] == desired[key] for x in project.pulse_list(kind)):
                    raise SetupError(f"Audio node {desired[key]} already exists with another owner")
                module_id = project.pactl("load-module", name, *shlex.split(args)).strip()
                created.append(module_id)
            print("Virtual audio ready: " + ", ".join(project.audio[k] for k in
                  ("incoming_sink", "microphone", "outgoing_sink")))
        except Exception:
            for module_id in reversed(created):
                project.pactl("unload-module", module_id)
            raise


class Rpc:
    """Short-lived connection to the owning Codex daemon for thread identity only."""
    def __init__(self, project):
        command = project.rpc_command()
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        except BaseException:
            self.errors.close()
            raise
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.pending = b""
        self.sequence = 0

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if b"\n" not in self.pending:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise SetupError("Codex daemon connection closed; run `codex app-server daemon start`")
                self.pending += chunk
                continue
            line, self.pending = self.pending.split(b"\n", 1)
            value = json.loads(line)
            if value.get("id") != request_id:
                continue
            if "error" in value:
                raise SetupError(f"Codex {method} failed: {value['error'].get('message', 'RPC error')}")
            return value["result"]
        raise SetupError("Timed out waiting for Codex " + method)

    def __enter__(self):
        try:
            self.request("initialize", {"clientInfo": {"name": "voip_setup", "version": "0.1.0"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.errors.close()


def operator_init(project, adopt=None):
    with lock(project.state / "ensure.lock"):
        target = project.state / "operator-thread-id"
        if target.exists():
            raise SetupError("An operator thread is already saved; it will not be replaced")
        pending = project.state / "operator-init-pending"
        if not adopt and pending.exists():
            raise SetupError("Prior thread creation has an unknown outcome; inspect Codex threads and use operator adopt UUID")
        if lock_busy(project.state / "operator-process.lock"):
            raise SetupError("Operator is already running")
        with Rpc(project) as rpc:
            if adopt:
                thread = rpc.request("thread/read", {"threadId": str(uuid.UUID(adopt)),
                                                       "includeTurns": False})["thread"]
                if Path(thread["cwd"]).resolve() != project.workspace:
                    raise SetupError("Refusing to adopt a thread with a different working directory")
            else:
                # A timeout/disconnect after thread/start may still have created
                # the thread. Do not silently issue another creation on retry.
                atomic_write(pending, str(time.time()) + "\n")
                thread = rpc.request("thread/start", {"cwd": str(project.workspace),
                    **project.permission_params(),
                    "ephemeral": False, **project.instructions()})["thread"]
            # Persist before secondary operations so a retry cannot create a second thread.
            atomic_write(target, str(uuid.UUID(thread["id"])) + "\n")
            pending.unlink(missing_ok=True)
            if not adopt:
                rpc.request("thread/name/set", {"threadId": thread["id"], "name": "Phone operator: " + project.session_id})
                # 0.154.0 creates the rollout lazily. Requesting a history page
                # persists even an empty thread before this connection closes.
                rpc.request("thread/resume", {"threadId": thread["id"],
                    "excludeTurns": True, "initialTurnsPage": {"limit": 1}, **project.instructions()})
        print("Saved operator thread " + project.thread_id())


def process_identity(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # starttime, field 22 in /proc/PID/stat
    except (OSError, ValueError, IndexError):
        return None


def operator_run(project):
    with lock(project.state / "operator-process.lock"):
        env = project.pulse_env()
        env["PULSE_SOURCE"] = project.audio["microphone"]
        env["PULSE_SINK"] = project.audio["outgoing_sink"]
        # Do not tag the new CLI with this setup agent's inherited session identity.
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        child = subprocess.Popen(project.operator_command(), cwd=project.root, env=env)
        runtime = {"pid": child.pid, "starttime": process_identity(child.pid),
                   "thread_id": project.thread_id(), "wrapper_pid": os.getpid(), "started_at": time.time()}
        def terminate(signum, frame):
            # Leave wait() so the bounded cleanup below also handles a child
            # that ignores termination. SIGHUP from a killed tmux follows this path.
            raise SystemExit(128 + signum)
        previous = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGHUP)}
        try:
            atomic_write(project.state / "operator-process.json", json.dumps(runtime) + "\n")
            return child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            (project.state / "operator-process.json").unlink(missing_ok=True)


def operator_status(project):
    busy = lock_busy(project.state / "operator-process.lock")
    try:
        runtime = json.loads((project.state / "operator-process.json").read_text())
    except (OSError, ValueError):
        runtime = {}
    live = bool(runtime.get("starttime") and
                process_identity(runtime.get("pid")) == runtime["starttime"])
    voice = "text frontend selected"
    if project.operator.get("frontend", "text") == "voice":
        voice = "disconnected"
        try:
            state = json.loads((project.state / "voice.json").read_text())
            if live and state.get("pid") == runtime.get("pid") and state.get("starttime") == runtime.get("starttime"):
                voice = state["state"]
        except (OSError, ValueError, KeyError):
            pass
    return {"supervision_paused": (project.state / "operator-paused").exists(),
            "process_lock_held": busy, "codex_process_alive": live,
            "thread_id": runtime.get("thread_id"),
            "voice": voice}


def operator_ensure(project, *, wait=False):
    with lock(project.state / "ensure.lock", blocking=wait):
        from operator_reset import recover_interrupted_reset
        recover_interrupted_reset(project)
        status = operator_status(project)
        if status["supervision_paused"]:
            print("Operator supervision paused")
            return
        project.operator_command()  # Validate identity/binary before creating tmux.
        session = project.operator["session"]
        listing = project.tmux("list-sessions", "-F", "#{session_name}\t#{session_id}", check=False)
        sessions = [line.rsplit("\t", 1) for line in listing.stdout.splitlines() if "\t" in line]
        session_id = next((sid for name, sid in sessions if name == session), None)
        exists = session_id is not None
        if status["process_lock_held"]:
            if not exists:
                raise SetupError("Operator process survives outside its tmux session; inspect before recovery")
            owner = project.tmux("show-options", "-qv", "-t", session_id, "@voip_root").stdout.strip()
            if owner != str(project.root):
                raise SetupError("Running operator has no matching owned tmux session; inspect before recovery")
            from endpoint_health import voice_health
            health = voice_health(project, status)
            if not health['restart']:
                print("Operator running" if status["codex_process_alive"] else "Operator starting or stopping")
                return
            # ensure.lock serializes the supervisor, and the reset lock excludes
            # explicit keypad recovery. Recheck health before stopping anything.
            with lock(project.state / 'operator-reset.lock'):
                health = voice_health(project)
                if not health['restart']:
                    return
                from operator_reset import stop_frontend
                atomic_write(project.state / 'voice-health-recovery.json', json.dumps({
                    'observed_at': time.time(), 'reason': health['reason'],
                    'thread_id': project.thread_id(), 'scope': 'frontend'})+'\n')
                stop_frontend(project)
            status = operator_status(project)
        if status["codex_process_alive"]:
            raise SetupError("Surviving Codex process has no supervisor lock; inspect before recovery")
        last = project.state / "last-start"
        if last.exists() and time.time() - float(last.read_text()) < project.operator["restart_delay_seconds"]:
            print("Waiting for restart delay")
            return
        if project.operator.get("frontend", "text") == "voice" and project.operator.get("manage_daemon", True) and not project.operator.get("daemon_socket"):
            # Recovery must also work if the owning daemon exited or the host rebooted.
            # This command leaves an already-running daemon and its workers intact.
            run([project.executable(project.operator["codex"]), "app-server", "daemon", "start"])
        if exists:
            owner = project.tmux("show-options", "-qv", "-t", session_id, "@voip_root").stdout.strip()
            if owner != str(project.root):
                raise SetupError("The tmux session name is already in use by another session")
            panes = project.tmux("list-panes", "-s", "-t", session_id,
                                 "-F", "#{pane_id}\t#{pane_dead}").stdout.strip().splitlines()
            if len(panes) != 1 or panes[0].split("\t")[1] != "1":
                raise SetupError("Operator pane is occupied by another live process; inspect it before recovery")
            pane = panes[0].split("\t")[0]
        else:
            session_id, pane = project.tmux("new-session", "-d", "-s", session, "-c", str(project.root),
                                "-P", "-F", "#{session_id}\t#{pane_id}", "/bin/sh").stdout.strip().split("\t")
            project.tmux("set-option", "-t", session_id, "@voip_root", str(project.root))
            project.tmux("set-option", "-w", "-t", pane, "remain-on-exit", "on")
        # Only the fresh, owned shell or a confirmed dead pane is replaced.
        atomic_write(last, str(time.time()) + "\n")
        launcher = shlex.join(project.cli() + ["operator", "run"])
        project.tmux("respawn-pane", "-k", "-t", pane, "-c", str(project.root), "exec " + launcher)
        print("Started persistent Codex operator in " + session + "; frontend=" + project.operator.get("frontend", "text"))


def sip_configure(project, password=None):
    domain, user = project.sip["domain"], project.sip["username"]
    if not re.fullmatch(r"[a-z0-9-]+\.sip\.twilio\.com", domain):
        raise SetupError("Set sip.domain to your SIP Domain, such as name.sip.twilio.com, without a region or edge suffix")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", user):
        raise SetupError("SIP username contains unsupported characters")
    if password is None:
        password = getpass.getpass("Twilio SIP credential password (not the Account Auth Token): ")
    if not password or any(c in password for c in '\r\n";\\'):
        raise SetupError('Use a SIP password without quotes, semicolons, backslashes, or newlines')
    binary = Path(project.executable("baresip"))
    module_path = binary.parent.parent / "lib" / "baresip" / "modules"
    if any(not (module_path / name).exists() for name in ("pulse.so", "ctrl_tcp.so")):
        raise SetupError("Baresip pulse/ctrl_tcp module missing; run scripts/build-baresip.py")
    a = project.audio
    config = f'''# Generated by voip.py; runtime credentials are in accounts.
poll_method epoll
# This Twilio connection uses IPv4. Ignore unrelated IPv6 address changes:
# netroam otherwise resets established SIP transports when those addresses churn.
net_af ipv4
sip_transports tls
filter_registrar tls
sip_verify_server yes
sip_cafile /etc/ssl/certs/ca-certificates.crt
call_max_calls 1
audio_player pulse,{a['incoming_sink']}
audio_source pulse,{a['outgoing_sink']}.monitor
ausrc_channels 1
auplay_channels 1
audio_buffer 20-160
rtp_timeout 60
module_path {module_path}
module g711.so
module auconv.so
module auresamp.so
module pulse.so
module srtp.so
module uuid.so
module_app account.so
module_app menu.so
module_app netroam.so
module_app ctrl_tcp.so
ctrl_tcp_listen 127.0.0.1:{project.sip.get('control_port', 4444)}
ringback_disabled yes
ring_aufile none
callwaiting_aufile none
ringback_aufile none
sip_autoanswer_aufile none
'''
    account = (f'<sip:{user}@{domain};transport=tls>;auth_user={user};auth_pass="{password}";'
               f'outbound="sip:{domain};transport=tls";sipnat=outbound;regint=600;'
               'answermode=auto;sip_autoanswer_beep=off;call_transfer=no;check_origin=yes;'
               'audio_codecs=PCMU/8000/1,PCMA/8000/1;mediaenc=srtp-mand\n')
    atomic_write(project.sip_directory / "config", config)
    atomic_write(project.sip_directory / "accounts", account)
    print("Wrote private Baresip configuration. Enable Secure Media on the Twilio SIP Domain.")


def render_systemd(project):
    directory = project.root / "systemd" / "generated"
    # systemd quoting is not shell quoting; percent is a specifier escape.
    def quote(value):
        return json.dumps(str(value).replace("%", "%%").replace("$", "$$"))
    launcher = " ".join(quote(x) for x in (sys.executable, ROOT / "voip.py", "--root", project.root))
    # User services may not inherit the interactive shell PATH where Codex lives.
    path_environment = json.dumps("PATH=" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")).replace("%", "%%")
    base = ("[Unit]\nAfter=pipewire-pulse.service\nWants=pipewire-pulse.service\n\n[Service]\n"
            + "Environment=" + path_environment + "\n")
    units = {
        "voip-audio.service": base + f"Type=oneshot\nExecStart={launcher} audio up\n",
        "voip-audio.timer": "[Unit]\nDescription=Restore phone virtual audio devices\n\n[Timer]\nOnStartupSec=5\nOnUnitInactiveSec=30\n\n[Install]\nWantedBy=timers.target\n",
        # tmux must survive the short-lived launcher, including when this check
        # started a new tmux server. Stopping this check does not stop the operator.
        "voip-operator.service": base + f"Type=oneshot\nKillMode=process\nExecStart={launcher} operator ensure\n",
        "voip-operator.timer": "[Unit]\nDescription=Recover the persistent phone operator\n\n[Timer]\nOnStartupSec=10\nOnUnitInactiveSec=5\nAccuracySec=1s\n\n[Install]\nWantedBy=timers.target\n",
        "voip-baresip.service": base + f"ExecStartPre={launcher} audio up\nExecStart={launcher} sip run\nRestart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n",
        "voip-call-events.service": "[Unit]\nDescription=Observe local phone call events\nAfter=voip-baresip.service\nWants=voip-baresip.service\n\n[Service]\n"
            + "Environment=" + path_environment + f"\nExecStart={launcher} sip monitor\nRestart=always\nRestartSec=2\n\n[Install]\nWantedBy=default.target\n",
    }
    for filename, content in list(units.items()):
        stem, suffix = filename.rsplit('.', 1)
        units[stem + '@.' + suffix] = content.replace(launcher, launcher + ' --session %i').replace(
            'voip-baresip.service', 'voip-baresip@%i.service')
    worker = " ".join(quote(x) for x in ('/bin/bash', ROOT / 'scripts/run-requests.sh',
                                        sys.executable, ROOT / 'voip.py', project.root))
    units['voip-requests.service'] = ("[Unit]\nDescription=Dispatch voice requests and scheduled calls\n"
        "After=network-online.target\n\n[Service]\nEnvironment=" + path_environment +
        "\nExecStart=" + worker + "\nRestart=always\nRestartSec=3\nTimeoutStopSec=300\n"
        "\n[Install]\nWantedBy=default.target\n")
    units['voip-requests@.service'] = units['voip-requests.service'].replace(
        '\nExecStart=' + worker, '\nExecStart=' + worker + ' %i')
    units['voip-directories.service'] = (base + f'ExecStart={launcher} directories run\n'
        'Restart=on-failure\nRestartSec=10\nTimeoutStopSec=60\n[Install]\nWantedBy=default.target\n')
    for name, content in units.items():
        atomic_write(directory / name, content, 0o644)
    print("Rendered user units in " + str(directory) + "; none have been installed or enabled")


def doctor(project):
    for name in ("python3", "tmux", "pactl", "parec", "pacat", "baresip", project.operator["codex"]):
        try:
            print("OK   " + name + ": " + project.executable(name))
        except SetupError:
            print("NEED " + name)
    try:
        for line in project.pactl("info").splitlines():
            if line.startswith(("Server Name:", "Server Version:")):
                print("OK   " + line)
        print("Audio sources:", ", ".join(x["name"] for x in project.pulse_list("sources")))
        print("Audio sinks:", ", ".join(x["name"] for x in project.pulse_list("sinks")))
    except (SetupError, FileNotFoundError) as error:
        print("NEED audio: " + str(error))
    try:
        print("OK   Saved Codex thread: " + project.thread_id())
    except SetupError as error:
        print("NEED " + str(error))
    print("Voice frontend: " + project.operator.get("frontend", "text") + "; " + operator_status(project)["voice"])
    print("Codex CLI has no built-in voice UI; voice mode uses this project's App Server frontend.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("VOIP_ROOT", str(ROOT))))
    parser.add_argument("--session", default="default", help="Named voice session profile (default: default)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    audio = commands.add_parser("audio").add_subparsers(dest="action", required=True)
    audio.add_parser("up")
    operator = commands.add_parser("operator").add_subparsers(dest="action", required=True)
    for action in ("init", "ensure", "run", "status", "health", "pause", "unpause"):
        operator.add_parser(action)
    operator.add_parser("adopt").add_argument("thread")
    operator.add_parser("reset", help="Recover voice, reload the operator, or start a fresh conversation").add_argument(
        "--scope", choices=("frontend", "session", "fresh"), default="frontend")
    sip = commands.add_parser("sip").add_subparsers(dest="action", required=True)
    sip.add_parser("configure")
    sip.add_parser("run")
    sip.add_parser("monitor")
    calls = commands.add_parser("call").add_subparsers(dest="action", required=True)
    start = calls.add_parser("start", help="Call an allowed recipient and connect the saved operator")
    start.add_argument("--request-key", required=True, help="Stable key; retries never create a second call")
    start.add_argument("--reason", required=True, help="Local purpose supplied to preparation; never sent to Twilio")
    start.add_argument("--to", help="Allowed E.164 recipient; defaults to the only configured caller")
    start.add_argument("--message", action="append", help="Agenda message ID; repeat to select several (default: pending messages)")
    start.add_argument("--session", dest="call_session")
    start.add_argument("--recipient", help="Named recipient from recipients.toml")
    start.add_argument("--mode", choices=("preemptive", "non_preemptive"), default="non_preemptive")
    start.add_argument("--ttl-seconds", type=int, help="Request lifetime; explicit delay lists automatically extend the default")
    start.add_argument("--max-attempts", type=int)
    start.add_argument("--retry-delay-seconds", type=int)
    start.add_argument("--retry-delays", help="Delays after failures, e.g. 1m,5m,8m,60m")
    prepare = calls.add_parser("prepare", help="Prepare the existing operator without placing a phone call")
    prepare.add_argument("--request-key", required=True)
    prepare.add_argument("--reason", required=True)
    prepare.add_argument("--message", action="append")
    prepare.add_argument("--session", dest="call_session")
    prepare.add_argument('--recipient')
    prepare.add_argument("--brief-file", type=Path, help="Operator-only: already prepared JSON opening/context; avoids waiting on its own turn")
    calls.add_parser('current')
    calls.add_parser('history').add_argument('--recipient')
    transfer = calls.add_parser('transfer', help='Move an identified caller to another allowed session')
    transfer.add_argument('--call-id', required=True)
    destination = transfer.add_mutually_exclusive_group(required=True)
    destination.add_argument('--to-session')
    destination.add_argument('--to-home', action='store_true', help='Resolve the configured home directory operator')
    transfer.add_argument('--request-key', required=True)
    transfer.add_argument('--note', default='')
    calls.add_parser('transfer-status').add_argument('request_key')
    calls.add_parser('hangup').add_argument('--call-id', required=True)
    status = calls.add_parser("status")
    status.add_argument("request_key", nargs="?")
    status.add_argument("--local", action="store_true", help="Read saved state without querying Twilio")
    for action in ("cancel", "reconcile"):
        calls.add_parser(action).add_argument("request_key")
    mailbox_parser = commands.add_parser("mailbox", help="Messages for one recipient and project")
    mailbox_parser.add_argument('--recipient', help='Recipient identity; defaults to the live caller, then project default')
    mailbox_parser.add_argument('--to', help='Configured recipient phone number instead of identity')
    mailbox = mailbox_parser.add_subparsers(dest="action", required=True)
    post = mailbox.add_parser("post")
    post.add_argument("--from", dest="sender", required=True)
    post.add_argument("--subject", required=True)
    post.add_argument("--key", help="Stable message ID; identical retries do not duplicate messages")
    post.add_argument("--session", dest="call_session")
    post.add_argument("--notify", choices=("passive", "preemptive", "non_preemptive"), default="non_preemptive")
    body = post.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--file", help="UTF-8 text file, or '-' to read stdin")
    mailbox.add_parser("list").add_argument("--all", action="store_true")
    mailbox.add_parser("count")
    mailbox.add_parser("read").add_argument("key")
    address = mailbox.add_parser("address")
    address.add_argument("key")
    address.add_argument("--note", default="")
    mailbox.add_parser("reopen").add_argument("key")
    mailbox.add_parser('migrate', help='Assign this session’s legacy mailbox to an explicitly selected recipient')
    for subparser in mailbox.choices.values():
        subparser.add_argument('--recipient', default=argparse.SUPPRESS)
        subparser.add_argument('--to', default=argparse.SUPPRESS)
    commands.add_parser("render-systemd")
    requests = commands.add_parser("request").add_subparsers(dest="action", required=True)
    for kind in ("message", "call"):
        request = requests.add_parser(kind)
        request.add_argument("--request-key", required=True)
        request.add_argument("--session", dest="call_session")
        request.add_argument("--reason", required=True)
        request.add_argument("--from", dest="sender", default="local-worker")
        request.add_argument("--message", action="append")
        request.add_argument("--file", type=Path, help="Post this agenda as a mailbox message and enqueue together")
        request.add_argument("--recipient")
        request.add_argument("--to")
        request.add_argument("--mode", choices=("preemptive", "non_preemptive"), default="non_preemptive")
        request.add_argument("--ttl-seconds", type=int, help="Request lifetime; explicit delay lists automatically extend the default")
        request.add_argument("--max-attempts", type=int)
        request.add_argument("--retry-delay-seconds", type=int)
        request.add_argument("--retry-delays", help="Delays after failures, e.g. 1m,5m,8m,60m")
    requests.add_parser("status").add_argument("key", nargs="?")
    requests.add_parser("cancel").add_argument("key")
    resume = requests.add_parser("resume", help="Resume after inspecting an ambiguous submission")
    resume.add_argument("key")
    resume.add_argument("--confirm-no-prompt", action="store_true")
    requests.add_parser("run", help="Run the dispatcher and cron scheduler")
    schedule = commands.add_parser("schedule").add_subparsers(dest="action", required=True)
    schedule.add_parser("list")
    schedule.add_parser("next").add_argument("--count", type=int, default=5)
    schedule.add_parser("tick").add_argument("--dry-run", action="store_true")
    for action in ('agenda', 'skip', 'start-early'):
        occurrence = schedule.add_parser(action, help='Inspect or change one occurrence without changing its recurring schedule')
        occurrence.add_argument('occurrence_key', help='Copy occurrence_key from schedule next')
        occurrence.add_argument('--recipient', required=True)
        if action != 'agenda':
            occurrence.add_argument('--note', required=True, help='Caller-requested reason for this change')
        if action == 'start-early':
            occurrence.add_argument('--call-id', required=True, help='Current authenticated call in this session')
    configuration = commands.add_parser("config").add_subparsers(dest="action", required=True)
    configuration.add_parser("check")
    configuration.add_parser("apply", help="Deploy phone routes and install the configured session services")
    pins = commands.add_parser('pin', help='Generate, migrate, or deliver protected recipient PINs').add_subparsers(dest='action', required=True)
    pins.add_parser('migrate', help='Replace plaintext PIN files/config with verifiers, preserving existing PINs')
    generate = pins.add_parser('generate', help='Rotate a recipient PIN and print it once or queue phone delivery')
    generate.add_argument('recipient')
    generate.add_argument('--delivery', choices=('print', 'call'), default='print')
    generate.add_argument('--request-key', help='Required stable key for phone delivery')
    generate.add_argument('--digits', type=int, default=8)
    generate.add_argument('--retry-delays', default='1m,5m,8m,60m', help='Delays after failures; last delay repeats until confirmed or cancelled')
    pins.add_parser('status').add_argument('key', nargs='?')
    pins.add_parser('cancel').add_argument('key')
    reconcile = pins.add_parser('reconcile', help='Bind a verified provider call to an ambiguous PIN delivery attempt')
    reconcile.add_argument('key')
    reconcile.add_argument('--call-sid', required=True)
    args = parser.parse_args()
    project = Project(args.root, getattr(args, "call_session", None) or args.session)
    if args.command == 'pin':
        from pin_delivery import Deliveries
        from pin_security import migrate
        from voice_config import parse_retry_delays
        deliveries = Deliveries(project)
        if args.action == 'migrate':
            result = migrate(project)
        elif args.action == 'generate':
            result = deliveries.generate(args.recipient, args.delivery, args.request_key,
                                         parse_retry_delays(args.retry_delays), args.digits)
            print('Run python3 voip.py config apply to activate this PIN. Phone delivery waits for deployment.', file=sys.stderr)
            if args.delivery == 'print':
                print(result)  # Sole intentional plaintext output; never stored or logged by this application.
                return
        elif args.action == 'cancel':
            result = deliveries.cancel(args.key)
        elif args.action == 'reconcile':
            result = deliveries.reconcile(args.key, args.call_sid)
        else:
            result = deliveries.status(args.key)
        print(json.dumps(result, indent=2))
    elif args.command == "config":
        from service_config import check_config, apply_config
        print(json.dumps((apply_config if args.action == "apply" else check_config)(project), indent=2))
    elif args.command == "schedule":
        from call_schedule import Scheduler
        scheduler = Scheduler(project)
        if args.action == "list":
            result = [{k: v for k, v in s.items() if not k.startswith('_')} for s in scheduler.definitions()]
        elif args.action == "next":
            result = scheduler.next(args.count)
        elif args.action in {'agenda', 'skip', 'start-early'}:
            from schedule_occurrences import Occurrences
            occurrences = Occurrences(project)
            if args.action == 'agenda':
                result = occurrences.agenda(args.occurrence_key, args.recipient)
            else:
                result = occurrences.change(args.occurrence_key, args.recipient, args.note,
                    args.call_id if args.action == 'start-early' else None)
        else:
            result = scheduler.tick(args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.command == "request":
        from requests_queue import Requests, Dispatcher
        queue = Requests(project)
        if args.action in ("message", "call"):
            from operator_mailbox import Mailbox
            ids = args.message
            if args.file:
                message = Mailbox(project, args.recipient, args.to).post(args.sender, args.reason[:240], args.file.read_text(), args.request_key)
                ids = list(dict.fromkeys([*(ids or []), message['id']]))
            result = queue.enqueue(args.request_key, args.action, args.reason, ids, args.recipient, args.to,
                args.mode, args.ttl_seconds, cli_redial(args))
        elif args.action == "status":
            result = queue.read(args.key) if args.key else list(queue.load().values())
        elif args.action == "cancel":
            result = queue.cancel(args.key)
        elif args.action == "resume":
            result = queue.resume(args.key, args.confirm_no_prompt)
        else:
            import threading
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            Dispatcher(project).run(stop)
            return
        print(json.dumps(result, indent=2))
    elif args.command == "doctor":
        doctor(project)
    elif args.command == "audio":
        audio_up(project)
    elif args.command == "render-systemd":
        render_systemd(project)
    elif args.command == "mailbox":
        from operator_mailbox import Mailbox, render
        identity = args.recipient
        if args.action == 'migrate' and not identity and not args.to:
            raise SetupError('Legacy migration requires an explicit --recipient or --to; inspect the old audience first')
        if not identity and not args.to:
            from call_events import CallEvents
            calls = CallEvents(project).load()
            active = [c for c in calls['calls'].values() if c['connected']]
            if active:
                if (not calls['observer_online'] or time.time() - calls['observed_at'] > 10 or
                        len(active) != 1 or not active[0].get('recipient_id')):
                    raise SetupError('Cannot identify the current caller; select --recipient explicitly')
                identity = active[0]['recipient_id']
        mailbox = Mailbox(project, identity, args.to)
        if args.action == 'migrate':
            print(json.dumps(mailbox.migrate(), indent=2))
        elif args.action == "post":
            body = args.body if args.body is not None else (sys.stdin.read(65537) if args.file == "-" else Path(args.file).read_text())
            message = mailbox.post(args.sender, args.subject, body, args.key)
            if args.notify != "passive" and message['status'] == 'unaddressed':
                from requests_queue import Requests
                import hashlib
                request_key = "message-" + hashlib.sha256((project.session_id + ":" + mailbox.recipient['id'] + ':' + message['id']).encode()).hexdigest()[:32]
                Requests(project).enqueue(request_key, "message", args.subject, [message['id']],
                                          recipient=mailbox.recipient['id'], mode=args.notify)
            print(json.dumps(message) if JSON_OUTPUT else message["id"])
        elif args.action == "list":
            messages = mailbox.list(args.all)
            if JSON_OUTPUT:
                print(json.dumps(messages))
            else:
                for message in messages:
                    print(f"{message['id']}\t{message['status']}\t{message['sender']}\t{message['subject']}")
                if not messages:
                    print("No messages." if args.all else "No unaddressed messages.")
        elif args.action == "count":
            print(mailbox.count())
        elif args.action == "read":
            print(json.dumps(mailbox.read(args.key)) if JSON_OUTPUT else render(mailbox.read(args.key)), end="\n" if JSON_OUTPUT else "")
        else:
            message = mailbox.address(args.key, getattr(args, "note", ""), reopen=args.action == "reopen")
            print(json.dumps(message) if JSON_OUTPUT else message["id"] + " " + message["status"])
    elif args.command == "call":
        if args.action == 'current':
            from call_events import CallEvents
            state=CallEvents(project).load()
            print(json.dumps({'observed_at':state.get('observed_at'), 'observer_online':state.get('observer_online'),
                'calls':[{'call_id':key,**value} for key,value in state['calls'].items() if value['connected']]},indent=2))
            return
        if args.action == 'history':
            from call_history import History
            print(json.dumps(History(project).list(project.session_id,args.recipient),indent=2))
            return
        if args.action in {'transfer','transfer-status','hangup'}:
            from call_control import Control
            control=Control(project)
            if args.action=='transfer':
                from directory_service import home_target
                target = home_target(project) if args.to_home else args.to_session
                result=control.transfer(args.call_id,target,args.request_key,args.note)
            elif args.action=='transfer-status': result=control.status(args.request_key)
            else: result=control.hangup(args.call_id)
            print(json.dumps(result,indent=2))
            return
        if args.action == "prepare":
            from call_preparation import Preparations
            brief = json.loads(args.brief_file.read_text()) if args.brief_file else None
            result = Preparations(project).prepare(args.request_key, args.reason, args.message, brief, recipient=args.recipient)
            print(json.dumps(result, indent=2))
            return
        from requests_queue import Requests
        queue = Requests(project)
        if args.action == "start":
            result = queue.enqueue(args.request_key, "call", args.reason, args.message, args.recipient,
                                   args.to, args.mode, args.ttl_seconds, cli_redial(args))
        elif args.request_key and args.request_key in queue.load() and args.action in ("status", "cancel"):
            result = queue.read(args.request_key) if args.action == "status" else queue.cancel(args.request_key)
        else:
            from outgoing_calls import Calls
            calls = Calls(project)
            if args.action == "status":
                result = calls.status(args.request_key, local=args.local)
            else:
                result = getattr(calls, args.action)(args.request_key)
        print(json.dumps(result, indent=2))
    elif args.command == "sip":
        if args.action == "configure":
            sip_configure(project)
        elif args.action == "monitor":
            import asyncio
            from call_events import monitor
            with lock(project.state / "sip-monitor.lock"):
                asyncio.run(monitor(project))
        else:
            if not (project.sip_directory / "accounts").is_file():
                raise SetupError("Run sip configure first")
            binary = project.executable("baresip")
            os.execve(binary, [binary, "-f", str(project.sip_directory)], project.pulse_env())
    elif args.action in ("init", "adopt"):
        operator_init(project, getattr(args, "thread", None))
    elif args.action == "run":
        return operator_run(project)
    elif args.action == "ensure":
        operator_ensure(project)
    elif args.action == "reset":
        from operator_reset import reset_operator
        print(json.dumps(reset_operator(project, args.scope), indent=2))
    elif args.action == "status":
        print(json.dumps(operator_status(project), indent=2))
    elif args.action == "health":
        from endpoint_health import summary
        print(json.dumps(summary(project), indent=2))
    elif args.action == "pause":
        atomic_write(project.state / "operator-paused", "paused\n")
        print("Automatic recovery paused; running Codex and workers remain running")
    elif args.action == "unpause":
        (project.state / "operator-paused").unlink(missing_ok=True)
        operator_ensure(project)
    return 0


def cli_redial(args):
    values = {}
    if args.max_attempts is not None:
        values["max_attempts"] = args.max_attempts
    if args.retry_delay_seconds is not None:
        values["delay_seconds"] = args.retry_delay_seconds
    if getattr(args, "retry_delays", None) is not None:
        if args.retry_delay_seconds is not None:
            raise SetupError("Choose --retry-delays or --retry-delay-seconds")
        from voice_config import parse_retry_delays
        values["delays_seconds"] = parse_retry_delays(args.retry_delays)
    return values


if __name__ == "__main__":
    # Helpers import voip; share these exception types and globals when the
    # launcher itself was loaded as __main__.
    sys.modules.setdefault("voip", sys.modules[__name__])
    try:
        from release_cli import entry
        sys.exit(entry())
    except (SetupError, FileNotFoundError, ValueError, subprocess.TimeoutExpired) as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
