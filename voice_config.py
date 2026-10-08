"""Server, recipient, and session configuration. Secret values never enter status output."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib

from voip import SetupError

NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
NUMBER = re.compile(r"\+[1-9]\d{7,14}")
PROMPTS = {"initial", "connection", "disconnection", "preparation", "request"}


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise SetupError("Names must start with a lowercase letter and contain at most 32 lowercase letters, digits, '_' or '-'")
    return value


def read_toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        raise SetupError("Cannot read valid TOML configuration: " + str(path)) from None


def configured_path(project, setting, default):
    value = project.config.get("service", {}).get(setting, default)
    if not isinstance(value, str) or not value:
        raise SetupError("Invalid service configuration path: " + setting)
    return (project.root / value).resolve()


def session_profile(root, config, session_id):
    name(session_id)
    directory = Path(root) / config.get("service", {}).get("sessions_dir", "sessions")
    path = directory / (session_id + ".toml")
    if not path.exists() and session_id == "default":
        values = {}
    else:
        values = read_toml(path)
    allowed = {"reset_on_outgoing_call", "control_port", "prompts", "redial", "extension", "permissions"}
    if set(values) - allowed:
        raise SetupError("Unknown session settings in " + str(path))
    if values.get("permissions") not in (None, "read-only", "workspace-write", "danger-full-access"):
        raise SetupError("Invalid session permissions in " + str(path))
    reset = values.get("reset_on_outgoing_call", False)
    if type(reset) is not bool:
        raise SetupError("reset_on_outgoing_call must be true or false")
    extension = values.get('extension')
    if extension is not None and (not isinstance(extension, str) or not re.fullmatch(r'[1-9][0-9]{0,4}', extension)):
        raise SetupError('Session extension must be a quoted string of 1–5 digits, starting with 1–9 (shorter than every PIN)')
    prompts = values.get("prompts", {})
    if not isinstance(prompts, dict) or set(prompts) - (PROMPTS | {p + "_file" for p in PROMPTS}):
        raise SetupError("Unsupported session prompt setting")
    loaded = {}
    for key in PROMPTS:
        if key in prompts and key + "_file" in prompts:
            raise SetupError("Choose inline text or a file for each prompt, not both")
        value = prompts.get(key, "")
        if key + "_file" in prompts:
            filename = prompts[key + "_file"]
            if not isinstance(filename, str):
                raise SetupError("Prompt file paths must be strings")
            try:
                value = (path.parent / filename).read_text()
            except OSError:
                raise SetupError("Cannot read session prompt file for " + key) from None
        if not isinstance(value, str) or len(value) > 64000 or "\x00" in value:
            raise SetupError("Session prompts must be text of at most 64000 characters")
        placeholders = {'default', 'session_id', 'mailbox_command'}
        if key in {'connection', 'disconnection'}:
            placeholders |= {'call_id', 'recipient_id', 'mailbox_count', 'prepared_opening'}
        if key == 'request':
            placeholders |= {'request_key', 'mode'}
        if set(re.findall(r'\{\{([a-z_]+)\}\}', value)) - placeholders:
            raise SetupError('Unknown placeholder in ' + key + ' prompt')
        loaded[key] = value
    port = values.get("control_port", 10000 + int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 45000)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise SetupError("Session control_port must be between 1024 and 65535")
    return {"id": session_id, "extension": extension, "permissions": values.get("permissions"), "reset_on_outgoing_call": reset, "control_port": port,
            "prompts": loaded, "redial": redial_policy(values.get("redial", {}))}


def registered_projects(project):
    values = project.config.get('service', {}).get('projects', {})
    if not isinstance(values, dict):
        raise SetupError('service.projects must map session names to project directories')
    result, directories = {}, set()
    local_dir = project.root / project.config.get('service', {}).get('sessions_dir', 'sessions')
    local_sessions = {'default'} | {p.stem for p in local_dir.glob('*.toml')}
    for identity, path in values.items():
        name(identity)
        if identity == 'default' or not isinstance(path, str) or not path:
            raise SetupError('External projects require a named session and directory')
        directory = (project.root / path).resolve()
        if directory == project.root or not directory.is_dir() or directory in directories:
            raise SetupError('Registered project directory must exist and differ from the provider directory')
        directories.add(directory)
        # One level keeps registrations inspectable and prevents cycles.
        config = read_toml(directory / 'server.toml')
        if config.get('service', {}).get('projects'):
            raise SetupError('Nested project registration is not supported')
        service = config.get('service', {})
        if (directory / service.get('provider_root', '.')).resolve() != project.root:
            raise SetupError('Registered project must reference this provider_root')
        identities = service.get('session_ids')
        if not isinstance(identities, list) or not identities:
            raise SetupError('Registered project must select its session_ids')
        for session_id in identities:
            name(session_id)
            if session_id in result or session_id in local_sessions:
                raise SetupError('Session names must be unique across registered projects and provider profiles')
            result[session_id] = directory
    return result


def sessions(project):
    directory = configured_path(project, "sessions_dir", "sessions")
    configured = project.config.get('service', {}).get('session_ids')
    if configured is not None and (not isinstance(configured, list) or not configured):
        raise SetupError('service.session_ids must be a nonempty list')
    identities = (set(configured) if configured is not None else
                  {"default"} | {p.stem for p in directory.glob("*.toml")})
    identities |= set(registered_projects(project))
    return sorted(name(i) for i in identities)


def redial_policy(overrides=None, defaults=None):
    policy = {"max_attempts": 1, "delay_seconds": 300, "backoff_multiplier": 1,
              "max_delay_seconds": 3600, "retry_on": ["busy", "no-answer"], "delays_seconds": []}
    if defaults:
        policy.update(defaults)
    changes = overrides or {}
    if not isinstance(changes, dict) or set(changes) - set(policy):
        raise SetupError("Unknown redial setting")
    policy.update(changes)
    delays = policy["delays_seconds"]
    if (not isinstance(delays, list) or len(delays) > 1000 or
            any(type(d) is not int or not 1 <= d <= 31536000 for d in delays)):
        raise SetupError("redial.delays_seconds must contain up to 1000 positive delays in seconds (at most one year each)")
    # A new explicit schedule defines its attempt count unless the caller also
    # supplies a cap. The final delay repeats when an explicit cap is larger.
    if "delays_seconds" in changes and delays and "max_attempts" not in changes:
        policy["max_attempts"] = len(delays) + 1
    for key, low, high in (("max_attempts", 1, 10000), ("delay_seconds", 1, 86400),
                           ("backoff_multiplier", 1, 10), ("max_delay_seconds", 1, 86400)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise SetupError("Invalid redial setting: " + key)
    if (not isinstance(policy["retry_on"], list) or
            any(x not in {"busy", "no-answer", "failed", "ended_without_sip_connection"} for x in policy["retry_on"])):
        raise SetupError("Unsupported redial outcome")
    return policy


def retry_delay(policy, failures):
    """Seconds after this failed attempt; never an offset from initial dialing."""
    if failures < 1:
        raise SetupError("Retry delay requires a failed attempt")
    if policy.get("delays_seconds"):
        return policy["delays_seconds"][min(failures, len(policy["delays_seconds"])) - 1]
    # Bound the exponent even for indefinitely retried PIN delivery.
    return min(policy["max_delay_seconds"], policy["delay_seconds"] *
               policy["backoff_multiplier"] ** min(failures - 1, 32))


def request_lifetime(policy):
    # Keep the old one-hour default for fixed/backoff policies. An explicit list
    # should not be silently cut short by that default. Allow ten minutes per
    # attempt for preparation/ringing; callers can choose a longer/shorter TTL.
    if not policy.get('delays_seconds'):
        return 3600
    return max(3600, sum(retry_delay(policy, n) for n in range(1, policy['max_attempts'])) +
               600 * policy['max_attempts'])


def parse_retry_delays(value):
    result = []
    for part in value.split(','):
        match = re.fullmatch(r"\s*([0-9]+)\s*([smhd]?)\s*", part)
        if not match:
            raise SetupError("Use comma-separated delays such as 1m,5m,8m,60m")
        result.append(int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]])
    return redial_policy({"delays_seconds": result})["delays_seconds"]


def recipients(project, secrets=False, allow_missing=None, include_projects=True):
    path = configured_path(project, "recipients_file", "recipients.toml")
    external = project.provider_root != project.root
    if not path.exists() and external:
        records, default = {}, None
    elif not path.exists():
        # Compatibility for existing installations until their first config migration.
        old = json.loads((project.shared_state / "twilio-call-settings.json").read_text())
        records = {"default" if len(old["allowed_callers"]) == 1 else "recipient_" + str(i):
                   {"number": number, "pin_file": "state/call-pin", "sessions": ["default"], "incoming_session": "default"}
                   for i, number in enumerate(old["allowed_callers"])}
        default = next(iter(records)) if len(records) == 1 else None
    else:
        data = read_toml(path)
        if set(data) - {"default_recipient", "recipients"}:
            raise SetupError("Unknown recipient configuration setting")
        records, default = data.get("recipients", {}), data.get("default_recipient")
    if not isinstance(records, dict) or (not records and not external):
        raise SetupError("Configure at least one recipient")
    result, numbers, pins = {}, set(), set()
    if external:
        from voip import Project
        known_sessions = set(sessions(Project(project.provider_root)))
    else:
        known_sessions = set(sessions(project))
    for identity, data in records.items():
        name(identity)
        if not isinstance(data, dict) or set(data) - {"number", "pin", "pin_file", "sessions", "incoming_session"}:
            raise SetupError("Unknown recipient fields")
        number = data.get("number")
        if not isinstance(number, str) or not NUMBER.fullmatch(number) or number in numbers:
            raise SetupError("Each recipient must have a unique E.164 phone number")
        numbers.add(number)
        allowed = data.get("sessions", ["default"])
        incoming = data.get("incoming_session", "default")
        if (not isinstance(allowed, list) or not allowed or any(not isinstance(s, str) or s not in known_sessions for s in allowed)
                or incoming not in allowed):
            raise SetupError("Recipient sessions and incoming_session must refer to configured, allowed sessions")
        item = {"id": identity, "number": number, "sessions": allowed, "incoming_session": incoming}
        if ("pin" in data) == ("pin_file" in data):
            raise SetupError("Each recipient needs exactly one pin or pin_file")
        if secrets:
            try:
                pin = data["pin"] if "pin" in data else (project.root / data["pin_file"]).read_text().strip()
            except FileNotFoundError:
                if identity != allow_missing:
                    raise SetupError('Cannot read a recipient PIN file') from None
                result[identity] = item
                continue
            except (OSError, TypeError):
                raise SetupError("Cannot read a recipient PIN file") from None
            if isinstance(pin, str) and pin.startswith('{'):
                from pin_security import valid_auth, master_key
                try:
                    auth = valid_auth(json.loads(pin))
                except ValueError:
                    raise SetupError('Invalid PIN verifier file') from None
                master_key(project)
                if auth['tag'] in pins:
                    raise SetupError('Use a different PIN for each recipient')
                pins.add(auth['tag'])
                item['pin_auth'] = auth
                result[identity] = item
                continue
            if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{6,12}", pin):
                raise SetupError("Each recipient PIN must be a string of 6–12 digits")
            if path.exists() and pin in pins:
                raise SetupError("Use a different PIN for each recipient")
            pins.add(pin)
            item["pin"] = pin
        result[identity] = item
    if default is None and len(result) == 1:
        default = next(iter(result))
    if default is not None and default not in result and not external:
        raise SetupError("default_recipient does not exist")
    if include_projects:
        from voip import Project
        visited = set()
        for session_id, directory in registered_projects(project).items():
            if directory in visited:
                continue
            visited.add(directory)
            child = Project(project.root, session_id)
            extra, _ = recipients(child, secrets, allow_missing, include_projects=False)
            for identity, person in extra.items():
                if identity in result or person['number'] in numbers:
                    raise SetupError('Recipient names and numbers must be unique across registered projects')
                result[identity] = person
                numbers.add(person['number'])
    return result, default


def resolve_recipient(project, identity=None, number=None):
    records, default = session_recipients(project)
    if identity and number:
        raise SetupError("Choose --recipient or --to, not both")
    selected = records.get(identity or default) if number is None else next(
        (r for r in records.values() if r["number"] == number), None)
    if not selected or project.session_id not in selected["sessions"]:
        raise SetupError("Choose a configured recipient allowed to access this session")
    return selected


def session_recipients(project):
    """Resolve shared identities without copying their credentials into a project."""
    records, default = recipients(project)
    if project.provider_root != project.root:
        from voip import Project
        records, _ = recipients(Project(project.provider_root))
    from directory_service import is_generated, settings
    if is_generated(project):
        cfg = settings(Project(project.provider_root))
        records = {k: {**v, 'sessions': [*v['sessions'], project.session_id]}
                   for k, v in records.items() if cfg['enabled'] and k in cfg['recipients']}
    allowed = {k: v for k, v in records.items() if project.session_id in v['sessions']}
    if default not in allowed:
        default = next(iter(allowed)) if len(allowed) == 1 else None
    return allowed, default


def operator_thread_name(project):
    """A stable person/workspace label; never derive ownership from the last caller."""
    from voip import Project
    from directory_service import is_generated
    provider = project if project.provider_root == project.root else Project(project.provider_root)
    # Audio-only/bootstrap installations can create an operator before contacts.
    configured = any(configured_path(p, 'recipients_file', 'recipients.toml').exists()
                     or (p.shared_state / 'twilio-call-settings.json').exists()
                     for p in (provider, project))
    people, default = session_recipients(project) if configured else ({}, None)
    identities = [default] if default else sorted(people)
    user = ', '.join(identity.replace('_', ' ').replace('-', ' ').title()
                     for identity in identities) or 'Unassigned'
    # Use the provider's configured directory root, not the container user's HOME.
    root = provider.config.get('directory_extensions', {}).get('root')
    workspace = project.workspace
    directory = workspace.name or str(workspace)
    if root:
        try:
            relative = workspace.relative_to(Path(root).expanduser().resolve())
            directory = 'home' if relative == Path('.') else str(relative)
        except ValueError:
            directory = str(workspace)
    if (project.session_id not in {'default', workspace.name}
            and not is_generated(project)):
        directory += ' (' + project.session_id + ')'
    return 'Phone operator: ' + user + ' — ' + directory


def phone_number(project):
    value = project.config.get("twilio", {}).get("phone_number")
    if value is None:
        value = json.loads((project.shared_state / "twilio-call-settings.json").read_text())["phone_number"]
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise SetupError("Server twilio.phone_number must use E.164 format")
    return value


def current_prompts(project):
    """Reload conversation text without changing live audio, routing or permissions.

    Long-lived frontends must see edits made during their own call. A partially
    saved/invalid profile must not break hangup delivery or an existing call;
    fresh Project/config checks still reject it instead of reporting success.
    """
    try:
        loaded = session_profile(project.root, project.config, project.session_id)["prompts"]
    except (SetupError, UnicodeError):
        if not getattr(project, '_prompt_reload_failed', False):
            print('Prompt reload failed for session ' + project.session_id +
                  '; retaining the last validated prompts. Run config check.', file=sys.stderr)
        project._prompt_reload_failed = True
    else:
        project.profile['prompts'] = loaded
        project._prompt_reload_failed = False
    return project.profile['prompts']


def prompt(project, kind, default, **values):
    text = current_prompts(project).get(kind) or default
    values = {"default": default, "session_id": project.session_id,
              "mailbox_command": project.mailbox_command(), **values}
    # Literal substitutions only; prompts are never shell commands or Python expressions.
    pattern = re.compile(r"\{\{([a-z_]+)\}\}")
    def replace(match):
        if match[1] not in values:
            raise SetupError("Unknown prompt placeholder: " + match[1])
        return str(values[match[1]])
    return pattern.sub(replace, text)


def operator_identity(project):
    """Human-facing directory identity, also valid when the host home is mounted in a container."""
    from voip import Project
    from directory_service import settings
    provider = Project(project.provider_root) if project.provider_root != project.root else project
    cfg = settings(provider)
    home = Path(cfg['root']).resolve()
    label = ('home directory' if project.workspace == home else
             (project.workspace.name or str(project.workspace)) + ' directory') + ' operator'
    destination = project.session_id
    if (cfg['enabled'] and project.session_id == 'default' and project.workspace != home
            and provider.workspace.name not in sessions(provider)):
        destination = provider.workspace.name
    return {'session': project.session_id, 'workspace': str(project.workspace),
            'label': label, 'home_directory': str(home), 'home_routing_enabled': cfg['enabled'],
            'transfer_destination': destination}
