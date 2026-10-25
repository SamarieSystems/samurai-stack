"""Read-only legacy seed proof and authenticated existing-server connection.

Jellyfin v12.3 source: Users.Password is nullable; its default authentication
provider accepts an empty password only for null/empty stored passwords. The
SQLite provider uses DataPath/jellyfin.db, with the official image's DataPath
under /config/data. Unknown/custom database locations are never guessed.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import urlencode
import uuid
import xml.etree.ElementTree as ET

from . import engine
from .integrations import jellyfin_headers
from .safety import SafetyError, atomic_write, no_symlinks, private_read

_DEFAULT_PROVIDER = "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{16,4096}\Z")


def _manual(message):
    from .onboarding import _Manual
    raise _Manual(message)


def passwordless_seed(c, startup_name):
    """Return sole local passwordless user's UUID hex, or None, with no login.

    This is only a precondition. The caller must still require incomplete
    startup, authenticate the seed, verify its admin role and exact ID, and
    prove no libraries. SQL returns a boolean, never a stored password/hash.
    Active WAL is read normally; immutable snapshots are deliberately not used.
    """
    if not isinstance(startup_name, str) or not startup_name:
        return None
    root = no_symlinks(Path(c["install_root"]) / "appdata" / "jellyfin")
    path = no_symlinks(root / "data" / "jellyfin.db")
    try:
        config = no_symlinks(root / "config" / "database.xml")
        if config.exists():
            if not config.is_file() or config.stat().st_nlink != 1 or config.stat().st_size > 64 * 1024:
                return None
            tree = ET.fromstring(config.read_text(encoding="utf-8"))
            kind = tree.findtext("DatabaseType")
            if kind != "Jellyfin-SQLite":
                return None
            for element in tree.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if (tag == "Key" and (element.text or "").casefold() == "path") or (
                        tag in {"PluginName", "PluginAssembly", "ConnectionString"} and (element.text or "").strip()):
                    return None
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name == "posix" and info.st_uid not in {0, c["puid"]})):
            return None
        wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
        for sidecar in (wal, shm):
            no_symlinks(sidecar)
            if sidecar.exists() and (not sidecar.is_file() or sidecar.stat().st_nlink != 1):
                return None
        if wal.exists() and not shm.exists():
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            database.execute("BEGIN")
            tables = database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Users'").fetchall()
            if tables != [("Users",)]:
                return None
            columns = {row[1]: row for row in database.execute('PRAGMA table_info("Users")').fetchall()}
            if not {"Id", "Username", "Password", "AuthenticationProviderId"}.issubset(columns) or columns["Id"][5] != 1:
                return None
            rows = database.execute('SELECT "Id", "Username", ("Password" IS NULL OR "Password" = \'\'), '
                                    '"AuthenticationProviderId" FROM "Users" LIMIT 2').fetchall()
            if (len(rows) != 1 or rows[0][1] != startup_name or rows[0][2] != 1
                    or rows[0][3] != _DEFAULT_PROVIDER or not isinstance(rows[0][0], str)):
                return None
            return uuid.UUID(rows[0][0]).hex
    except (OSError, ValueError, ET.ParseError, sqlite3.Error):
        return None


def _existing_key(operation, headers, server_id):
    """Preserve a working imported key, otherwise discover/create one name."""
    app, entry = "jellyfin", operation.receipt["apps"]["jellyfin"]
    path = engine.secret_path(operation.c, "jellyfin_api_key")
    if path.exists():
        key = private_read(path).rstrip("\r\n")
        if not _TOKEN.fullmatch(key):
            _manual("Existing private Jellyfin API key is malformed; it was not replaced")
    else:
        name = "Samurai Stack " + operation.c["deployment_id"]
        response = operation.call("GET", app, "/Auth/Keys", headers=headers)
        rows = response.get("Items") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            _manual("Jellyfin API-key collection is invalid; no key was changed")
        matches = [row for row in rows if isinstance(row, dict) and row.get("AppName") == name]
        if not matches:
            if entry.get("api_key_verified"):
                _manual("The recorded Jellyfin API key is missing; no replacement was created")
            operation.mark(app, api_key_intent=True)
            operation.call("POST", app, "/Auth/Keys?" + urlencode({"app": name}), headers=headers)
            response = operation.call("GET", app, "/Auth/Keys", headers=headers)
            rows = response.get("Items") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                _manual("Jellyfin API-key readback is invalid")
            matches = [row for row in rows if isinstance(row, dict) and row.get("AppName") == name]
        if (len(matches) != 1 or not isinstance(matches[0].get("AccessToken"), str)
                or not _TOKEN.fullmatch(matches[0]["AccessToken"])):
            _manual("Deployment-named Jellyfin API key is missing or ambiguous; no existing key was rotated")
        key = matches[0]["AccessToken"]
        atomic_write(path, key + "\n")
    info = operation.call("GET", app, "/System/Info", headers=jellyfin_headers(key))
    if not isinstance(info, dict) or info.get("Id") != server_id:
        _manual("Existing Jellyfin API key did not authenticate the expected server; it was not replaced")
    operation.mark(app, api_key_verified=True)


def connect(operation, *, need_viewer=False):
    """Connect an existing completed server after real administrator login.

    Caller stores credentials privately and proves owned local targets under
    the deployment lock. Existing admin credentials/policy are never changed.
    An existing viewer is authenticated without policy edits. Only an absent,
    explicitly desired viewer may be newly created with restricted policy.
    """
    app, creds = "jellyfin", operation.credentials
    entry = operation.receipt.get("apps", {}).get(app)
    if not isinstance(entry, dict):
        _manual("Existing Jellyfin installation has no managed identity record")
    public = operation.call("GET", app, "/System/Info/Public")
    complete = public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted")) if isinstance(public, dict) else None
    if not isinstance(public, dict) or not public.get("Id") or type(complete) is not bool:
        _manual("Existing Jellyfin setup state is unknown; no account or startup setting was changed")
    admin, headers = operation.login_jellyfin(creds["admin_username"], creds["admin_password"], administrator=True)
    if entry.get("admin_id") not in {None, admin["Id"]} or entry.get("server_id") not in {None, public["Id"]}:
        _manual("Existing Jellyfin administrator or server identity differs; no account was replaced")
    identity = {"admin_username": creds["admin_username"], "admin_id": admin["Id"], "server_id": public["Id"]}
    if entry.get("legacy_identity") not in (None, identity):
        _manual("Recorded existing Jellyfin identity differs; no account was replaced")
    info = operation.call("GET", app, "/System/Info", headers=headers)
    if not isinstance(info, dict) or info.get("Id") != public["Id"]:
        _manual("Existing Jellyfin authenticated server identity differs")
    operation.mark(app, legacy_identity=identity, admin_id=admin["Id"], server_id=public["Id"], admin_verified=True)
    if complete is not True:
        # Finishing the wizard is authorized after an actual existing-admin
        # login. This never changes that account's name, password or policy.
        operation.mark(app, legacy_complete_intent=True)
        operation.call("POST", app, "/Startup/Complete", headers=headers)
        final = operation.call("GET", app, "/System/Info/Public")
        if (not isinstance(final, dict) or final.get("Id") != public["Id"]
                or final.get("StartupWizardCompleted", final.get("IsStartupWizardCompleted")) is not True):
            _manual("Existing Jellyfin startup completion was not confirmed; account credentials were preserved")
    _existing_key(operation, headers, public["Id"])
    if need_viewer:
        users = operation.call("GET", app, "/Users", headers=headers)
        if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
            _manual("Existing Jellyfin user collection is invalid")
        matches = [user for user in users if str(user.get("Name", "")).casefold() == creds["viewer_username"].casefold()]
        if len(matches) > 1:
            _manual("Existing Jellyfin viewer identity is ambiguous; no account was changed")
        if not matches or entry.get("legacy_viewer_create_intent"):
            if not matches and entry.get("viewer_id"):
                _manual("The recorded Jellyfin viewer is missing; no replacement account was created")
            operation.mark(app, legacy_viewer_create_intent=True)
            operation.viewer(headers, admin["Id"])
        else:
            candidate = matches[0]
            user, viewer_headers = operation.login_jellyfin(creds["viewer_username"], creds["viewer_password"], administrator=False)
            if (candidate.get("Id") != user["Id"] or user["Id"] == admin["Id"]
                    or candidate.get("Policy", {}).get("IsAdministrator") is not False
                    or entry.get("viewer_id") not in {None, user["Id"]}):
                _manual("Existing Jellyfin viewer identity or role differs; its policy was preserved")
            session = operation.session_factory(operation.endpoints[app])
            elevated = "/System/Configuration/MetadataOptions/Default"
            if session.send("GET", elevated, headers=headers).status != 200:
                _manual("Existing Jellyfin administrator authorization could not be verified")
            if session.send("GET", elevated, headers=viewer_headers).status not in {401, 403}:
                _manual("Existing Jellyfin viewer unexpectedly reached administrator settings")
            operation.mark(app, viewer_id=user["Id"], legacy_viewer_verified=True)
    operation.mark(app, legacy_connection_verified=True)
    return "Existing Jellyfin administrator login and API access verified; existing account credentials and policies preserved"
