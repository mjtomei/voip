import json
import subprocess
from service_fixture import ServiceFixture
from pin_security import master_key, migrate, seal, unseal, verifier, verify
from service_config import access_bytes, check_config
from voice_config import recipients
from voip import ROOT, SetupError

class PinSecurityTests(ServiceFixture):
    def test_migration_preserves_pins_without_plaintext_sources_or_assets(self):
        first = access_bytes(self.project)
        migrate(self.project)
        self.assertEqual(first, access_bytes(self.project))
        self.assertNotIn('123456', (self.root/'recipients.toml').read_text())
        people, _ = recipients(self.project, secrets=True)
        key = master_key(self.project)
        self.assertTrue(verify('123456', people['alice']['pin_auth'], key))
        self.assertFalse(verify('65432100', people['alice']['pin_auth'], key))
        for path in [self.root/'state/pins/alice.json', self.root/'state/pin-master-key']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('123456', path.read_text())
        self.assertNotIn('pin_auth', json.dumps(check_config(self.project)))
        migrate(self.project)
        self.assertEqual(first, access_bytes(self.project))

    def test_python_verifier_and_ciphertext_work_in_node(self):
        key = bytes.fromhex('ab'*32)
        auth = verifier('90765432', key)
        envelope = seal('90765432', key, 'fixture')
        data = dict(key=key.hex(), auth=auth, envelope=envelope)
        script = '''const fs=require('fs'), c=require('crypto');
const {voiceAccess}=require('./twilio/access');const d=JSON.parse(fs.readFileSync(0,'utf8'));
if(!voiceAccess.verify({PIN_MASTER_KEY:d.key},{pin_auth:d.auth},'90765432')) process.exit(2);
if(voiceAccess.verify({PIN_MASTER_KEY:d.key},{pin_auth:d.auth},'90765433')) process.exit(3);
const key=c.createHmac('sha256',Buffer.from(d.key,'hex')).update('pin-delivery-v1').digest();
const blob=Buffer.from(d.envelope.ciphertext,'hex');
const dec=c.createDecipheriv('aes-256-gcm',key,Buffer.from(d.envelope.nonce,'hex'));
dec.setAAD(Buffer.from('fixture'));dec.setAuthTag(blob.subarray(-16));
if(Buffer.concat([dec.update(blob.subarray(0,-16)),dec.final()]).toString()!=='90765432') process.exit(4);
'''
        completed = subprocess.run(['node','-e',script], cwd=ROOT, input=json.dumps(data), text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(completed.stdout)

    def test_authenticated_encryption_rejects_tampering_wrong_job_and_key(self):
        key = b'a'*32
        value = seal('90012345', key, 'job')
        self.assertEqual(unseal(value,key,'job'), '90012345')
        for data, secret, identity in [(value,key,'other'),(value,b'b'*32,'job'),
                ({**value,'ciphertext':'00'+value['ciphertext'][2:]},key,'job')]:
            with self.assertRaises(SetupError): unseal(data,secret,identity)

    def test_missing_key_does_not_silently_replace_existing_verifiers(self):
        migrate(self.project)
        (self.root/'state/pin-master-key').unlink()
        with self.assertRaises(SetupError): access_bytes(self.project)
        self.assertFalse((self.root/'state/pin-master-key').exists())
