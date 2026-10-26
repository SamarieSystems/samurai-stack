"""First accounts for proven-new application directories, never account recovery.

Reserve before the first Compose start. Enrollment is deliberately separate from
ordinary configuration: a legacy installation, initialized account, or ambiguous
interrupted write is never a reason to reset a password or edit a database.

Do not run a browser startup wizard concurrently with automated enrollment.
The empty-password login and account-absence proofs bound first-run operations;
they cannot serialize another human's simultaneous browser changes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.cookiejar
import ipaddress
import json as codec
import os
from pathlib import Path
import re
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request

from . import catalog, engine, naming
from .integrations import jellyfin_headers
from .safety import SafetyError, atomic_write, no_symlinks, private_read, run, validate_config, write_json

SUPPORTED = ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd", "seerr")
MEDIA_APPS = frozenset(SUPPORTED)
CREDENTIAL_FIELDS = ("admin_username", "admin_password", "viewer_username", "viewer_password")
VIEWER_POLICY = {
    "IsAdministrator": False, "IsDisabled": False,
    "EnableContentDeletion": False, "EnableContentDeletionFromFolders": [],
    "EnableCollectionManagement": False, "EnableSubtitleManagement": False,
    "EnableLyricManagement": False, "EnableContentDownloading": False,
    "EnablePublicSharing": False, "EnableRemoteControlOfOtherUsers": False,
    "EnableLiveTvManagement": False, "EnableMediaConversion": False,
    "EnableSharedDeviceControl": False, "EnableMediaPlayback": True,
}


class _Manual(SafetyError):
    """Only fixed, installer-written messages belong in this exception."""


def _guard(c):
    engine.linux_only()
    if os.geteuid() != 0:
        raise SafetyError("Account enrollment requires root inside the target Linux guest")
    validate_config(c)
    engine.stage_allowed(c, 2)


def _root_owned(path):
    """Production ownership rule, isolated from test runner privileges."""
    return os.name != "posix" or path.stat().st_uid == 0


def _private_json(path):
    text = private_read(path)
    if not _root_owned(path):
        raise SafetyError("Account enrollment files must be owned by root")
    try:
        value = codec.loads(text)
    except ValueError:
        raise SafetyError("Private account enrollment file is not valid JSON") from None
    if not isinstance(value, dict):
        raise SafetyError("Private account enrollment file has an invalid structure")
    return value


def _paths(c):
    root = no_symlinks(Path(c["install_root"]))
    marker = _private_json(root / ".samurai-install.json")
    if marker.get("deployment_id") != c["deployment_id"]:
        raise SafetyError("Account enrollment root belongs to another deployment")
    for part in ("state", "secrets", "appdata"):
        directory = no_symlinks(root / part)
        if not directory.is_dir():
            raise SafetyError("Prepare the managed installation before reserving accounts")
        if not _root_owned(directory):
            raise SafetyError("Account enrollment parent directories must be owned by root")
        if os.name == "posix" and part in {"state", "secrets"} and directory.stat().st_mode & 0o077:
            raise SafetyError("Account enrollment state and secret directories must be private to root")
    return root, root / "state" / "onboarding.json"


def _identity(path, *, directory=True):
    """Bind an owned path to its inode and reboot-stable backing filesystem.

    st_dev is retained for same-boot race checks and strict legacy receipts.
    It is not a persistent block-device identity: Linux can assign different
    major/minor numbers after a normal reboot.
    """
    path = no_symlinks(path)
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise SafetyError("Application configuration path is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise SafetyError("Application datastore path is not a regular file")
    result = {"device": info.st_dev, "inode": info.st_ino}
    if os.name == "posix":
        from .storage import mount_identity, persistent_identity
        mounted = mount_identity(path)
        # Anonymous/ephemeral mounts do not acquire cross-boot authority from a
        # reused source label such as "tmpfs" or "overlay".
        if mounted["fstype"] in {"ext4", "xfs", "btrfs", "nfs", "nfs4"}:
            mounted = persistent_identity(mounted)
            result["filesystem"] = {key: mounted[key] for key in ("target", "source", "fstype")}
        after = path.stat()
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise SafetyError("Application path changed identity during inspection")
    return result


def same_identity(saved, current):
    """Compare persistent receipts without silently rebinding legacy records."""
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return False
    if any(type(row.get(key)) is not int or row[key] < 0 for row in (saved, current) for key in ("device", "inode")):
        return False
    if saved["inode"] != current["inode"]:
        return False
    if "filesystem" not in saved:
        return saved["device"] == current["device"]
    for row in (saved, current):
        fs = row.get("filesystem")
        if (not isinstance(fs, dict) or set(fs) != {"target", "source", "fstype"}
                or any(not isinstance(value, str) or not value for value in fs.values())):
            return False
        if (not fs["target"].startswith("/") or fs["fstype"] not in {"ext4", "xfs", "btrfs", "nfs", "nfs4"}
                or (fs["fstype"] in {"ext4", "xfs", "btrfs"} and not fs["source"].startswith("UUID="))):
            return False
    return saved["filesystem"] == current["filesystem"]


def _load(c, *, allow_restored_directories=False):
    # Only the explicit reviewed restore tool may inspect a replaced directory.
    # Ordinary setup/reservation calls retain the original fail-closed guard.
    if type(allow_restored_directories) is not bool:
        raise SafetyError("Restored-directory inspection requires an explicit boolean")
    root, path = _paths(c)
    if not path.exists():
        return None
    value = _private_json(path)
    if (value.get("schema_version") != 1 or value.get("deployment_id") != c["deployment_id"]
            or value.get("host_id") != engine.host_identity()
            or value.get("bind_address") != c["bind_address"]
            or not isinstance(value.get("apps"), dict)):
        raise SafetyError("Account reservation belongs to another host, address, or deployment")
    for name, item in value["apps"].items():
        if name not in MEDIA_APPS or not isinstance(item, dict):
            raise SafetyError("Account reservation contains an unknown application")
        if (not allow_restored_directories and (item.get("eligible") is True or item.get("restore_verified") is True)
                and name in catalog.stage_endpoints(c, 2)):
            if not same_identity(item.get("directory"), _identity(root / "appdata" / name)):
                raise SafetyError("Reserved application directory was replaced; after restoring verified app data, run ./setup --restore-accounts. Empty or unverified directories cannot be rebound")
    return value


def _save(c, receipt):
    _, path = _paths(c)
    write_json(path, receipt, replace=path.exists())


def reserve_fresh_apps(c, stage=2, *, runner=run):
    """Record genuinely empty owned app directories before first Compose start.

    Existing Compose files/project containers make the entire stage ineligible.
    An existing reservation is validated and reused, never enlarged or refreshed.
    This function belongs inside the engine's deployment lock.
    """
    if stage != 2:
        raise SafetyError("Account reservation supports media stage 2 only")
    _guard(c)
    root, _ = _paths(c)
    existing = _load(c)
    if existing is not None:
        return existing
    containers = engine.docker(runner, "ps", "--all", "--filter",
        "label=com.docker.compose.project=" + naming.project_name(c, 2), "--format", "{{.ID}}").stdout.strip()
    legacy = bool(containers) or no_symlinks(engine.compose_path(c, 2)).exists()
    receipt = {"schema_version": 1, "deployment_id": c["deployment_id"],
               "host_id": engine.host_identity(), "bind_address": c["bind_address"], "apps": {}}
    for name in catalog.stage_endpoints(c, 2):
        path = no_symlinks(root / "appdata" / name)
        identity = _identity(path) if path.exists() else None
        owned = identity is not None and (os.name != "posix" or path.stat().st_uid in {0, c["puid"]})
        empty = owned and not any(path.iterdir())
        eligible = not legacy and empty and name in SUPPORTED
        receipt["apps"][name] = {"eligible": bool(eligible), "directory": identity,
            "state": "reserved" if eligible else "manual",
            "reason": "New empty managed application directory" if eligible else
                      "Existing application state or browser enrollment required"}
    _save(c, receipt)
    return receipt


def eligible_apps(c):
    """Read-only GUI gate; legacy installations do not need password prompts."""
    _guard(c)
    receipt = _load(c)
    return [name for name in SUPPORTED if name in catalog.stage_endpoints(c, 2)
            and receipt and receipt["apps"].get(name, {}).get("eligible") is True]


def pending_apps(c):
    """Reserved first enrollment only, excluding already verified accounts."""
    _guard(c)
    receipt = _load(c)
    return [name for name in SUPPORTED if name in catalog.stage_endpoints(c, 2)
            and receipt and receipt['apps'].get(name, {}).get('eligible') is True
            and receipt['apps'][name].get('state') != 'verified']


def check_targets(c, apps, runner=run):
    """Prove account requests address local ports owned by these containers."""
    interfaces = codec.loads(runner(["ip", "-j", "address", "show"]).stdout)
    if (not isinstance(interfaces, list) or any(not isinstance(iface, dict)
            or not isinstance(iface.get("addr_info", []), list)
            or any(not isinstance(item, dict) for item in iface.get("addr_info", [])) for iface in interfaces)):
        raise SafetyError("Cannot verify the local interface inventory for account enrollment")
    if c["bind_address"] not in {item.get("local") for iface in interfaces for item in iface.get("addr_info", [])}:
        raise SafetyError("The saved media address is not present on this guest; no account request was sent")
    engine.check_docker(runner)
    for app in apps:
        if app not in SUPPORTED:
            raise SafetyError("Unsupported application account target")
        ids = engine.docker(runner, "ps", "--filter", "status=running", "--filter",
            "label=com.docker.compose.project=" + naming.project_name(c, 2), "--filter",
            "label=com.docker.compose.service=" + app, "--format", "{{.ID}}").stdout.split()
        if len(ids) != 1:
            raise SafetyError("An account target is not a unique running deployment service")
        labels = codec.loads(engine.docker(runner, "inspect", ids[0], "--format", "{{json .Config.Labels}}").stdout)
        if (not isinstance(labels, dict) or labels.get("systems.samarie.deployment") != c["deployment_id"]
                or labels.get("systems.samarie.stage") != "2"
                or labels.get("com.docker.compose.project") != naming.project_name(c, 2)
                or labels.get("com.docker.compose.service") != app):
            raise SafetyError("Account target container belongs to another installation")
        ports = codec.loads(engine.docker(runner, "inspect", ids[0], "--format", "{{json .NetworkSettings.Ports}}").stdout)
        port = str(catalog.PORTS[app])
        bindings = ports.get(port + "/tcp") if isinstance(ports, dict) else None
        if (not isinstance(bindings, list) or not bindings or any(not isinstance(binding, dict)
                or binding.get("HostIp") != c["bind_address"] or binding.get("HostPort") != port for binding in bindings)):
            raise SafetyError("Account endpoint does not match the owned application's published LAN port")
        mounts = codec.loads(engine.docker(runner, "inspect", ids[0], "--format", "{{json .Mounts}}").stdout)
        destination = "/app/config" if app == "seerr" else "/config"
        config_mounts = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == destination] if isinstance(mounts, list) else []
        if (len(config_mounts) != 1 or config_mounts[0].get("Type") != "bind"
                or config_mounts[0].get("Source") != str(Path(c["install_root"]) / "appdata" / app)):
            raise SafetyError("Account target does not use its reserved application directory")
        if app in {"jellyfin", "seerr"}:
            if any(not isinstance(item, dict) or not isinstance(item.get("Destination"), str)
                   or item["Destination"].startswith(destination + "/") for item in mounts):
                raise SafetyError("Nested application mounts prevent account datastore attestation")
            # A stale default SQLite file cannot attest the active account
            # datastore when a container points at a different directory/DB.
            runtime = codec.loads(engine.docker(runner, "inspect", ids[0], "--format", "{{json .Config}}").stdout)
            if not isinstance(runtime, dict) or not isinstance(runtime.get("Env"), list) or any(not isinstance(item, str) or "=" not in item for item in runtime["Env"]):
                raise SafetyError("Cannot attest the application's active account datastore")
            selected = {}
            for item in runtime["Env"]:
                key, value = item.split("=", 1)
                if key in {"JELLYFIN_DATA_DIR", "JELLYFIN_CONFIG_DIR", "DB_TYPE", "CONFIG_DIRECTORY", "NODE_ENV"}:
                    if key in selected:
                        raise SafetyError("Account datastore environment is ambiguous")
                    selected[key] = value
            if app == "jellyfin":
                if (selected.get("JELLYFIN_DATA_DIR") != "/config" or selected.get("JELLYFIN_CONFIG_DIR") != "/config/config"
                        or runtime.get("Cmd") not in (None, []) or runtime.get("Entrypoint") != ["/jellyfin/jellyfin"]):
                    raise SafetyError("Jellyfin uses a custom account datastore or startup command; existing accounts were preserved")
            elif (selected.get("DB_TYPE", "sqlite") not in {"", "sqlite"}
                    or selected.get("CONFIG_DIRECTORY", "/app/config") not in {"", "/app/config"}
                    or selected.get("NODE_ENV") != "production" or runtime.get("WorkingDir") != "/app"
                    or runtime.get("Cmd") != ["npm", "start"] or runtime.get("Entrypoint") != ["docker-entrypoint.sh"]):
                raise SafetyError("Seerr uses a custom account datastore or startup command; existing accounts were preserved")


def _credentials(c, supplied, *, persist=True):
    root, _ = _paths(c)
    path = root / "secrets" / "onboarding_credentials.json"
    saved = _private_json(path) if path.exists() else None
    if supplied is None:
        supplied = saved
    if not isinstance(supplied, dict) or set(supplied) != set(CREDENTIAL_FIELDS):
        raise SafetyError("Provide a separate administrator and viewer username/password pair")
    value = {name: supplied[name] for name in CREDENTIAL_FIELDS}
    for name in ("admin_username", "viewer_username"):
        if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}", value[name]):
            raise SafetyError("Account names must be 1-64 letters, digits, dot, underscore, @, + or hyphen")
    for name in ("admin_password", "viewer_password"):
        password = value[name]
        if (not isinstance(password, str) or not 12 <= len(password) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in password)):
            raise SafetyError("Use account passwords of 12-256 characters without control characters")
    if (value["admin_username"].casefold() == value["viewer_username"].casefold()
            or value["admin_password"] == value["viewer_password"]):
        raise SafetyError("Administrator and viewer accounts must have distinct names and passwords")
    if c["features"].get("usenet") and not value["admin_password"].strip("*"):
        raise SafetyError("SABnzbd cannot store an all-asterisk password; choose a different application administrator password")
    if saved is not None and saved != value:
        raise SafetyError("Saved enrollment credentials differ; refusing account rotation or replacement")
    if saved is None and persist:
        # Publish the complete private pair before the first network operation.
        write_json(path, value)
    return value


@contextmanager
def _lock(c):
    root, _ = _paths(c)
    path = root / "state" / "onboarding.lock"
    try:
        fd = os.open(no_symlinks(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SafetyError("Another account enrollment is active or interrupted; inspect state/onboarding.lock before recovery") from None
    try:
        with os.fdopen(fd, "w") as stream:
            codec.dump({"pid": os.getpid(), "host_id": engine.host_identity()}, stream)
        yield
    finally:
        path.unlink()


@dataclass(frozen=True)
class HttpResult:
    status: int
    data: object = None
    location: str = ""


class HttpSession:
    """Dedicated per-service cookie jar, no proxies, redirects or URL secrets."""
    def __init__(self, base):
        parsed = urllib.parse.urlsplit(base)
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            raise SafetyError("Enrollment HTTP target must be an explicit private IPv4 address") from None
        if (parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or address.version != 4
                or not (address.is_private or address.is_loopback) or address.is_unspecified):
            raise SafetyError("Enrollment HTTP target must be a configured private service address")
        self.base = base.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), engine.NoRedirects(),
                                                  urllib.request.HTTPCookieProcessor(self.cookies))

    def has_cookies(self):
        return bool(list(self.cookies))

    def send(self, method, path, *, headers=None, json=None, form=None, timeout=20):
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise SafetyError("Enrollment session accepts service-local paths without query parameters only")
        data, values = None, dict(headers or {})
        if json is not None and form is not None:
            raise SafetyError("Enrollment request cannot contain both JSON and form data")
        if json is not None:
            data, values["Content-Type"] = codec.dumps(json).encode(), "application/json"
        if form is not None:
            data, values["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=data, headers=values, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                payload = response.read(2 * 1024 * 1024 + 1)
                if len(payload) > 2 * 1024 * 1024:
                    raise SafetyError("Enrollment HTTP response exceeded the safe size limit")
                try:
                    content = codec.loads(payload) if payload else None
                except (ValueError, UnicodeError):
                    content = payload.decode("utf-8", errors="replace")
                return HttpResult(response.code, content, response.headers.get("Location", ""))
        except (OSError, ValueError, urllib.error.URLError):
            raise SafetyError("Enrollment HTTP check failed; inspect the service locally") from None


def _local_location(base, location):
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base + "/", location))
    target = urllib.parse.urlsplit(base)
    if (parsed.scheme, parsed.netloc) != (target.scheme, target.netloc):
        raise _Manual("Unexpected login redirect; finish this application's login manually")
    return parsed.path


def _denied(result, base):
    return result.status in {401, 403} or (result.status in {302, 303}
        and bool(result.location) and _local_location(base, result.location).rstrip("/") == "/login")


class _Enrollment:
    def __init__(self, c, credentials, receipt, request, session_factory, secret_resolver):
        self.c, self.credentials, self.receipt = c, credentials, receipt
        self.request, self.session_factory = request, session_factory
        self.secret_resolver = secret_resolver or engine.secret_resolver(c)
        self.endpoints = catalog.stage_endpoints(c, 2)

    def save(self):
        _save(self.c, self.receipt)

    def call(self, method, app, path, *, headers=None, json=None):
        # Exceptions from transports/vendors can contain the complete password.
        try:
            return self.request(method, self.endpoints[app] + path, headers=headers or {}, json=json, timeout=20)
        except Exception:
            raise SafetyError("Account API request failed; inspect the application locally") from None

    def mark(self, app, **values):
        self.receipt["apps"][app].update(values)
        self.save()

    def login_jellyfin(self, username, password, *, administrator):
        auth = self.call("POST", "jellyfin", "/Users/AuthenticateByName",
            json={"Username": username, "Pw": password}, headers={"Authorization":
                'MediaBrowser Client="Samurai Stack", Device="Stack Installer", DeviceId="samurai-stack", Version="0.1"'})
        if not isinstance(auth, dict) or not isinstance(auth.get("User"), dict):
            raise _Manual("Jellyfin login could not be verified; existing accounts were preserved")
        user, token = auth["User"], auth.get("AccessToken")
        if (user.get("Name") != username or user.get("Policy", {}).get("IsAdministrator") is not administrator
                or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{16,4096}", token)):
            raise _Manual("Jellyfin account identity or role differs; resolve it in the application")
        _user_id(user.get("Id"))
        return user, jellyfin_headers(token)

    def jellyfin(self):
        app, creds = "jellyfin", self.credentials
        entry = self.receipt["apps"][app]
        public = self.call("GET", app, "/System/Info/Public")
        if not isinstance(public, dict) or not public.get("Id"):
            raise _Manual("Jellyfin did not return a server identity")
        complete = public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted"))
        if type(complete) is not bool:
            raise _Manual("Jellyfin startup state is unknown; finish enrollment in its browser")
        if entry.get("server_id") not in {None, public["Id"]}:
            raise _Manual("Jellyfin server identity changed; existing accounts were preserved")
        if not entry.get("admin_intent"):
            if complete:
                raise _Manual("Jellyfin already has completed setup; use its existing administrator")
            # Current UserDto password flags are obsolete and always true;
            # Users/Public can omit the hidden seed account. Prove an actual
            # empty-password seed login instead of guessing from those fields.
            seed = self.call("GET", app, "/Startup/User")
            if not isinstance(seed, dict) or not isinstance(seed.get("Name"), str) or not seed["Name"]:
                raise _Manual("Jellyfin did not return its startup account name")
            try:
                initial, _ = self.login_jellyfin(seed["Name"], "", administrator=True)
            except SafetyError:
                raise _Manual("Jellyfin startup account is not an empty-password seed; no account was changed") from None
            if (entry.get("claim_seed_id") not in {None, initial["Id"]}
                    or entry.get("claim_server_id") not in {None, public["Id"]}):
                raise _Manual("Jellyfin account-absence proof changed; no account was modified")
            self.mark(app, server_id=public["Id"], admin_id=_user_id(initial.get("Id")),
                      seed_name=seed["Name"], admin_intent=True, state="intent")
        admin = admin_headers = None
        if not complete:
            if entry.get("admin_write_intent"):
                try:
                    admin, admin_headers = self.login_jellyfin(creds["admin_username"], creds["admin_password"], administrator=True)
                except SafetyError:
                    # A recorded intent is not proof that its network write
                    # reached the application. Only another real empty login
                    # permits retrying a write that may not have happened.
                    pass
            if admin is None:
                seed = self.call("GET", app, "/Startup/User")
                if not isinstance(seed, dict) or seed.get("Name") not in {entry.get("seed_name"), creds["admin_username"]}:
                    raise _Manual("Jellyfin startup account name changed; resolve enrollment manually")
                try:
                    initial, _ = self.login_jellyfin(seed["Name"], "", administrator=True)
                except SafetyError:
                    raise _Manual("Jellyfin startup password differs; no account was reset") from None
                if initial["Id"] != entry["admin_id"]:
                    raise _Manual("Jellyfin startup account ID changed; no account was modified")
                self.mark(app, admin_write_intent=True)
                # This is first-run setup, not a password-reset API. Older
                # pinned servers do not enforce an existing-password guard;
                # the fresh reservation and real empty-password proof above
                # are mandatory, even when a newer upstream server adds one.
                self.call("POST", app, "/Startup/User", json={"Name": creds["admin_username"],
                                                              "Password": creds["admin_password"]})
        if admin is None:
            admin, admin_headers = self.login_jellyfin(creds["admin_username"], creds["admin_password"], administrator=True)
        if admin["Id"] != entry["admin_id"]:
            raise _Manual("Jellyfin administrator ID changed; no password was reset")
        if not complete:
            self.call("POST", app, "/Startup/Complete", headers=admin_headers)
            done = self.call("GET", app, "/System/Info/Public")
            if done.get("StartupWizardCompleted", done.get("IsStartupWizardCompleted")) is not True:
                raise _Manual("Jellyfin startup completion was not confirmed")
        info = self.call("GET", app, "/System/Info", headers=admin_headers)
        if info.get("Id") != entry["server_id"]:
            raise _Manual("Jellyfin authenticated server identity differs")
        network = self.call("GET", app, "/System/Configuration/network", headers=admin_headers)
        if not isinstance(network, dict):
            raise _Manual("Jellyfin network configuration readback is invalid")
        # v10.11.8 marks EnableUPnP and the startup DTO's automatic-mapping
        # field obsolete, with UPnP default false. Do not POST ignored settings
        # or change a completed server's networking; reject a present enabled
        # flag and leave its review to the operator instead.
        for key in ("EnableUPnP", "EnableAutomaticPortMapping"):
            if key in network and network[key] is not False:
                raise _Manual("Jellyfin reports automatic port mapping enabled or unknown; review its network settings")
        self.mark(app, admin_verified=True)
        self.viewer(admin_headers, admin["Id"])
        self.jellyfin_key(admin_headers)
        self.mark(app, state="verified")
        return "Administrator and restricted viewer logins verified; viewer admin access rejected"

    def viewer(self, admin_headers, admin_id):
        app, creds = "jellyfin", self.credentials
        entry = self.receipt["apps"][app]
        users = self.call("GET", app, "/Users", headers=admin_headers)
        if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
            raise _Manual("Jellyfin user collection is invalid")
        matches = [user for user in users if str(user.get("Name", "")).casefold() == creds["viewer_username"].casefold()]
        if len(matches) > 1 or (matches and not entry.get("viewer_intent")):
            raise _Manual("The requested Jellyfin viewer already exists; it was not modified")
        if not entry.get("viewer_intent"):
            self.mark(app, viewer_intent=True)
        if not matches:
            if entry.get("viewer_id"):
                raise _Manual("The recorded Jellyfin viewer is missing; no replacement was created")
            user = self.call("POST", app, "/Users/New", headers=admin_headers,
                             json={"Name": creds["viewer_username"], "Password": creds["viewer_password"]})
        else:
            user = matches[0]
        if not isinstance(user, dict) or user.get("Name") != creds["viewer_username"]:
            raise _Manual("Jellyfin viewer identity is invalid")
        viewer_id = _user_id(user.get("Id"))
        if viewer_id == admin_id or entry.get("viewer_id") not in {None, viewer_id}:
            raise _Manual("Jellyfin viewer ID differs; no account policy was changed")
        # Prove the saved password before restricting an interrupted own create.
        verified, viewer_headers = self.login_jellyfin(creds["viewer_username"], creds["viewer_password"], administrator=False)
        if verified["Id"] != viewer_id:
            raise _Manual("Jellyfin viewer login returned a different account")
        self.mark(app, viewer_id=viewer_id)
        policy = user.get("Policy")
        if not isinstance(policy, dict) or policy.get("IsAdministrator") is not False:
            raise _Manual("Jellyfin viewer is not an ordinary account; resolve it manually")
        desired = {**policy, **VIEWER_POLICY}
        if any(policy.get(key) != value for key, value in VIEWER_POLICY.items()):
            if entry.get("viewer_policy_verified"):
                raise _Manual("The Jellyfin viewer policy changed after enrollment; it was not overwritten")
            self.mark(app, viewer_policy_intent=True)
            self.call("POST", app, f"/Users/{viewer_id}/Policy", headers=admin_headers, json=desired)
        saved = self.call("GET", app, "/Users/" + viewer_id, headers=admin_headers)
        if (saved.get("Id") != viewer_id or saved.get("Name") != creds["viewer_username"]
                or any(saved.get("Policy", {}).get(key) != value for key, value in VIEWER_POLICY.items())):
            raise _Manual("Jellyfin viewer restrictions were not confirmed")
        verified, viewer_headers = self.login_jellyfin(creds["viewer_username"], creds["viewer_password"], administrator=False)
        session = self.session_factory(self.endpoints[app])
        elevated = "/System/Configuration/MetadataOptions/Default"
        if session.send("GET", elevated, headers=admin_headers).status != 200:
            raise _Manual("Jellyfin administrator authorization could not be confirmed")
        if session.send("GET", elevated, headers=viewer_headers).status not in {401, 403}:
            raise _Manual("Jellyfin viewer unexpectedly reached an administrator endpoint")
        if self.session_factory(self.endpoints[app]).send("GET", elevated).status not in {401, 403}:
            raise _Manual("Jellyfin administrator endpoint unexpectedly allowed anonymous access")
        self.mark(app, viewer_policy_verified=True)

    def jellyfin_key(self, headers):
        app, name = "jellyfin", "Samurai Stack " + self.c["deployment_id"]
        entry = self.receipt["apps"][app]
        keys = self.call("GET", app, "/Auth/Keys", headers=headers)
        rows = keys.get("Items") if isinstance(keys, dict) else None
        if not isinstance(rows, list):
            raise _Manual("Jellyfin API-key collection is invalid")
        managed = [key for key in rows if isinstance(key, dict) and key.get("AppName") == name]
        if managed and not entry.get("api_key_intent"):
            raise _Manual("A deployment-named Jellyfin API key already exists; it was not adopted or replaced")
        if not managed:
            if entry.get("api_key_verified"):
                raise _Manual("The recorded Jellyfin API key is missing; no replacement was created")
            self.mark(app, api_key_intent=True)
            self.call("POST", app, "/Auth/Keys?" + urllib.parse.urlencode({"app": name}), headers=headers)
            rows = self.call("GET", app, "/Auth/Keys", headers=headers).get("Items", [])
            managed = [key for key in rows if isinstance(key, dict) and key.get("AppName") == name]
        if (len(managed) != 1 or not isinstance(managed[0].get("AccessToken"), str)
                or not re.fullmatch(r"[A-Za-z0-9._~-]{16,4096}", managed[0]["AccessToken"])):
            raise _Manual("Jellyfin API-key identity is ambiguous; resolve it in its dashboard")
        path = engine.secret_path(self.c, "jellyfin_api_key")
        key = managed[0]["AccessToken"]
        if path.exists():
            if private_read(path).rstrip("\r\n") != key:
                raise _Manual("Saved Jellyfin API key differs; no key or private file was replaced")
        else:
            atomic_write(path, key + "\n")
        info = self.call("GET", app, "/System/Info", headers=jellyfin_headers(key))
        if info.get("Id") != entry["server_id"]:
            raise _Manual("Jellyfin API-key authentication could not be confirmed")
        self.mark(app, api_key_verified=True)

    def starr(self, app):
        entry, creds = self.receipt["apps"][app], self.credentials
        prefix = "/api/v1" if app == "prowlarr" else "/api/v3"
        headers = {"X-Api-Key": self.secret_resolver(app + "_api_key")}
        path = prefix + "/config/host"
        current = self.call("GET", app, path, headers=headers)
        if (not isinstance(current, dict) or not {"username", "password"} <= set(current)
                or any(current[key] is not None and not isinstance(current[key], str) for key in ("username", "password"))):
            raise _Manual("Starr host settings are invalid; finish enrollment in its browser")
        blank = current.get("username") in {None, ""} and current.get("password") in {None, ""}
        if not entry.get("admin_intent"):
            if not blank:
                raise _Manual("This application already has account credentials; they were not changed")
            self.mark(app, admin_intent=True, state="intent")
        if blank:
            if entry.get("state") == "verified":
                raise _Manual("The enrolled account is missing; no replacement was created")
            identifier = current.get("id", 1)
            if type(identifier) is not int or identifier < 1:
                raise _Manual("Starr host-settings identity is invalid")
            self.call("PUT", app, path + "/" + str(identifier), headers=headers,
                      json={**current, "authenticationMethod": "forms", "authenticationRequired": "enabled",
                            "username": creds["admin_username"], "password": creds["admin_password"],
                            "passwordConfirmation": creds["admin_password"]})
        saved = self.call("GET", app, path, headers=headers)
        if (saved.get("username") != creds["admin_username"] or not saved.get("password")
                or saved.get("authenticationMethod") != "forms" or saved.get("authenticationRequired") != "enabled"):
            raise _Manual("Starr account settings differ; no existing credential was replaced")
        self.starr_login(app)
        self.mark(app, state="verified")
        return "Administrator form login verified; anonymous and incorrect-password access rejected"

    def inspect_existing(self, app):
        """Verify existing accounts through local API keys without replacing them."""
        if app == "jellyfin":
            headers = jellyfin_headers(self.secret_resolver("jellyfin_api_key"))
            public = self.call("GET", app, "/System/Info/Public")
            info = self.call("GET", app, "/System/Info", headers=headers)
            users = self.call("GET", app, "/Users", headers=headers)
            if (public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted")) is not True
                    or not info.get("Id") or info.get("Id") != public.get("Id")
                    or not isinstance(users, list) or not any(isinstance(user, dict)
                        and user.get("Policy", {}).get("IsAdministrator") is True for user in users)):
                raise _Manual("Jellyfin existing administrator/API access is not ready; accounts were preserved")
            return "Existing Jellyfin administrator API access verified; accounts and passwords preserved"
        if app in {"sonarr", "radarr", "prowlarr"}:
            prefix = "/api/v1" if app == "prowlarr" else "/api/v3"
            headers = {"X-Api-Key": self.secret_resolver(app + "_api_key")}
            current = self.call("GET", app, prefix + "/config/host", headers=headers)
            if (not isinstance(current, dict) or not current.get("username") or not current.get("password")
                    or current.get("authenticationMethod") not in {"forms", "basic"}
                    or current.get("authenticationRequired") != "enabled"):
                raise _Manual("Existing Starr UI authentication is incomplete; no password was replaced")
            if not _denied(self.session_factory(self.endpoints[app]).send("GET", "/"), self.endpoints[app]):
                raise _Manual("Existing Starr UI unexpectedly allowed anonymous access")
            return "Existing administrator authentication and API access verified; accounts and passwords preserved"
        if app == "bazarr":
            from .enroll_bazarr import inspect_existing
        elif app == "sabnzbd":
            from .enroll_sabnzbd import inspect_existing
        elif app == "seerr":
            from .enroll_seerr import inspect_existing
        else:
            raise SafetyError("Unsupported existing account inspection")
        return inspect_existing(self)

    def starr_login(self, app):
        base, creds = self.endpoints[app], self.credentials
        session = self.session_factory(base)
        if not _denied(session.send("GET", "/"), base):
            raise _Manual("Application UI unexpectedly allowed anonymous access")
        login = session.send("POST", "/login", form={"username": creds["admin_username"],
                                                    "password": creds["admin_password"]})
        if (login.status not in {302, 303} or not login.location
                or _local_location(base, login.location).rstrip("/") == "/login"
                or not session.has_cookies() or session.send("GET", "/").status != 200):
            raise _Manual("Application administrator form login could not be verified")
        bad = self.session_factory(base)
        bad.send("POST", "/login", form={"username": creds["admin_username"], "password": secrets.token_urlsafe(36)})
        if not _denied(bad.send("GET", "/"), base):
            raise _Manual("Application UI unexpectedly accepted an incorrect password")


def _user_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise _Manual("Jellyfin returned an invalid account identifier")
    return value


def _legacy_seed(operation):
    """Prove a solitary passwordless startup administrator, not a hidden user list."""
    public = operation.call("GET", "jellyfin", "/System/Info/Public")
    if (not isinstance(public, dict) or not public.get("Id")
            or public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted")) is not False):
        return None
    startup = operation.call("GET", "jellyfin", "/Startup/User")
    if not isinstance(startup, dict) or not isinstance(startup.get("Name"), str) or not startup["Name"]:
        return None
    from .enroll_jellyfin_existing import passwordless_seed
    local_id = passwordless_seed(operation.c, startup["Name"])
    if local_id is None:
        return None
    try:
        seed, headers = operation.login_jellyfin(startup["Name"], "", administrator=True)
    except SafetyError:
        return None
    users = operation.call("GET", "jellyfin", "/Users", headers=headers)
    libraries = operation.call("GET", "jellyfin", "/Library/VirtualFolders", headers=headers)
    if (not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict)
            or seed["Id"].replace("-", "").lower() != local_id
            or users[0].get("Id") != seed["Id"] or users[0].get("Name") != startup["Name"]
            or users[0].get("Policy", {}).get("IsAdministrator") is not True
            or not isinstance(libraries, list) or libraries):
        return None
    return {"claim_seed_id": seed["Id"], "claim_server_id": public["Id"]}


def claim_unconfigured(c, *, request=engine.request, session_factory=HttpSession, secret_resolver=None, runner=run):
    """Upgrade an old source install by proving account absence, never by reset.

    Existing configured accounts remain ineligible for writes. Backend proofs
    run only against owned local ports/mounts, under the same deployment lock.
    Unknown schemas and partial credentials stay unchanged and are reported.
    """
    from . import enrollment_preflight, enroll_seerr
    _guard(c)
    with engine.deployment_lock(c), _lock(c):
        root, _ = _paths(c)
        enabled = catalog.stage_endpoints(c, 2)
        check_targets(c, enabled, runner)
        receipt = _load(c)
        if receipt is None:
            receipt = {"schema_version": 1, "deployment_id": c["deployment_id"],
                       "host_id": engine.host_identity(), "bind_address": c["bind_address"], "apps": {}}
        for app in enabled:
            if app not in receipt["apps"]:
                receipt["apps"][app] = {"eligible": False, "directory": _identity(root / "appdata" / app),
                                         "state": "manual", "reason": "Existing accounts are preserved"}
        _save(c, receipt)
        operation = _Enrollment(c, None, receipt, request, session_factory, secret_resolver)
        result = {"claimed": [], "preserved": [], "errors": []}
        for app in enabled:
            if receipt["apps"][app].get("eligible") is True:
                continue
            if receipt["apps"][app].get("state") == "verified":
                # A verified/restored account must never regain first-owner
                # authority when its backend later disappears or resets.
                result["preserved"].append(app)
                continue
            try:
                proof = _legacy_seed(operation) if app == "jellyfin" else (
                    enroll_seerr.claimable(operation) if app == "seerr" else enrollment_preflight.claimable(operation, app))
                if proof:
                    receipt["apps"][app].update(eligible=True, state="reserved", reservation_kind="account-absence",
                        directory=_identity(root / "appdata" / app), reason="Authenticated backend account-absence proof")
                    if isinstance(proof, dict):
                        receipt["apps"][app].update(proof)
                    _save(c, receipt)
                    result["claimed"].append(app)
                else:
                    result["preserved"].append(app)
            except Exception:
                result["errors"].append({"name": app + ":accounts", "error":
                    "Account absence could not be verified; existing accounts and configuration were preserved"})
        return result


def requires_credentials(c):
    """Whether setup needs a hidden pair, without attempting any password login."""
    if eligible_apps(c):
        return True
    try:
        engine.secret_resolver(c)("jellyfin_api_key")
        return False
    except (SafetyError, OSError, ValueError):
        return True


def _existing_jellyfin(operation, entry):
    if entry.get("legacy_connection_verified") or entry.get("eligible") is not True:
        return True
    if entry.get("admin_intent"):
        return False
    public = operation.call("GET", "jellyfin", "/System/Info/Public")
    complete = public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted")) if isinstance(public, dict) else None
    if complete is True:
        return True
    if complete is not False:
        raise _Manual("Jellyfin startup state is unknown; existing accounts were preserved")
    startup = operation.call("GET", "jellyfin", "/Startup/User")
    if not isinstance(startup, dict) or not isinstance(startup.get("Name"), str):
        raise _Manual("Jellyfin startup account identity is unknown")
    from .enroll_jellyfin_existing import passwordless_seed
    return passwordless_seed(operation.c, startup["Name"]) is None


def onboarding(c, credentials=None, *, request=engine.request, session_factory=HttpSession, secret_resolver=None, runner=run):
    """Enroll reserved fresh apps and return a public-safe per-app report.

    Credentials are a dict with admin_username/admin_password and
    viewer_username/viewer_password. Passing None resumes the exact saved pair.
    Unsupported/legacy apps stay manual. Reports never contain vendor output,
    exception text, account passwords, API keys, response bodies or session IDs.
    """
    _guard(c)
    report = {"stage": 2, "mode": "onboard", "status": "pending", "ok": False,
              "checks": [], "pending": [], "errors": []}
    with engine.deployment_lock(c), _lock(c):
        receipt = _load(c)
        enabled = catalog.stage_endpoints(c, 2)
        eligible = [name for name in SUPPORTED if name in enabled
                    and receipt and receipt["apps"].get(name, {}).get("eligible") is True]
        check_targets(c, enabled, runner)
        creds = None
        operation = None
        inspection = _Enrollment(c, None, receipt or {}, request, session_factory, secret_resolver)
        existing_jellyfin = _existing_jellyfin(inspection, (receipt or {}).get("apps", {}).get("jellyfin", {}))
        if existing_jellyfin and "jellyfin" in eligible:
            # Preserve the historical reservation for audit, but never use it
            # as authority to rename/reset an owner initialized by a person.
            eligible.remove("jellyfin")
        if eligible or credentials is not None:
            if receipt is None:
                raise SafetyError("Run guided account inspection before enrolling an existing installation")
            if existing_jellyfin and credentials is not None:
                preview = _credentials(c, credentials, persist=False)
                probe = _Enrollment(c, preview, receipt, request, session_factory, secret_resolver)
                # Authenticate an existing administrator before freezing the
                # common pair or creating any other account. A typo must not
                # initialize half the installation with the wrong identity.
                _, headers = probe.login_jellyfin(preview["admin_username"], preview["admin_password"], administrator=True)
                if "seerr" in eligible:
                    users = probe.call("GET", "jellyfin", "/Users", headers=headers)
                    if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
                        raise _Manual("Jellyfin existing users could not be verified; no account was changed")
                    matches = [user for user in users if str(user.get("Name", "")).casefold() == preview["viewer_username"].casefold()]
                    if len(matches) > 1:
                        raise _Manual("Jellyfin viewer name is ambiguous; no account was changed")
                    if matches:
                        viewer, _ = probe.login_jellyfin(preview["viewer_username"], preview["viewer_password"], administrator=False)
                        if viewer["Id"] != matches[0].get("Id"):
                            raise _Manual("Jellyfin viewer identity differs; no account was changed")
            creds = _credentials(c, credentials)
            desired = {name: creds[name] for name in ("admin_username", "viewer_username")}
            if receipt.get("identity") not in (None, desired):
                raise SafetyError("Reserved account identity differs; refusing account replacement")
            receipt["identity"] = desired
            _save(c, receipt)
            operation = _Enrollment(c, creds, receipt, request, session_factory, secret_resolver)
            if existing_jellyfin:
                from .enroll_jellyfin_existing import connect
                try:
                    connect(operation, need_viewer="seerr" in eligible)
                except _Manual as error:
                    report["pending"].append({"name": "jellyfin:accounts", "action": str(error)})
                    return report
                except Exception:
                    report["errors"].append({"name": "jellyfin:accounts", "error":
                        "Existing Jellyfin login/API connection could not be verified; its accounts were preserved"})
                    report["status"] = "failed"
                    return report
        existing = _Enrollment(c, None, receipt or {}, request, session_factory, secret_resolver)
        for app in enabled:
            if app in eligible:
                continue
            try:
                detail = existing.inspect_existing(app)
                report["checks"].append({"name": app + ":accounts", "status": "passed", "detail": detail})
            except _Manual as error:
                report["pending"].append({"name": app + ":accounts", "action": str(error)})
            except Exception:
                report["errors"].append({"name": app + ":accounts", "error":
                    "Existing account/API access could not be verified; no account or password was changed"})
        if eligible:
            for app in eligible:
                name = app + ":accounts"
                try:
                    if app == "jellyfin":
                        detail = operation.jellyfin()
                    elif app in {"sonarr", "radarr", "prowlarr"}:
                        detail = operation.starr(app)
                    else:
                        if app == "bazarr":
                            from .enroll_bazarr import enroll
                        elif app == "sabnzbd":
                            from .enroll_sabnzbd import enroll
                        else:
                            from .enroll_seerr import enroll
                        detail = enroll(operation)
                    report["checks"].append({"name": name, "status": "passed", "detail": detail})
                except _Manual as error:
                    report["pending"].append({"name": name, "action": str(error)})
                except Exception:
                    report["errors"].append({"name": name, "error":
                        "Account enrollment or verification failed; inspect the service locally. Existing state and saved credentials were preserved"})
        if report["errors"]:
            report["status"] = "failed"
        elif not report["pending"]:
            report.update(status="verified", ok=True)
    return report
