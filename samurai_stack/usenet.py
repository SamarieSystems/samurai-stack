"""Locked, private NNTP provider onboarding for guided media setup.

Provider enrollment is separate from application account creation. Existing SAB
accounts are authenticated, never changed, and provider-only authorization does
not expand a legacy installation's fresh-account reservation.
"""
from __future__ import annotations

from contextlib import contextmanager
import json as codec
import os
import uuid

from . import catalog, engine, enroll_sabnzbd as sab, onboarding as accounts
from .safety import SafetyError, no_symlinks, run, write_json


@contextmanager
def _operation(c, request, session_factory, secret_resolver, runner):
    accounts._guard(c)
    root, _ = accounts._paths(c)
    if sab.APP not in catalog.stage_endpoints(c, 2):
        raise SafetyError("Enable Usenet in this deployment before configuring its NNTP provider")
    with engine.deployment_lock(c), accounts._lock(c):
        receipt = accounts._load(c)
        accounts.check_targets(c, [sab.APP], runner)
        if receipt is None:
            receipt = {"schema_version": 1, "deployment_id": c["deployment_id"],
                       "host_id": engine.host_identity(), "bind_address": c["bind_address"], "apps": {}}
        if sab.APP not in receipt["apps"]:
            # Read-only operations never save this provisional row. A provider
            # write may save it only after a successful real administrator login.
            receipt["apps"][sab.APP] = {"eligible": False, "state": "manual",
                "directory": accounts._identity(root / "appdata" / sab.APP),
                "reason": "Existing SABnzbd account is preserved; provider-only authorization"}
        operation = accounts._Enrollment(c, None, receipt, request, session_factory, secret_resolver)
        yield operation, root


def providers(c, *, request=engine.request, session_factory=accounts.HttpSession, secret_resolver=None, runner=run):
    """Inspect provider identifiers without passwords or administrator prompts."""
    with _operation(c, request, session_factory, secret_resolver, runner) as (operation, _):
        return sab.configured_providers(operation)


def verify(c, *, request=engine.request, session_factory=accounts.HttpSession, secret_resolver=None, runner=run):
    """Run actual connection tests using the configured, stored NNTP credentials."""
    with _operation(c, request, session_factory, secret_resolver, runner) as (operation, _):
        return sab.verify_existing_providers(operation)


def needs_login(c):
    """Check private local authorization state without sending an HTTP request."""
    accounts._guard(c)
    root, _ = accounts._paths(c)
    if sab.APP not in catalog.stage_endpoints(c, 2):
        raise SafetyError("Enable Usenet in this deployment before configuring its NNTP provider")
    receipt = accounts._load(c)
    dedicated = no_symlinks(root / "secrets/usenet_admin_credentials.json")
    if dedicated.exists():
        _admin_pair(accounts._private_json(dedicated))
        return False
    entry = (receipt or {}).get("apps", {}).get(sab.APP, {})
    shared = no_symlinks(root / "secrets/onboarding_credentials.json")
    if entry.get("eligible") is True and entry.get("admin_verified") is True and shared.exists():
        saved = accounts._private_json(shared)
        _admin_pair({name: saved.get(name) for name in ("admin_username", "admin_password")})
        return False
    return True


def saved_provider(c):
    """Return a local retry/edit summary with no NNTP username or password."""
    accounts._guard(c)
    root, _ = accounts._paths(c)
    receipt = accounts._load(c)
    path = no_symlinks(root / "secrets/usenet_provider.json")
    if not path.exists():
        return None
    value = sab._provider(accounts._private_json(path))
    entry = (receipt or {}).get("apps", {}).get(sab.APP, {})
    return {"host": value["host"], "port": value["port"], "connections": value["connections"],
            "safely_editable": not bool(entry.get("provider_intent") or entry.get("provider_verified"))}


