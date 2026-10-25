"""Pinned Seerr 3.5.1 enrollment, with no owner-recovery or password-reset path.

The caller holds the deployment/account locks and proves the local service and
fresh appdata reservation. Do not run a browser wizard concurrently. Public
initialized=false alone never proves that Seerr has no existing owner.

Contracts: seerr-team/seerr v3.5.1 server/routes/auth.ts,
server/routes/settings/index.ts and server/middleware/auth.ts. The generated
settings API key permits a read-only owner probe before the first account write.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import catalog, engine
from .integrations import jellyfin_headers
from .safety import SafetyError, atomic_write, no_symlinks, private_read

JELLYFIN, NOT_CONFIGURED, ADMIN, REQUEST = 2, 4, 2, 32
_KEY = re.compile(r"[A-Za-z0-9+/=_-]{16,512}\Z")


def _manual(message):
    from .onboarding import _Manual
    raise _Manual(message)


def _settings(c):
    """Read app-owned JSON, not a database or arbitrary operator file."""
    path = no_symlinks(Path(c["install_root"]) / "appdata" / "seerr" / "settings.json")
    try:
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024
                or (os.name == "posix" and info.st_uid not in {0, c["puid"]})):
            raise SafetyError("Seerr settings must be a bounded regular application-owned JSON file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise SafetyError("Seerr generated settings are missing or invalid; start the owned application first") from None
    if not isinstance(value, dict) or not isinstance(value.get("main"), dict):
        raise SafetyError("Seerr generated settings structure is invalid")
    return value


def _valid_key(value):
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def resolve_api_key(c):
    """Read-only generated-key discovery; never rotate or overwrite a secret."""
    key = _settings(c)["main"].get("apiKey")
    if not _valid_key(key):
        raise SafetyError("Seerr has not generated a valid application API key")
    path = engine.secret_path(c, "seerr_api_key")
    if path.exists():
        if private_read(path).rstrip("\r\n") != key:
            raise SafetyError("Saved and generated Seerr API keys differ; neither was replaced")
    return key


def _send(operation, session, method, path, *, body=None, key=None):
    base = operation.endpoints["seerr"]
    headers = {"Origin": base, "Referer": base + "/"}
    if key is not None:
        headers["X-Api-Key"] = key
    try:
        result = session.send(method, path, headers=headers, **({"json": body} if body is not None else {}))
    except Exception:
        raise SafetyError("Seerr account request failed; existing state was preserved") from None
    if type(result.status) is not int:
        raise SafetyError("Seerr account response status is invalid")
    return result


def _get(operation, session, path, *, key=None):
    result = _send(operation, session, "GET", path, key=key)
    if result.status != 200 or not isinstance(result.data, dict):
        _manual("Seerr authenticated readback failed; no account or server setting was replaced")
    return result.data


def _owner(value, username=None, jellyfin_id=None):
    if (not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != 1
            or type(value.get("permissions")) is not int or not value["permissions"] & ADMIN
            or not isinstance(value.get("jellyfinUserId"), str) or not value["jellyfinUserId"]
            or not isinstance(value.get("jellyfinUsername"), str) or not value["jellyfinUsername"]
            or (username is not None and value.get("jellyfinUsername") != username)
            or (jellyfin_id is not None and value["jellyfinUserId"] != jellyfin_id)):
        _manual("Seerr owner identity differs; existing owner credentials and account were preserved")
    return value


def _viewer(value, username, jellyfin_id):
    if (not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 1
            or value.get("permissions") != REQUEST or type(value.get("permissions")) is not int
            or value.get("jellyfinUsername") != username or value.get("jellyfinUserId") != jellyfin_id):
        _manual("Seerr viewer identity or request-only permissions differ; no account permission was overwritten")
    return value


def _connection(operation, data, server_id=None):
    if (not isinstance(data, dict) or data.get("ip") != catalog.role_host(operation.c, 2)
            or type(data.get("port")) is not int or data["port"] != 8096
            or data.get("useSsl", False) is not False or data.get("urlBase", "") not in {"", "/"}
            or not isinstance(data.get("serverId"), str) or not data["serverId"]
            or (server_id is not None and data["serverId"] != server_id)):
        _manual("Seerr Jellyfin connection differs; no existing server connection was replaced")
    return data


def _public(operation, session):
    value = _get(operation, session, "/api/v1/settings/public")
    if type(value.get("initialized")) is not bool or type(value.get("mediaServerType")) is not int:
        _manual("Seerr setup state is unknown; no account setup was attempted")
    return value


def _save_key(c, key):
    path = engine.secret_path(c, "seerr_api_key")
    if path.exists():
        if private_read(path).rstrip("\r\n") != key:
            _manual("Saved Seerr API key differs; no key or private file was replaced")
    else:
        atomic_write(path, key + "\n")


def _no_users(c):
    """Read the active WAL snapshot, without editing or creating a database."""
    path = no_symlinks(Path(c["install_root"]) / "appdata" / "seerr" / "db" / "db.sqlite3")
    try:
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name == "posix" and info.st_uid not in {0, c["puid"]})):
            return False
        # A running WAL database has both sidecars. Do not let a supposedly
        # read-only probe create missing shared-memory files to open its WAL.
        wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
        for sidecar in (wal, shm):
            no_symlinks(sidecar)
            if sidecar.exists() and (not sidecar.is_file() or sidecar.stat().st_nlink != 1):
                return False
        if wal.exists() and not shm.exists():
            return False
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            database.execute("BEGIN")
            tables = database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user'").fetchall()
            if tables != [("user",)]:
                return False
            columns = {row[1]: row for row in database.execute('PRAGMA table_info("user")').fetchall()}
            if not {"id", "email", "permissions", "jellyfinUserId", "jellyfinUsername"}.issubset(columns):
                return False
            if columns["id"][5] != 1:
                return False
            return database.execute('SELECT COUNT(*) FROM "user"').fetchone() == (0,)
    except (OSError, ValueError, sqlite3.Error):
        return False


def claimable(operation):
    """Pure legacy-upgrade absence proof; never write a reservation or account.

    The caller proves owned local containers and holds the deployment lock.
    False means manual review, including unsupported/non-SQLite databases.
    """
    settings = _settings(operation.c)
    if (settings["main"].get("mediaServerType") != NOT_CONFIGURED
            or settings.get("public", {}).get("initialized") is not False
            or not isinstance(settings.get("jellyfin"), dict) or settings["jellyfin"].get("ip", "")):
        return False
    key = resolve_api_key(operation.c)
    session = operation.session_factory(operation.endpoints["seerr"])
    public = _public(operation, session)
    if public["initialized"] is not False or public["mediaServerType"] != NOT_CONFIGURED:
        return False
    owner = _send(operation, session, "GET", "/api/v1/auth/me", key=key)
    return owner.status in {401, 403} and _no_users(operation.c)


def inspect_existing(operation):
    """GET-only owner/server/API authentication checks, not password enrollment."""
    key = resolve_api_key(operation.c)
    session = operation.session_factory(operation.endpoints["seerr"])
    public = _public(operation, session)
    if public["initialized"] is not True or public["mediaServerType"] != JELLYFIN:
        _manual("Existing Seerr setup is incomplete; its owner and settings were preserved")
    owner = _owner(_get(operation, session, "/api/v1/auth/me", key=key))
    connection = _connection(operation, _get(operation, session, "/api/v1/settings/jellyfin", key=key))
    jellyfin_key = connection.get("apiKey")
    if not _valid_key(jellyfin_key):
        _manual("Existing Seerr Jellyfin API key is invalid; it was not replaced")
    info = operation.call("GET", "jellyfin", "/System/Info", headers=jellyfin_headers(jellyfin_key))
    if not isinstance(info, dict) or info.get("Id") != connection["serverId"]:
        _manual("Existing Seerr and Jellyfin server identities differ")
    from .onboarding import _user_id
    linked = operation.call("GET", "jellyfin", "/Users/" + _user_id(owner["jellyfinUserId"]),
                            headers=jellyfin_headers(jellyfin_key))
    if (not isinstance(linked, dict) or linked.get("Id") != owner["jellyfinUserId"]
            or linked.get("Name") != owner["jellyfinUsername"]
            or linked.get("Policy", {}).get("IsAdministrator") is not True):
        _manual("Existing Seerr owner is not linked to the expected Jellyfin administrator")
    return "Existing owner API access and matching Jellyfin server verified; accounts and settings unchanged"


def enroll(operation):
    """Enroll only a reserved fresh Seerr, or verify a configured exact owner.

    An interrupted create that left an owner but NOT_CONFIGURED settings is
    deliberately stopped. Upstream's login would rewrite that owner, so this
    module never invokes the recovery branch, even for an intended identity.
    """
    app = "seerr"
    entry = operation.receipt.get("apps", {}).get(app, {})
    if entry.get("eligible") is not True:
        _manual("Seerr has no proven fresh reservation; existing accounts were preserved")
    creds = operation.credentials
    settings = _settings(operation.c)
    key = resolve_api_key(operation.c)
    probe = operation.session_factory(operation.endpoints[app])
    public = _public(operation, probe)
    if settings["main"].get("mediaServerType") != public["mediaServerType"]:
        _manual("Seerr generated and running setup state differ; no account write was attempted")
    own = _send(operation, probe, "GET", "/api/v1/auth/me", key=key)
    if own.status not in {200, 401, 403}:
        _manual("Seerr owner presence could not be established; no account write was attempted")
    owner_exists = own.status == 200
    if public["mediaServerType"] == NOT_CONFIGURED and owner_exists:
        _manual("Seerr already has an owner but unfinished server configuration; automatic owner recovery was refused")
    if public["mediaServerType"] not in {JELLYFIN, NOT_CONFIGURED}:
        _manual("Seerr is configured for another media server; existing settings were preserved")
    if not owner_exists and (public["initialized"] or entry.get("owner_verified") or entry.get("state") == "verified"):
        _manual("The expected Seerr owner is missing; no replacement owner was created")
    if not owner_exists and public["mediaServerType"] != NOT_CONFIGURED:
        _manual("Seerr server configuration exists without a verified owner; no account was created")
    read_only = owner_exists and not entry.get("owner_intent")
    if read_only and public["initialized"] is not True:
        _manual("Seerr already has an unfinished existing owner; no account or setting was changed")

    admin, admin_headers = operation.login_jellyfin(creds["admin_username"], creds["admin_password"], administrator=True)
    viewer, _ = operation.login_jellyfin(creds["viewer_username"], creds["viewer_password"], administrator=False)
    info = operation.call("GET", "jellyfin", "/System/Info", headers=admin_headers)
    if not isinstance(info, dict) or not isinstance(info.get("Id"), str) or not info["Id"]:
        _manual("Jellyfin server identity could not be verified before Seerr enrollment")
    identity = {"admin_username": creds["admin_username"], "viewer_username": creds["viewer_username"],
                "jellyfin_admin_id": admin["Id"], "jellyfin_viewer_id": viewer["Id"], "jellyfin_server_id": info["Id"]}
    if entry.get("desired_identity") not in (None, identity):
        _manual("Recorded Seerr enrollment identity differs; no account was replaced")
    if owner_exists:
        _owner(own.data, creds["admin_username"], admin["Id"])
        if entry.get("owner_id") not in {None, own.data["id"]}:
            _manual("Recorded Seerr owner ID differs; no account was replaced")
        _connection(operation, _get(operation, probe, "/api/v1/settings/jellyfin", key=key), info["Id"])
    else:
        if not claimable(operation):
            _manual("Seerr account absence could not be proved; no first-owner setup was attempted")
        operation.mark(app, desired_identity=identity, owner_absent_verified=True, owner_intent=True, state="intent")
    _save_key(operation.c, key)

    owner_session = operation.session_factory(operation.endpoints[app])
    body = {"username": creds["admin_username"], "password": creds["admin_password"]}
    if not owner_exists:
        body.update(hostname=catalog.role_host(operation.c, 2), port=8096, useSsl=False, urlBase="", serverType=JELLYFIN)
    logged = _send(operation, owner_session, "POST", "/api/v1/auth/jellyfin", body=body)
    if logged.status != 200 or not owner_session.has_cookies():
        _manual("Seerr owner cookie login failed; no password or owner was reset")
    _owner(logged.data, creds["admin_username"], admin["Id"])
    _owner(_get(operation, owner_session, "/api/v1/auth/me"), creds["admin_username"], admin["Id"])
    operation.mark(app, desired_identity=identity, owner_id=1, owner_verified=True)
    main = _get(operation, owner_session, "/api/v1/settings/main")
    if main.get("apiKey") != key or main.get("mediaServerType") != JELLYFIN:
        _manual("Seerr owner API-key readback differs; no key was rotated")
    _connection(operation, _get(operation, owner_session, "/api/v1/settings/jellyfin"), info["Id"])
    # API-key probes use a fresh cookie jar, so a session cannot hide a bad key.
    _owner(_get(operation, operation.session_factory(operation.endpoints[app]), "/api/v1/auth/me", key=key),
           creds["admin_username"], admin["Id"])
    if (type(main.get("defaultPermissions")) is not int or main["defaultPermissions"] != REQUEST
            or main.get("mediaServerLogin") is not True):
        _manual("Seerr default request permissions or Jellyfin login differ; existing policy was preserved")

    users = _get(operation, owner_session, "/api/v1/user")
    rows = users.get("results")
    if not isinstance(rows, list) or any(not isinstance(user, dict) for user in rows):
        _manual("Seerr user collection readback is invalid")
    matches = [user for user in rows if user.get("jellyfinUserId") == viewer["Id"]
               or user.get("jellyfinUsername") == creds["viewer_username"]]
    if len(matches) > 1:
        _manual("Seerr viewer identity is ambiguous; existing accounts were preserved")
    if matches:
        user = _viewer(matches[0], creds["viewer_username"], viewer["Id"])
        if entry.get("viewer_id") not in {None, user["id"]}:
            _manual("Recorded Seerr viewer ID differs; no account was replaced")
        if not entry.get("viewer_intent") and not read_only:
            _manual("An unexpected Seerr viewer already exists; it was not adopted or modified")
    else:
        if read_only or entry.get("viewer_id"):
            _manual("The existing Seerr viewer was not found; no replacement account was created")
        if main.get("newPlexLogin") is not True:
            _manual("Seerr new Jellyfin-user login is disabled; existing login policy was preserved")
        operation.mark(app, viewer_intent=True)
    viewer_session = operation.session_factory(operation.endpoints[app])
    logged_viewer = _send(operation, viewer_session, "POST", "/api/v1/auth/jellyfin",
        body={"username": creds["viewer_username"], "password": creds["viewer_password"]})
    if logged_viewer.status != 200 or not viewer_session.has_cookies():
        _manual("Seerr viewer cookie login failed; no password was reset")
    user = _viewer(logged_viewer.data, creds["viewer_username"], viewer["Id"])
    if entry.get("viewer_id") not in {None, user["id"]}:
        _manual("Seerr viewer login returned a different account")
    _viewer(_get(operation, viewer_session, "/api/v1/auth/me"), creds["viewer_username"], viewer["Id"])
    if _send(operation, viewer_session, "GET", "/api/v1/settings/main").status not in {401, 403}:
        _manual("Seerr viewer unexpectedly reached administrator settings")
    anonymous = operation.session_factory(operation.endpoints[app])
    if _send(operation, anonymous, "GET", "/api/v1/settings/main").status not in {401, 403}:
        _manual("Seerr administrator settings unexpectedly allowed anonymous access")
    operation.mark(app, viewer_id=user["id"], viewer_verified=True)
    if public["initialized"] is not True:
        if read_only:
            _manual("Existing Seerr setup remains unfinished; no setting was changed")
        operation.mark(app, initialize_intent=True)
        initialized = _send(operation, owner_session, "POST", "/api/v1/settings/initialize")
        if initialized.status != 200 or not isinstance(initialized.data, dict) or initialized.data.get("initialized") is not True:
            _manual("Seerr initialization was not confirmed; saved accounts were preserved")
    final = _public(operation, owner_session)
    if final["initialized"] is not True or final["mediaServerType"] != JELLYFIN:
        _manual("Seerr final setup readback was not confirmed")
    operation.mark(app, state="verified", existing_accounts_only=read_only)
    return "Owner and request-only viewer cookie logins verified; API key discovered; viewer administrator access rejected"
