"""PIN verifiers and authenticated encryption. Never include secrets in errors."""
import hashlib
import hmac
import json
import re
import secrets
from voip import SetupError, atomic_write, lock


def master_key(project, create=False):
    path = project.provider_state / 'pin-master-key'
    with lock(project.provider_state / 'pin-key.lock', blocking=True):
        if not path.exists() and create:
            atomic_write(path, secrets.token_hex(32) + '\n')
        try:
            value = path.read_text().strip()
            if not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError()
            return bytes.fromhex(value)
        except (OSError, ValueError):
            raise SetupError('PIN encryption key is missing or invalid; restore it from your secure backup') from None


def derived(key, label):
    return hmac.digest(key, label.encode(), 'sha256')


def valid_auth(value):
    if (not isinstance(value, dict) or set(value) != {'v', 'salt', 'hash', 'length', 'tag'} or
            type(value['v']) is not int or value['v'] != 1 or type(value['length']) is not int or not 6 <= value['length'] <= 12 or
            not all(isinstance(value[k], str) and re.fullmatch(p, value[k]) for k, p in
                    [('salt', r'[0-9a-f]{32}'), ('hash', r'[0-9a-f]{64}'), ('tag', r'[0-9a-f]{64}')])):
        raise SetupError('Invalid PIN verifier; restore it or generate a new PIN')
    return value


def verifier(pin, key, salt=None):
    if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{6,12}', pin):
        raise SetupError('PIN must contain 6–12 digits')
    salt = salt or secrets.token_bytes(16)
    peppered = hmac.digest(derived(key, 'pin-auth-v1'), pin.encode(), 'sha256')
    digest = hashlib.scrypt(peppered, salt=salt, n=16384, r=8, p=5, dklen=32)
    return {'v': 1, 'salt': salt.hex(), 'hash': digest.hex(), 'length': len(pin),
            'tag': hmac.digest(derived(key, 'pin-unique-v1'), pin.encode(), 'sha256').hex()}


def verify(pin, auth, key):
    valid_auth(auth)
    if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{6,12}', pin):
        return False
    return hmac.compare_digest(verifier(pin, key, bytes.fromhex(auth['salt']))['hash'], auth['hash'])


def authentication(project, person):
    if 'pin_auth' in person:
        master_key(project)  # Missing keys must never silently create replacements.
        return valid_auth(person['pin_auth'])
    # Compatibility during migration: stable pseudorandom salt keeps deployment
    # fingerprints stable, while plaintext never reaches the hosted access map.
    key = master_key(project, create=True)
    salt = hmac.digest(key, ('legacy-salt:' + person['id'] + ':' + person['pin']).encode(), 'sha256')[:16]
    return verifier(person['pin'], key, salt)


def seal(pin, key, delivery_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(derived(key, 'pin-delivery-v1')).encrypt(nonce, pin.encode(), delivery_id.encode())
    return {'nonce': nonce.hex(), 'ciphertext': encrypted.hex()}


def unseal(value, key, delivery_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(derived(key, 'pin-delivery-v1')).decrypt(
            bytes.fromhex(value['nonce']), bytes.fromhex(value['ciphertext']), delivery_id.encode()).decode()
    except Exception:
        raise SetupError('Pending PIN could not be decrypted') from None


def pin_path(project, identity):
    from voice_config import configured_path, read_toml
    path = configured_path(project, 'recipients_file', 'recipients.toml')
    data = read_toml(path)
    person = data.get('recipients', {}).get(identity)
    if not person:
        raise SetupError('Unknown recipient')
    if 'pin_file' not in person:
        raise SetupError('Run pin migrate first to replace inline PINs with verifier files')
    return project.root / person['pin_file']


def migrate(project):
    """Preserve existing PINs, replace live plaintext sources; no plaintext backup."""
    with lock(project.shared_state / 'pin-deliveries.lock'):
        result = _migrate(project)
    from voice_config import registered_projects
    from voip import Project
    visited = set()
    for identity, directory in registered_projects(project).items():
        if directory in visited:
            continue
        visited.add(directory)
        result['migrated_recipients'] += migrate(Project(project.root, identity))['migrated_recipients']
    return result


def _migrate(project):
    from voice_config import recipients, configured_path, read_toml
    people, default = recipients(project, secrets=True, include_projects=False)
    path = configured_path(project, 'recipients_file', 'recipients.toml')
    data = read_toml(path) if path.exists() else {'recipients': {}}
    records = data.get('recipients', {})
    # Compute all before changing anything, including validating duplicates.
    values = {identity: authentication(project, person) for identity, person in people.items()}
    if len({v['tag'] for v in values.values()}) != len(values):
        raise SetupError('Use a different PIN for each recipient')
    for identity, person in people.items():
        item = records.setdefault(identity, {k: person[k] for k in ('number', 'sessions', 'incoming_session')})
        location = item.get('pin_file', 'state/pins/' + identity + '.json')
        atomic_write(project.root / location, json.dumps(values[identity]) + '\n')
        item.pop('pin', None)
        item['pin_file'] = location
    # The recipient schema is deliberately small and only uses TOML-compatible
    # JSON strings/lists. Preserve all validated settings, never duplicate secrets.
    lines = ['default_recipient = ' + json.dumps(default)] if default else []
    for identity, item in records.items():
        lines += ['', '[recipients.' + identity + ']']
        lines += [k + ' = ' + json.dumps(v) for k, v in item.items()]
    atomic_write(path, '\n'.join(lines) + '\n')
    return {'migrated_recipients': list(values), 'pins_changed': False}
