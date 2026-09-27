"""Portable installation configuration; never serialize credential values."""
import json
import os
from pathlib import Path
import shlex

from voip import ROOT, SetupError, atomic_write

FIELDS = {
    'audio': {'incoming_sink', 'microphone', 'outgoing_sink', 'rate'},
    'operator': {'session', 'tmux_socket', 'codex', 'daemon_socket', 'frontend',
                 'restart_delay_seconds', 'extra_args', 'permissions', 'manage_daemon',
                 'state_database', 'cli_command', 'working_directory'},
    'sip': {'domain', 'username', 'control_port'},
    'twilio': {'phone_number'},
    'service': {'recipients_file', 'sessions_dir', 'schedules_file', 'projects',
                'provider_root', 'session_ids', 'credentials_file'},
    'directory_extensions': {'enabled', 'root', 'recipients', 'mode', 'tmux_socket', 'interval_seconds', 'snapshot_file'},
}

def validate_server(config, path):
    unknown = set(config) - set(FIELDS)
    if unknown:
        raise SetupError(f'{path}: unknown section(s): ' + ', '.join(sorted(unknown)))
    for section, keys in FIELDS.items():
        values = config.get(section, {})
        if not isinstance(values, dict):
            raise SetupError(f'{path}: [{section}] must be a table')
        unknown = set(values) - keys
        if unknown:
            raise SetupError(f'{path}: unknown {section} field(s): ' + ', '.join(sorted(unknown)))
    operator = config.get('operator', {})
    if operator.get('permissions', 'read-only') not in {'read-only', 'workspace-write', 'danger-full-access'}:
        raise SetupError(f'{path}: operator.permissions must be read-only, workspace-write, or danger-full-access')
    if type(operator.get('manage_daemon', True)) is not bool:
        raise SetupError(f'{path}: operator.manage_daemon must be boolean')
    command = operator.get('cli_command', [])
    if 'working_directory' in operator and (not isinstance(operator['working_directory'], str) or not Path(operator['working_directory']).is_absolute()):
        raise SetupError(f'{path}: operator.working_directory must be an absolute path')
    if not isinstance(command, list) or any(not isinstance(x, str) or not x for x in command):
        raise SetupError(f'{path}: operator.cli_command must be an array of command arguments')
    forbidden = {'--yolo', '--dangerously-bypass-approvals-and-sandbox', '--sandbox', '-s', '--ask-for-approval', '-a'}
    if any(x.split('=')[0] in forbidden for x in operator.get('extra_args', []) if isinstance(x, str)):
        raise SetupError(f'{path}: configure permissions through operator.permissions, not extra_args')

def load_credentials(project):
    value = project.config.get('service', {}).get('credentials_file')
    if not value:
        if project.provider_root != project.root:
            from voip import Project
            return load_credentials(Project(project.provider_root))
        return
    path = (project.root / value).resolve()
    if not path.is_file():
        raise SetupError('Configured credentials file is missing')
    if path.stat().st_mode & 0o077:
        raise SetupError('Credentials file must be private: chmod 600 FILE')
    allowed = {'TWILIO_SID', 'TWILIO_SECRET', 'TWILIO_API_KEY_SID', 'TWILIO_API_KEY_SECRET'}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in allowed or '\x00' in value:
            raise SetupError('Invalid credentials file entry; use documented KEY=value lines')
        # No shell execution or interpolation. Explicit process environment wins.
        os.environ.setdefault(key, value)

def initialize(root, number, recipient, destination, socket=None):
    from voice_config import name, NUMBER
    name(recipient)
    if not NUMBER.fullmatch(number) or not NUMBER.fullmatch(destination):
        raise SetupError('Use E.164 server and recipient phone numbers')
    root = Path(root).resolve()
    targets = ['server.toml', 'recipients.toml', 'sessions/default.toml', 'schedules.toml']
    if any((root / f).exists() for f in targets):
        raise SetupError('Initialization refuses to overwrite an existing configuration')
    server = (ROOT / 'server.toml.example').read_text().replace('+12025550100', number)
    if socket:
        server = server.replace('daemon_socket = ""', 'daemon_socket = ' + json.dumps(str(Path(socket).resolve())))
        server = server.replace('manage_daemon = true', 'manage_daemon = false')
    atomic_write(root / 'server.toml', server)
    atomic_write(root / 'recipients.toml', f'default_recipient = {json.dumps(recipient)}\n\n[recipients.{recipient}]\n'
        f'number = {json.dumps(destination)}\npin_file = "state/{recipient}-pin"\nsessions = ["default"]\nincoming_session = "default"\n')
    atomic_write(root / 'sessions/default.toml', (ROOT / 'sessions/default.toml.example').read_text())
    atomic_write(root / 'schedules.toml', '# No schedules enabled. See schedules.toml.example.\n')
    return {'root': str(root), 'created': targets, 'next': 'Generate a recipient PIN, check configuration, then deploy explicitly.'}

def effective(project):
    from voice_config import sessions
    return {'root': str(project.root), 'provider_root': str(project.provider_root),
            'server_file': str(project.server_path), 'session': project.session_id,
            'profile_file': str(project.root / project.config.get('service', {}).get('sessions_dir', 'sessions') / (project.session_id + '.toml')),
            'state': str(project.state), 'audio': project.audio,
            'operator': {**project.operator, 'permissions': project.permissions()},
            'sip': project.sip, 'profile': project.profile, 'sessions': sessions(project),
            'inheritance': ['built-in defaults', 'provider audio/operator/sip', 'project server', 'session profile'],
            'credentials': 'process environment or configured private file; values omitted'}

def plan(project, hosted_only=False):
    from service_config import check_config
    return {**check_config(project), 'hosted_only': hosted_only,
            'effects': ['Deploy protected Twilio handlers and private recipient verifiers',
                        'Verify and update the configured existing phone number route'] +
                       ([] if hosted_only else ['Install/enable user services', 'Restart SIP observers and reconnect voice frontends']),
            'calls': 'No direct dial; enabling a dispatcher can release queued or scheduled calls',
            'active_calls': 'Full apply refuses connected calls; use hosted-only for handler changes'}