def _backup_provider(root, original):
    backups = no_symlinks(root / "backups")
    if not backups.exists():
        backups.mkdir(mode=0o700)
    if (not backups.is_dir() or not accounts._root_owned(backups)
            or (os.name == "posix" and backups.stat().st_mode & 0o077)):
        raise SafetyError("NNTP input backup directory must be private and owned by root")
    path = no_symlinks(backups / ("usenet-provider-before-edit-" + uuid.uuid4().hex + ".json"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())


def _provider(operation, root, supplied, *, replace_unverified=False):
    path = no_symlinks(root / "secrets/usenet_provider.json")
    saved = accounts._private_json(path) if path.exists() else None
    original = path.read_bytes() if saved is not None else None
    if original is not None:
        try:
            same = codec.loads(original) == saved
        except (ValueError, UnicodeError):
            same = False
        if not same:
            raise SafetyError("Saved NNTP input changed during inspection; no replacement was made")
    if replace_unverified and (supplied is None or saved is None):
        raise SafetyError("Editing NNTP retry input requires explicit new details and an existing saved input")
    if supplied is None:
        supplied = saved
    if supplied is None:
        raise SafetyError("Provide the NNTP hostname, TLS port, service login and connection allowance first")
    # Validate the provider contract before any secret publication or request.
    value = sab._provider(supplied)
    canonical = {name: value[name] for name in ("host", "port", "username", "password", "connections")}
    canonical["ssl"] = True
    if saved is not None:
        # Revalidate stored shape as well. Never reinterpret or replace an
        # unrelated provider file or silently rotate a supplied password.
        saved_value = sab._provider(saved)
        if saved_value != value:
            if not replace_unverified:
                raise SafetyError("Saved NNTP provider differs; existing secrets and server settings were not replaced")
            entry = operation.receipt["apps"][sab.APP]
            if entry.get("provider_intent") or entry.get("provider_verified"):
                raise SafetyError("NNTP provider creation was already attempted; saved settings cannot be replaced through retry-input editing")
            # A missing local intent is not enough: a lost/manual server write
            # may exist. Refuse to change the input if our real server exists.
            name = "samurai-" + operation.c["deployment_id"]
            if any(row["name"] == name for row in sab._Api(operation).servers()):
                raise SafetyError("The deployment's NNTP provider already exists; its saved input was not replaced")
            if path.read_bytes() != original:
                raise SafetyError("Saved NNTP input changed during review; no replacement was made")
            _backup_provider(root, original)
            if path.read_bytes() != original:
                raise SafetyError("Saved NNTP input changed during backup; private originals were retained without replacing it")
            write_json(path, canonical, replace=True)
    else:
        write_json(path, canonical)
    return canonical


def _admin_pair(value):
    if not isinstance(value, dict) or set(value) != {"admin_username", "admin_password"}:
        raise SafetyError("Provide the existing SABnzbd administrator username and password in the terminal")
    pair = dict(value)
    for name, content in pair.items():
        if (not isinstance(content, str) or not content or len(content) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in content)):
            raise SafetyError("Existing SABnzbd administrator credentials must be nonempty and contain no control characters")
    return pair


def _credentials(root, supplied, *, allow_shared):
    path = no_symlinks(root / "secrets/usenet_admin_credentials.json")
    saved = _admin_pair(accounts._private_json(path)) if path.exists() else None
    if supplied is not None:
        supplied = _admin_pair(supplied)
        if saved is not None and saved != supplied:
            raise SafetyError("Saved SABnzbd provider-authorization login differs; no credential file was replaced")
        return supplied, path if saved is None else None
    if saved is not None:
        return saved, None
    shared = no_symlinks(root / "secrets/onboarding_credentials.json")
    if allow_shared and shared.exists():
        credentials = accounts._private_json(shared)
        return _admin_pair({name: credentials.get(name) for name in ("admin_username", "admin_password")}), None
    raise SafetyError("NNTP setup needs the existing SABnzbd administrator username/password in the terminal; no account or password will be changed")


def configure(c, provider=None, *, credentials=None, replace_unverified=False, request=engine.request,
              session_factory=accounts.HttpSession, secret_resolver=None, runner=run):
    """Save an exact private retry input, authenticate, then add only our server.

    Passing provider=None resumes secrets/usenet_provider.json. Optional
    credentials contains only admin_username/admin_password for an existing
    SAB account; it is verified before private publication and never changes
    the account itself. Explicit replace_unverified=True may correct a failed
    input only before creation intent and after a fresh server inventory check.
    """
    if type(replace_unverified) is not bool:
        raise SafetyError("NNTP retry-input replacement must be an explicit boolean choice")
    with _operation(c, request, session_factory, secret_resolver, runner) as (operation, root):
        value = _provider(operation, root, provider, replace_unverified=replace_unverified)
        entry = operation.receipt["apps"][sab.APP]
        pair, candidate = _credentials(root, credentials,
            allow_shared=entry.get("eligible") is True and entry.get("admin_verified") is True)
        operation.credentials = pair
        # The API key alone is not provider-write authorization for a legacy
        # account. Require both its configured UI gate and an actual form login.
        sab.inspect_existing(operation)
        sab._login(operation)
        if candidate is not None:
            write_json(candidate, pair)
        operation.mark(sab.APP, provider_scope_authorized=True,
                       provider_authorized_username=pair["admin_username"])
        return sab.configure_provider(operation, value)
