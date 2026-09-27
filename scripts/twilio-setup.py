#!/usr/bin/env python3
"""Provision incoming and outgoing phone routing using environment auth.

Run `prepare`, then verify local SIP registration, then `route`. No phone numbers
are purchased and no calls are originated. Runtime secrets never go to stdout.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT
sys.path.insert(0, str(ROOT))
from voip import Project, SetupError, atomic_write, lock, sip_configure
from voice_config import phone_number, recipients, sessions
from service_config import access_bytes, access_config
from twilio_source import function_source

SERVERLESS = "https://serverless.twilio.com/v1/Services"
RUNTIME = "node22"
DEPENDENCIES = [{"name": "twilio", "version": "6.1.1"},
                # Hosted versions differ from the npm package used for local development.
                {"name": "@twilio/runtime-handler", "version": "2.0.1"}]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self):
        sid = os.environ.get("TWILIO_API_KEY_SID") or os.environ.get("TWILIO_SID", "")
        secret = os.environ.get("TWILIO_API_KEY_SECRET") or os.environ.get("TWILIO_SECRET", "")
        if not re.fullmatch(r"SK[0-9a-fA-F]{32}", sid) or not secret:
            raise SetupError("Load the Twilio API key SID and secret from your environment first")
        self.authorization = "Basic " + base64.b64encode(f"{sid}:{secret}".encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, fields=None, upload=None, upload_filename="handler.js", upload_content_type="application/javascript", method=None, revision=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {
                "api.twilio.com", "serverless.twilio.com", "serverless-upload.twilio.com", "sync.twilio.com"}:
            raise SetupError("Refusing to send API credentials to an unexpected destination")
        headers = {"Authorization": self.authorization, "Accept": "application/json"}
        if revision is not None:
            headers['If-Match'] = str(revision)
        data = None
        if fields is not None:
            data = urllib.parse.urlencode(fields, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if upload is not None:
            boundary = "voip-" + secrets.token_hex(16)
            chunks = []
            for name, value in fields.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="Content"; filename="{upload_filename}"\r\nContent-Type: {upload_content_type}\r\n\r\n'.encode())
            chunks.extend([upload, f"\r\n--{boundary}--\r\n".encode()])
            data = b"".join(chunks)
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=45) as response:
                return {} if response.status == 204 else json.load(response)
        except urllib.error.HTTPError as error:
            try:
                code = json.load(error).get("code")
            except (ValueError, AttributeError):
                code = None
            # Provider messages can repeat request data; deliberately omit them.
            raise SetupError(f"Twilio request failed: HTTP {error.code}, provider code {code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise SetupError("Twilio request did not complete; inspect/reconcile before retrying") from None

    def items(self, url, key):
        items = []
        while url:
            data = self.request(url)
            items.extend(data[key])
            following = data.get("meta", {}).get("next_page_url") or data.get("next_page_uri")
            url = urllib.parse.urljoin(url, following) if following else None
        return items


def select_one(items, field, value):
    matches = [item for item in items if item.get(field) == value]
    if len(matches) > 1:
        raise SetupError("Multiple resources match the dedicated configuration; inspect before continuing")
    return matches[0] if matches else None


class Setup:
    def __init__(self):
        self.api = Api()
        self.path = ROOT / "state/twilio-provisioning.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "service_unique_name": "codex-phone-" + secrets.token_hex(4)}
        self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.state, indent=2) + "\n")

    def ensure(self, url, key, field, value, body):
        existing = select_one(self.api.items(url + "?PageSize=100", key), field, value)
        return existing or self.api.request(url, body)

    def service(self):
        service = self.ensure(SERVERLESS, "services", "unique_name", self.state["service_unique_name"], {
            "UniqueName": self.state["service_unique_name"], "FriendlyName": "Persistent Codex phone operator",
            "IncludeCredentials": "false", "UiEditable": "true"})
        if self.state.get("account_sid", service["account_sid"]) != service["account_sid"]:
            raise SetupError("API key belongs to a different account from the saved deployment")
        self.state.update(service_sid=service["sid"], account_sid=service["account_sid"])
        self.save()
        self.service_url = SERVERLESS + "/" + service["sid"]
        self.account_url = "https://api.twilio.com/2010-04-01/Accounts/" + service["account_sid"]

    def settings(self):
        project = Project(ROOT)
        settings = {"phone_number": phone_number(project),
                    "allowed_callers": [r['number'] for r in recipients(project)[0].values()]}
        values = [settings["phone_number"], *settings["allowed_callers"]]
        if not settings["allowed_callers"] or not all(re.fullmatch(r"\+[1-9]\d{7,14}", v) for v in values):
            raise SetupError("Phone number and allowed callers must use E.164 format")
        return settings

    def prepare(self):
        from pin_security import migrate, master_key
        migrate(Project(ROOT))
        settings = self.settings()
        self.service()
        private = ROOT / "state/twilio-sip-secret"
        if self.state.get("sip_credential_sid") and not private.exists():
            raise SetupError("Saved SIP password is missing; recover it before modifying the credential")
        if not private.exists():
            alphabet = string.ascii_letters + string.digits
            atomic_write(private, "Aa9" + "".join(secrets.choice(alphabet) for _ in range(45)) + "\n")
        password = private.read_text().strip()
        ticket_secret = ROOT / "state/twilio-call-request-secret"
        if not ticket_secret.exists():
            if "outgoing" in self.state.get("functions", {}):
                raise SetupError("Outgoing call secret is missing; recover it before redeploying")
            atomic_write(ticket_secret, secrets.token_hex(32) + "\n")
        request_secret = ticket_secret.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", request_secret):
            raise SetupError("Invalid outgoing call request secret")
        env = self.ensure(self.service_url + "/Environments", "environments", "unique_name", "production", {
            "UniqueName": "production", "DomainSuffix": "live"})
        self.state.update(environment_sid=env["sid"], function_domain=env["domain_name"])
        self.save()
        # Receipt survives host outages; no client tokens are issued for this service.
        sync = self.ensure('https://sync.twilio.com/v1/Services', 'services', 'friendly_name',
                          self.state['service_unique_name'] + '-pin-delivery',
                          {'FriendlyName': self.state['service_unique_name'] + '-pin-delivery',
                           'AclEnabled': 'true', 'ReachabilityWebhooksEnabled': 'false'})
        self.state['pin_sync_service_sid'] = sync['sid']
        self.save()
        webhook = "https://" + env["domain_name"] + "/incoming"
        name = self.state["service_unique_name"]
        domain_name = name + ".sip.twilio.com"
        domain = self.ensure(self.account_url + "/SIP/Domains.json", "domains", "domain_name", domain_name, {
            "DomainName": domain_name, "FriendlyName": name, "SipRegistration": "true",
            "Secure": "true", "VoiceUrl": webhook, "VoiceMethod": "POST"})
        self.state.update(domain_sid=domain["sid"], sip_domain=domain_name, sip_username="operator")
        self.save()
        if not domain["sip_registration"] or not domain["secure"]:
            raise SetupError("Dedicated SIP domain does not have registration and secure media enabled")
        credential_list = self.ensure(self.account_url + "/SIP/CredentialLists.json", "credential_lists",
                                     "friendly_name", name, {"FriendlyName": name})
        self.state["credential_list_sid"] = credential_list["sid"]
        self.save()
        credentials_url = self.account_url + "/SIP/CredentialLists/" + credential_list["sid"] + "/Credentials.json"
        credential = self.ensure(credentials_url, "credentials", "username", "operator", {
            "Username": "operator", "Password": password})
        self.state["sip_credential_sid"] = credential["sid"]
        self.save()
        for kind in ("Calls", "Registrations"):
            url = self.account_url + "/SIP/Domains/" + domain["sid"] + f"/Auth/{kind}/CredentialListMappings.json"
            self.ensure(url, "contents", "sid", credential_list["sid"], {
                "CredentialListSid": credential_list["sid"]})
        print("SIP domain and credential mappings ready.", flush=True)

        project = Project(ROOT)
        self.state['session_credentials'] = self.state.get('session_credentials', {})
        for identity in sessions(project):
            if identity == 'default':
                continue
            scoped = Project(ROOT, identity)
            private = scoped.state / 'twilio-sip-secret'
            if identity in self.state['session_credentials'] and not private.exists():
                raise SetupError('A session SIP password is missing; recover it before changing credentials')
            if not private.exists():
                atomic_write(private, 'Aa9' + secrets.token_urlsafe(36) + '\n')
            username = 'operator-' + identity
            credential = self.ensure(credentials_url, 'credentials', 'username', username,
                                     {'Username': username, 'Password': private.read_text().strip()})
            self.state['session_credentials'][identity] = {'username': username, 'sid': credential['sid']}
            self.save()

        # PIN maps exceed Twilio's small environment-variable limit as contacts grow.
        # Deploy them as a private Asset, never as a public file or Function response.
        mapping = access_config(project)
        mapping['sessions'] = {s: 'operator' + ('-' + s if s != 'default' else '') for s in sessions(project)}
        content = json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode()
        asset = self.ensure(self.service_url + '/Assets', 'assets', 'friendly_name', 'voice-access', {'FriendlyName': 'voice-access'})
        digest = hashlib.sha256(content).hexdigest()
        access = self.state.get('access_asset', {})
        if access.get('sha256') != digest:
            upload_url = self.service_url.replace('serverless.twilio.com', 'serverless-upload.twilio.com')
            version = self.api.request(upload_url + '/Assets/' + asset['sid'] + '/Versions',
                {'Path': '/voice-access.json', 'Visibility': 'private'}, upload=content,
                upload_filename='voice-access.json', upload_content_type='application/json')
            access = {'sid': asset['sid'], 'version_sid': version['sid'], 'sha256': digest}
            self.state['access_asset'] = access
            self.save()

        variables_url = self.service_url + "/Environments/" + env["sid"] + "/Variables"
        variables = self.api.items(variables_url + "?PageSize=100", "variables")
        required = {"TWILIO_NUMBER": settings["phone_number"], "ALLOWED_CALLERS": settings["allowed_callers"][0],
                    "PIN_MASTER_KEY": master_key(project).hex(), "SIP_DOMAIN": domain_name, "SIP_USERNAME": "operator",
                    "VOICE_ACCOUNT_SID": self.state["account_sid"],
                    "OUTBOUND_ALLOWED_RECIPIENTS": settings["allowed_callers"][0],
                    "CALL_REQUEST_SECRET": request_secret}
        required['CALL_ROUTING_ENABLED'] = '1'
        required['VOICE_ACCESS_ASSET'] = '/voice-access.json'
        required.update(PIN_SYNC_SERVICE_SID=self.state['pin_sync_service_sid'],
                        PIN_SYNC_KEY_SID=os.environ.get('TWILIO_API_KEY_SID') or os.environ.get('TWILIO_SID'),
                        PIN_SYNC_KEY_SECRET=os.environ.get('TWILIO_API_KEY_SECRET') or os.environ.get('TWILIO_SECRET'))
        for key, value in required.items():
            current = select_one(variables, "key", key)
            if current is None:
                self.api.request(variables_url, {"Key": key, "Value": value})
            elif current["value"] != value:
                self.api.request(variables_url + "/" + current["sid"], {"Value": value})
        for current in variables:
            if current['key'] == 'CALL_PIN':
                self.api.request(variables_url + '/' + current['sid'], method='DELETE')

        versions = []
        self.state.setdefault("functions", {})
        for name in ("incoming", "authorize", "outgoing", "pin-delivery", "call-status", "handoff"):
            function = self.ensure(self.service_url + "/Functions", "functions", "friendly_name", name, {"FriendlyName": name})
            content = function_source(SOURCE_ROOT, name)
            digest = hashlib.sha256(content).hexdigest()
            saved = self.state["functions"].get(name, {})
            if saved.get("sha256") != digest:
                upload_url = self.service_url.replace("serverless.twilio.com", "serverless-upload.twilio.com")
                version = self.api.request(upload_url + "/Functions/" + function["sid"] + "/Versions", {
                    "Path": "/" + name, "Visibility": "protected"}, upload=content)
                saved = {"sid": function["sid"], "version_sid": version["sid"], "sha256": digest}
                self.state["functions"][name] = saved
                self.save()
            versions.append(saved["version_sid"])
        fingerprint = hashlib.sha256(json.dumps([versions, access['version_sid'], DEPENDENCIES, RUNTIME]).encode()).hexdigest()
        if self.state.get("build_fingerprint") != fingerprint:
            build = self.api.request(self.service_url + "/Builds", {
                "FunctionVersions": versions, "AssetVersions": [access['version_sid']],
                "Dependencies": json.dumps(DEPENDENCIES), "Runtime": RUNTIME})
            self.state.update(build_sid=build["sid"], build_fingerprint=fingerprint)
            self.save()
        deadline = time.monotonic() + 300
        while True:
            build = self.api.request(self.service_url + "/Builds/" + self.state["build_sid"])
            print("Hosted Function build: " + build["status"], flush=True)
            if build["status"] == "completed":
                break
            if build["status"] == "failed" or time.monotonic() > deadline:
                raise SetupError("Hosted Function build did not complete; inspect the build before retrying")
            time.sleep(10)
        if self.state.get("deployed_build_sid") != build["sid"]:
            deployment = self.api.request(self.service_url + "/Environments/" + env["sid"] + "/Deployments", {"BuildSid": build["sid"]})
            self.state.update(deployed_build_sid=build["sid"], deployment_sid=deployment["sid"])
            self.save()
        config_path = Project(ROOT).server_path
        original = (config_path if config_path.exists() else SOURCE_ROOT / "config.example.toml").read_text()
        if not (ROOT / "state/config-before-twilio.toml").exists():
            atomic_write(ROOT / "state/config-before-twilio.toml", original)
        project = Project(ROOT)
        updated = original.replace('domain = ' + json.dumps(project.sip["domain"]), 'domain = ' + json.dumps(domain_name))
        updated = updated.replace('username = ' + json.dumps(project.sip["username"]), 'username = "operator"')
        atomic_write(config_path, updated)
        project = Project(ROOT)
        if project.sip["domain"] != domain_name or project.sip["username"] != "operator":
            raise SetupError("Could not update SIP configuration; inspect config.toml")
        sip_configure(project, password=password)
        for identity in sessions(project):
            if identity != 'default':
                scoped = Project(ROOT, identity)
                sip_configure(scoped, password=(scoped.state / 'twilio-sip-secret').read_text().strip())
        print("Protected Functions deployed; private Baresip configuration ready. Phone routing is unchanged.", flush=True)

    def verify(self):
        from pin_security import master_key
        self.service()
        settings = self.settings()
        env = self.api.request(self.service_url + "/Environments/" + self.state["environment_sid"])
        if env["build_sid"] != self.state["deployed_build_sid"]:
            raise SetupError("Live environment build differs from the saved deployment")
        for name, info in self.state["functions"].items():
            current_digest = hashlib.sha256(function_source(SOURCE_ROOT, name)).hexdigest()
            if info["sha256"] != current_digest:
                raise SetupError("Local call-entry code differs from the deployed version")
            version = self.api.request(self.service_url + "/Functions/" + info["sid"] + "/Versions/" + info["version_sid"])
            if version["visibility"] != "protected" or version["path"] != "/" + name:
                raise SetupError("Function visibility/path verification failed")
        variables = self.api.items(self.service_url + "/Environments/" + env["sid"] + "/Variables?PageSize=100", "variables")
        observed = {v["key"]: v["value"] for v in variables}
        expected = {"TWILIO_NUMBER": settings["phone_number"], "ALLOWED_CALLERS": settings["allowed_callers"][0],
                    "PIN_MASTER_KEY": master_key(Project(ROOT)).hex(),
                    "PIN_SYNC_SERVICE_SID": self.state['pin_sync_service_sid'],
                    "PIN_SYNC_KEY_SID": os.environ.get('TWILIO_API_KEY_SID') or os.environ.get('TWILIO_SID'),
                    "PIN_SYNC_KEY_SECRET": os.environ.get('TWILIO_API_KEY_SECRET') or os.environ.get('TWILIO_SECRET'),
                    "SIP_DOMAIN": self.state["sip_domain"], "SIP_USERNAME": self.state["sip_username"],
                    "VOICE_ACCOUNT_SID": self.state["account_sid"],
                    "OUTBOUND_ALLOWED_RECIPIENTS": settings["allowed_callers"][0],
                    "CALL_REQUEST_SECRET": (ROOT / "state/twilio-call-request-secret").read_text().strip()}
        if any(observed.get(k) != v for k, v in expected.items()):
            raise SetupError("Hosted call-entry variables differ from local configuration")
        if 'CALL_PIN' in observed:
            raise SetupError('Legacy plaintext PIN variable still exists')
        sync = self.api.request('https://sync.twilio.com/v1/Services/' + self.state['pin_sync_service_sid'])
        if not sync.get('acl_enabled') or sync.get('account_sid') != self.state['account_sid']:
            raise SetupError('PIN receipt service security/account verification failed')
        if observed.get('VOICE_ACCESS_ASSET') != '/voice-access.json':
            raise SetupError('Hosted recipient configuration is missing')
        access = self.state.get('access_asset', {})
        if access.get('sha256') != hashlib.sha256(access_bytes(Project(ROOT))).hexdigest():
            raise SetupError('Recipient or session routing changed; redeploy configuration')
        version = self.api.request(self.service_url + '/Assets/' + access['sid'] + '/Versions/' + access['version_sid'])
        if version.get('visibility') != 'private' or version.get('path') != '/voice-access.json':
            raise SetupError('Recipient configuration Asset is not private')
        domain = self.api.request(self.account_url + "/SIP/Domains/" + self.state["domain_sid"] + ".json")
        webhook = "https://" + self.state["function_domain"] + "/incoming"
        if (not domain["secure"] or not domain["sip_registration"] or
                domain["domain_name"] != self.state["sip_domain"] or
                domain["voice_url"] != webhook or domain["voice_method"] != "POST"):
            raise SetupError("SIP security/registration verification failed")
        for kind in ("Calls", "Registrations"):
            mappings = self.api.items(self.account_url + "/SIP/Domains/" + domain["sid"] +
                f"/Auth/{kind}/CredentialListMappings.json?PageSize=100", "contents")
            if not select_one(mappings, "sid", self.state["credential_list_sid"]):
                raise SetupError("SIP authentication mapping verification failed")
        numbers = self.api.items(self.account_url + "/IncomingPhoneNumbers.json?PageSize=100", "incoming_phone_numbers")
        number = select_one(numbers, "phone_number", settings["phone_number"])
        if not number or not number["capabilities"].get("voice"):
            raise SetupError("Configured phone number is not voice-capable on this account")
        self.state.update(number_sid=number["sid"], phone_number=number["phone_number"])
        self.save()
        print("Verified hosted build, protected routes, caller/PIN configuration, and secure SIP domain.")
        return number

    def route(self):
        number = self.verify()
        url = "https://" + self.state["function_domain"] + "/incoming"
        if number.get("voice_application_sid") or number.get("trunk_sid"):
            raise SetupError("Number belongs to an existing application/trunk; inspect before changing it")
        if number.get("voice_url") not in (None, "", url):
            raise SetupError("Number has another voice webhook; inspect before replacing it")
        if "previous_number_routing" not in self.state:
            self.state["previous_number_routing"] = {key: number.get(key) for key in
                ("voice_url", "voice_method", "voice_fallback_url", "voice_application_sid", "trunk_sid")}
            self.save()
        updated = self.api.request(self.account_url + "/IncomingPhoneNumbers/" + number["sid"] + ".json", {
            "VoiceUrl": url, "VoiceMethod": "POST", "VoiceFallbackUrl": "",
            "StatusCallback": "https://" + self.state["function_domain"] + "/call-status", "StatusCallbackMethod":"POST"})
        if updated["voice_url"] != url or updated["voice_method"] != "POST":
            raise SetupError("Number routing update did not match the requested webhook")
        self.state["number_routed"] = True
        self.save()
        print("Incoming calls now route to the protected PIN entry: " + number["phone_number"])


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "route"))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("VOIP_ROOT", str(ROOT))))
    args = parser.parse_args()
    ROOT = args.root.resolve()
    from release_config import load_credentials
    load_credentials(Project(ROOT))
    with lock(ROOT / "state/twilio-setup.lock"):
        getattr(Setup(), args.action)()


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, KeyError, ValueError) as error:
        # Do not print arbitrary responses, request bodies, or credential values.
        print(str(error) if isinstance(error, SetupError) else "Setup failed while reading local/provider configuration; inspect its shape without printing secrets", file=sys.stderr)
        raise SystemExit(1)
