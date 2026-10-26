"""SABnzbd 5.1.3 first-run enrollment through its supported HTTP APIs.

The caller owns the enrollment lock, fresh-directory reservation and local
container/endpoint checks. Credentials and API keys travel only in POST bodies.
The only wizard-completion condition in this SAB version is a real configured
NNTP server; do not invent one or pretend an untested provider is ready.

Upstream contracts: sabnzbd/sabnzbd tag 5.1.3, interface.py LoginPage,
MainPage.index/check_login; api.py _api_set_config/test_nntp_server_dict.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
from urllib.parse import urljoin, urlsplit

from . import engine
from .safety import atomic_write, private_read


APP = "sabnzbd"
VERSION = "5.1.3"
PROTECTED_UI = "/config/general/"


def _fail(message):
    # Call-time import allows onboarding to dispatch here without an import cycle.
    from .onboarding import _Manual
    raise _Manual(message)


def _send(session, method, path, **kwargs):
    try:
        return session.send(method, path, **kwargs)
    except Exception:
        _fail("SABnzbd HTTP verification did not finish; saved state and existing credentials were retained")


def _redirect_path(base, location):
    if not isinstance(location, str) or not location:
        return None
    target, origin = urlsplit(urljoin(base + "/", location)), urlsplit(base)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        _fail("SABnzbd returned an unexpected login redirect; no existing account was changed")
    return target.path.rstrip("/") or "/"


def _denied(result, base):
    return result.status in {401, 403} or (result.status in {302, 303}
        and _redirect_path(base, result.location) == "/login")


class _Api:
    def __init__(self, operation):
        self.operation, self.base = operation, operation.endpoints[APP]
        try:
            self.key = operation.secret_resolver("sab_api_key")
        except Exception:
            _fail("SABnzbd's generated API key is not readable yet; rerun account setup after the service is ready")
        if not isinstance(self.key, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", self.key):
            _fail("SABnzbd's generated API key has an unsupported format; no key was replaced")
        self.session = operation.session_factory(self.base)
        version = self.call("version").get("version")
        if version != VERSION:
            _fail("SABnzbd's running version differs from the reviewed enrollment API; no account was changed")

    def call(self, mode, **fields):
        response = _send(self.session, "POST", "/api", form={
            "mode": mode, "output": "json", "apikey": self.key, **fields})
        if response.status != 200 or not isinstance(response.data, dict) or response.data.get("status") is False:
            _fail("SABnzbd rejected an authenticated API operation; existing state and saved credentials were preserved")
        return response.data

    def misc(self):
        report = self.call("get_config", section="misc").get("config")
        value = report.get("misc") if isinstance(report, dict) else None
        if (not isinstance(value, dict) or not isinstance(value.get("username"), str)
                or not isinstance(value.get("password"), str)
                or not _boolean(value.get("html_login"))):
            _fail("SABnzbd returned unsupported account settings; no existing credentials were changed")
        return value

    def servers(self):
        report = self.call("get_config", section="servers").get("config")
        # A fresh SAB has no servers key in CFG_DATABASE: get_dconfig returns
        # {} for that missing section, hence {"config": {}} on the wire.
        value = [] if report == {} else report.get("servers") if isinstance(report, dict) else None
        if isinstance(value, dict):
            rows = []
            for name, row in value.items():
                if not isinstance(name, str) or not isinstance(row, dict) or row.get("name", name) != name:
                    _fail("SABnzbd's NNTP provider identities are ambiguous; existing providers were preserved")
                rows.append({**row, "name": name})
            value = rows
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            _fail("SABnzbd returned an invalid NNTP provider collection; existing providers were preserved")
        names = [row.get("name") for row in value]
        if any(not isinstance(name, str) or not name or len(name) > 256 or any(ord(c) < 32 for c in name)
               for name in names) or len(set(names)) != len(names):
            _fail("SABnzbd's NNTP provider identities are ambiguous; existing providers were preserved")
        return value

    def test_server(self, **fields):
        # test_server belongs to _api_config_table, not the top-level mode
        # table. The supported dispatcher is mode=config&name=test_server.
        result = self.call("config", name="test_server", **fields).get("value")
        if not isinstance(result, dict) or type(result.get("result")) is not bool:
            _fail("SABnzbd returned an unsupported NNTP connection-test report; no connection success was assumed")
        return result["result"]


def _fresh_entry(operation):
    entry = operation.receipt.get("apps", {}).get(APP, {})
    if entry.get("eligible") is not True:
        _fail("SABnzbd was not reserved as a new empty application; its existing account was not changed")
    return entry


def _login(operation):
    base, creds = operation.endpoints[APP], operation.credentials
    anonymous = operation.session_factory(base)
    if not _denied(_send(anonymous, "GET", PROTECTED_UI), base):
        _fail("SABnzbd's administrator UI allowed anonymous access; review its authentication settings")
    session = operation.session_factory(base)
    result = _send(session, "POST", "/login/", form={
        "username": creds["admin_username"], "password": creds["admin_password"]})
    if (result.status not in {302, 303} or _redirect_path(base, result.location) != "/"
            or not session.has_cookies() or _send(session, "GET", PROTECTED_UI).status != 200):
        _fail("SABnzbd's administrator login could not be verified; no existing password was reset")
    bad = operation.session_factory(base)
    wrong = secrets.token_urlsafe(48)
    if wrong == creds["admin_password"]:
        wrong += "x"
    _send(bad, "POST", "/login/", form={"username": creds["admin_username"], "password": wrong})
    if not _denied(_send(bad, "GET", PROTECTED_UI), base):
        _fail("SABnzbd's UI accepted an incorrect password; authentication verification failed")
    return session


def _save_key(operation, api):
    path = engine.secret_path(operation.c, "sab_api_key")
    if path.exists():
        if private_read(path).rstrip("\r\n") != api.key:
            _fail("Saved SABnzbd API key differs; no existing key or secret file was replaced")
    else:
        operation.mark(APP, api_key_intent=True)
        atomic_write(path, api.key + "\n")
    api.misc()  # Prove the saved key has full configuration access.
    operation.mark(APP, api_key_verified=True)


def enroll(operation):
    """Create only a reserved blank account; interrupted own writes may resume."""
    entry, creds = _fresh_entry(operation), operation.credentials
    if not creds["admin_password"].strip("*"):
        _fail("SABnzbd cannot store an all-asterisk administrator password; choose another password before enrollment")
    api = _Api(operation)
    current = api.misc()
    blank = not current["username"] and not current["password"]
    if not entry.get("admin_intent"):
        if not blank:
            # Existing credentials are verify-only, including the same username.
            if current["username"] != creds["admin_username"] or not current["password"] or not current["html_login"]:
                _fail("SABnzbd already has different account settings; no existing credentials were changed")
            _login(operation)
            _save_key(operation, api)
            operation.mark(APP, admin_verified=True, state="verified", existing_account=True,
                           wizard_complete=bool(api.servers()))
            return "Existing SABnzbd administrator login verified without changing its account; anonymous and incorrect-password access rejected"
        operation.mark(APP, admin_intent=True, state="intent")
    if current["username"] not in {"", creds["admin_username"]}:
        _fail("SABnzbd's account identity changed after reservation; no existing password was reset")
    if entry.get("state") == "verified" and (not current["username"] or not current["password"]):
        _fail("The enrolled SABnzbd account is missing; no replacement was created")
    if not current["html_login"]:
        if not blank:
            _fail("SABnzbd's authentication mode changed; existing settings were preserved")
        operation.mark(APP, login_mode_intent=True)
        api.call("set_config", section="misc", keyword="html_login", value=1)
        current = api.misc()
        if not current["html_login"]:
            _fail("SABnzbd did not enable form authentication")
    if not current["username"]:
        if current["password"]:
            _fail("SABnzbd already contains a password without the expected username; no account was reset")
        operation.mark(APP, username_write_intent=True)
        current = api.misc()
        if current["username"] or current["password"]:
            _fail("SABnzbd account settings changed before enrollment; rerun for a read-only verification")
        api.call("set_config", section="misc", keyword="username", value=creds["admin_username"])
        current = api.misc()
    if current["username"] != creds["admin_username"]:
        _fail("SABnzbd did not persist the requested administrator name; no password was written")
    if not current["password"]:
        operation.mark(APP, password_write_intent=True)
        current = api.misc()
        if current["username"] != creds["admin_username"]:
            _fail("SABnzbd's account name changed before the password write; no password was changed")
        # An interrupted request may already have set a password. Never reset it.
        if not current["password"]:
            api.call("set_config", section="misc", keyword="password", value=creds["admin_password"])
    saved = api.misc()
    if saved["username"] != creds["admin_username"] or not saved["password"] or not saved["html_login"]:
        _fail("SABnzbd's account settings were not confirmed; existing state was retained")
    _login(operation)
    _save_key(operation, api)
    operation.mark(APP, admin_verified=True, state="verified", wizard_complete=bool(api.servers()))
    return "SABnzbd administrator form login verified; anonymous and incorrect-password access rejected"


def inspect_existing(operation):
    """Read configuration/auth gates only; do not claim a password login."""
    api = _Api(operation)
    current = api.misc()
    if not current["username"] or not current["password"]:
        _fail("Existing SABnzbd has no complete UI account; its credentials were not changed")
    if not _denied(_send(operation.session_factory(api.base), "GET", PROTECTED_UI), api.base):
        _fail("Existing SABnzbd allows anonymous administrator UI access; its settings were not changed")
    return "Existing SABnzbd API-key access and configured UI authentication verified; existing credentials preserved, password login not tested"


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        _fail("NNTP provider hostname is invalid; use a DNS hostname or IP address without a URL or port")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            _fail("NNTP provider hostname is invalid; use a DNS hostname or IP address without a URL or port")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
            _fail("NNTP provider hostname is invalid; use a DNS hostname or IP address without a URL or port")
        return value.lower()
    if address.is_unspecified or address.is_multicast:
        _fail("NNTP provider hostname cannot be an unspecified or multicast address")
    return str(address)


def _boolean(value):
    return type(value) in {bool, int} and value in {0, 1}


def _provider(provider):
    if not isinstance(provider, dict) or set(provider) - {"host", "port", "username", "password", "connections", "ssl"}:
        _fail("NNTP provider input has unsupported fields")
    host, port, connections = _host(provider.get("host")), provider.get("port", 563), provider.get("connections", 10)
    if type(port) is not int or not 1 <= port <= 65535 or type(connections) is not int or not 1 <= connections <= 500:
        _fail("NNTP port must be 1-65535 and connection allowance 1-500")
    if provider.get("ssl", True) is not True:
        _fail("Automatic NNTP enrollment requires TLS with strict certificate verification")
    for field in ("username", "password"):
        value = provider.get(field)
        if (not isinstance(value, str) or not value or len(value) > 256 or value != value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            _fail("NNTP credentials must be nonempty, without control characters or surrounding whitespace")
    if not provider["password"].strip("*"):
        _fail("SABnzbd cannot store an all-asterisk NNTP password")
    return {"host": host, "port": port, "username": provider["username"], "password": provider["password"],
            "connections": connections, "ssl": 1, "ssl_verify": 3, "enable": 1}


def _server_public(row):
    host = _host(row.get("host"))
    if type(row.get("port")) is not int or not 1 <= row["port"] <= 65535:
        _fail("An existing NNTP provider has an invalid port; it was not changed")
    if type(row.get("connections")) is not int or not 0 <= row["connections"] <= 500:
        _fail("An existing NNTP provider has an invalid connection allowance; it was not changed")
    if not _boolean(row.get("enable")) or not _boolean(row.get("ssl")):
        _fail("An existing NNTP provider has unknown enable/TLS settings; it was not changed")
    return {"name": row["name"], "host": host, "port": row["port"], "enabled": bool(row["enable"]),
            "ssl": bool(row["ssl"]), "connections": row["connections"]}


def configured_providers(operation):
    """Return identifiers/settings suitable for display, never provider secrets."""
    return [_server_public(row) for row in _Api(operation).servers()]


def _test_stored(api, row):
    public = _server_public(row)
    if (not isinstance(row.get("username"), str) or type(row.get("ssl_verify")) is not int
            or row["ssl_verify"] not in {0, 1, 2, 3}):
        _fail("An existing NNTP provider has unsupported authentication/TLS settings; it was not changed")
    return api.test_server(server=row["name"], host=public["host"], port=public["port"],
        username=row["username"], password="******" if row.get("password") else "",
        connections=public["connections"], ssl=int(public["ssl"]), ssl_verify=row["ssl_verify"], timeout=10)


def verify_existing_providers(operation):
    """Actually test stored provider credentials without modifying any server."""
    api, results, verified = _Api(operation), [], 0
    for row in api.servers():
        public = _server_public(row)
        status = "skipped" if not public["enabled"] else "passed" if _test_stored(api, row) else "failed"
        verified += status == "passed"
        results.append({"name": public["name"], "host": public["host"], "port": public["port"], "status": status})
    return {"ok": bool(verified) and not any(row["status"] == "failed" for row in results),
            "verified": verified, "results": results}


def configure_provider(operation, provider):
    """Add only a deployment-owned server, test credentials and confirm readiness."""
    entry = operation.receipt.get("apps", {}).get(APP, {})
    fresh = entry.get("eligible") is True and entry.get("admin_verified") is True
    scoped = entry.get("provider_scope_authorized") is True
    if not fresh and not scoped:
        if entry.get("eligible") is not True:
            _fail("SABnzbd was not reserved as a new empty application; its existing account was not changed")
        _fail("Verify SABnzbd's administrator login before adding an NNTP provider")
    desired = _provider(provider)
    identifier = str(operation.c["deployment_id"])
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", identifier):
        _fail("NNTP provider enrollment requires a valid deployment identity")
    name = "samurai-" + identifier
    identity = {key: value for key, value in desired.items() if key != "password"}
    if entry.get("provider_identity") not in (None, identity):
        _fail("Saved NNTP provider settings differ; no existing provider was changed")
    # A provider-only authorization never permits account enrollment. Prove
    # the existing UI password again before every such server configuration.
    if scoped:
        if not isinstance(operation.credentials, dict):
            _fail("NNTP setup requires the existing SABnzbd administrator login; no account was changed")
        _login(operation)
    api = _Api(operation)
    before = api.servers()
    matches = [row for row in before if row["name"] == name]
    if matches and not entry.get("provider_intent"):
        _fail("A deployment-named NNTP provider already exists; it was not adopted or overwritten")
    if not matches and entry.get("provider_verified"):
        _fail("The enrolled NNTP provider is missing; no replacement was created")
    # Verify only a new provider's durable label intent. A previously verified
    # provider may have been renamed deliberately and its custom label is kept.
    expected_displayname = entry.get("provider_displayname") if not entry.get("provider_verified") else None
    # First test supplied settings; a failed connection must not create a server.
    if not api.test_server(timeout=10, **desired):
        _fail("NNTP TLS connection or authentication test failed; existing providers were preserved")
    if not matches:
        expected_displayname = "NNTP | " + desired["host"]
        operation.mark(APP, provider_intent=True, provider_identity=identity,
                       provider_displayname=expected_displayname)
        latest = api.servers()
        if latest != before:
            _fail("NNTP providers changed after review; no new provider was written")
        # The stable private server key is an ownership identity, not a UI label.
        # Only this new provider is named here; existing custom labels are kept.
        api.call("set_config", section="servers", keyword=name,
                 displayname=expected_displayname, **desired)
    after = api.servers()
    matches = [row for row in after if row["name"] == name]
    others = [row for row in after if row["name"] != name]
    if others != [row for row in before if row["name"] != name]:
        _fail("Other NNTP provider settings changed during enrollment; inspect them before continuing")
    if len(matches) != 1 or any(matches[0].get(key) != value for key, value in identity.items()):
        _fail("NNTP provider settings differ from the reviewed input; no existing provider was overwritten")
    if expected_displayname is not None and matches[0].get("displayname") != expected_displayname:
        _fail("New NNTP provider display label was not saved; the provider was retained for review")
    if not _test_stored(api, matches[0]):
        _fail("Stored NNTP credentials did not pass the connection test; the provider was retained for review")
    session = _login(operation)
    if _send(session, "GET", "/").status != 200:
        _fail("SABnzbd still requires its setup wizard; the saved provider was retained for review")
    operation.mark(APP, provider_verified=True, wizard_complete=True)
    return {"name": name, "host": desired["host"], "port": desired["port"],
            "connection_verified": True, "wizard_complete": True}
