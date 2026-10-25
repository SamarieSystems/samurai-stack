"""First-run Bazarr form authentication without password recovery or media work.

Contract matched to upstream Bazarr v1.6.2: api/system/settings.py,
api/system/account.py and app/ui.py. Authentication settings take effect in the
running process; neither a container restart nor a config-file edit is needed.
The SPA root intentionally returns HTTP 200 for unauthenticated clients. Its
server-injected API key, not the status code alone, proves UI authorization.
"""
from __future__ import annotations

import re
import secrets
import time

from . import engine
from .safety import SafetyError, atomic_write, private_read


def _manual(message):
    # Import lazily: onboarding dispatches to this module.
    from .onboarding import _Manual
    return _Manual(message)


def _send(session, method, path, **kwargs):
    try:
        return session.send(method, path, **kwargs)
    except Exception:
        raise SafetyError("Bazarr enrollment HTTP check failed; saved credentials were preserved") from None


def _auth(operation, key):
    value = operation.call("GET", "bazarr", "/api/system/settings", headers={"X-API-KEY": key})
    return _auth_report(value, key)


def _auth_report(value, key):
    auth = value.get("auth") if isinstance(value, dict) else None
    if (not isinstance(auth, dict) or not {"type", "username", "password", "apikey"} <= set(auth)
            or auth["type"] not in {None, "basic", "form"}
            or not isinstance(auth["username"], str) or not isinstance(auth["password"], str)
            or auth["apikey"] != key):
        raise _manual("Bazarr authentication settings are unknown; existing accounts were preserved")
    return auth


def _ready_auth(operation, key):
    """Wait for the real settings API, not the image's early TCP health check.

    LSIO can mark the socket healthy before Waitress starts serving requests.
    This gate is read-only; no account write is retried while startup settles.
    """
    session = operation.session_factory(operation.endpoints["bazarr"])
    deadline = time.monotonic() + 120
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _manual("Bazarr's settings API did not become ready within 120 seconds; this readiness check changed no account settings")
        try:
            response = session.send("GET", "/api/system/settings", headers={"X-API-KEY": key},
                                    timeout=min(5, remaining))
        except SafetyError as error:
            # HttpSession deliberately sanitizes connection/timeout errors into
            # this fixed transport-owned message. Other safety refusals, such
            # as an oversized response or invalid target, are not readiness.
            if str(error) != "Enrollment HTTP check failed; inspect the service locally":
                raise
        else:
            if response.status == 200:
                return _auth_report(response.data, key)
            if response.status in {401, 403}:
                raise _manual("Bazarr settings API rejected its generated key; no account settings were changed")
            if not 500 <= response.status <= 599:
                raise _manual("Bazarr settings API returned an unsupported readiness response; no account settings were changed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _manual("Bazarr's settings API did not become ready within 120 seconds; this readiness check changed no account settings")
        time.sleep(min(2, remaining))


def _ui_login(operation, key):
    base, creds = operation.endpoints["bazarr"], operation.credentials
    anonymous = operation.session_factory(base)
    public = _send(anonymous, "GET", "/")
    if public.status != 200 or not isinstance(public.data, str) or key in public.data:
        raise _manual("Bazarr UI unexpectedly exposes administrator access without a login")
    if _send(anonymous, "GET", "/api/system/settings").status != 401:
        raise _manual("Bazarr settings unexpectedly allowed access without an API key")

    good = operation.session_factory(base)
    login = _send(good, "POST", "/api/system/account", form={
        "action": "login", "username": creds["admin_username"], "password": creds["admin_password"]})
    page = _send(good, "GET", "/")
    if (login.status != 204 or not good.has_cookies() or page.status != 200
            or not isinstance(page.data, str) or key not in page.data):
        raise _manual("Bazarr administrator form login could not be verified; no password was reset")

    bad = operation.session_factory(base)
    denied = _send(bad, "POST", "/api/system/account", form={
        "action": "login", "username": creds["admin_username"], "password": secrets.token_urlsafe(36)})
    page = _send(bad, "GET", "/")
    if (denied.status != 403 or page.status != 200 or not isinstance(page.data, str) or key in page.data):
        raise _manual("Bazarr unexpectedly accepted an incorrect administrator password")


def _enroll(operation):
    entry = operation.receipt["apps"].get("bazarr")
    if not isinstance(entry, dict) or entry.get("eligible") is not True:
        raise _manual("Bazarr has no fresh managed account reservation; existing accounts were preserved")
    key = operation.secret_resolver("bazarr_api_key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", key):
        raise _manual("Bazarr generated API-key identity is unknown; no settings were changed")
    auth = _ready_auth(operation, key)
    blank = auth["type"] is None and auth["username"] == "" and auth["password"] == ""
    if blank:
        if entry.get("state") == "verified":
            raise _manual("The enrolled Bazarr account is missing; no replacement was created")
        # This durable intent is published before the sole account mutation.
        # A lost response is resumed through real saved-password login. Only
        # another wholly blank auth block allows retrying an unreached write.
        operation.mark("bazarr", admin_intent=True, state="intent")
        changed = _send(operation.session_factory(operation.endpoints["bazarr"]),
            "POST", "/api/system/settings", headers={"X-API-KEY": key}, form={
                "settings-auth-type": "form", "settings-auth-username": operation.credentials["admin_username"],
                "settings-auth-password": operation.credentials["admin_password"]})
        if changed.status != 204:
            raise _manual("Bazarr did not confirm first-run authentication settings; rerun to verify saved state")
        auth = _auth(operation, key)
    elif not entry.get("admin_intent"):
        # A browser may have initialized this fresh directory first. An exact
        # saved-credential login can verify it read-only; it never grants a
        # password write to an initialized account, even with a matching name.
        operation.mark("bazarr", existing_account=True)
    if (auth["type"] != "form" or auth["username"] != operation.credentials["admin_username"]
            or not re.fullmatch(r"[A-Fa-f0-9]{32}", auth["password"])):
        raise _manual("Bazarr already has different account settings; no credential was replaced")
    _ui_login(operation, key)
    # Confirm authenticated policy again after positive and negative logins.
    saved = _auth(operation, key)
    if saved != auth:
        raise _manual("Bazarr authentication settings changed during verification; no credential was replaced")
    path = engine.secret_path(operation.c, "bazarr_api_key")
    if path.exists():
        if private_read(path).rstrip("\r\n") != key:
            raise _manual("Saved Bazarr API key differs; no key or private file was replaced")
    else:
        atomic_write(path, key + "\n")
    operation.mark("bazarr", state="verified", admin_verified=True, api_key_verified=True)
    return "Administrator form login verified; anonymous and incorrect-password access rejected; generated API key saved privately"


def enroll(operation):
    """Enroll one pre-reserved, owned Bazarr endpoint using the common pair.

    The caller holds deployment/account locks, proves container/port/mount
    ownership and durably saves credentials before invoking this function.
    Only the three first-run auth fields can change. No restart, account reset,
    provider enrollment, library operation, download or media write is issued.
    """
    from .onboarding import _Manual
    try:
        return _enroll(operation)
    except _Manual:
        raise
    except Exception:
        raise SafetyError("Bazarr enrollment or verification failed; existing state and saved credentials were preserved") from None


def inspect_existing(operation):
    """Read-only legacy readiness, without adopting credentials or accounts.

    The generated/local API key authenticates settings readback. This proves
    configured UI authentication and anonymous denial, not knowledge of the
    existing password. The caller must first prove owned local endpoint/mount
    identity, just as for enrollment. No reservation or credential pair is used.
    """
    from .onboarding import _Manual
    try:
        key = operation.secret_resolver("bazarr_api_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", key):
            raise _manual("Bazarr generated API-key identity is unknown; existing accounts were preserved")
        auth = _ready_auth(operation, key)
        if (auth["type"] not in {"basic", "form"} or not auth["username"]
                or not re.fullmatch(r"[A-Fa-f0-9]{32}", auth["password"])):
            raise _manual("Bazarr has no initialized UI authentication; finish first-account enrollment")
        anonymous = operation.session_factory(operation.endpoints["bazarr"])
        if _send(anonymous, "GET", "/api/system/settings").status != 401:
            raise _manual("Bazarr settings unexpectedly allowed access without an API key")
        page = _send(anonymous, "GET", "/")
        if auth["type"] == "basic":
            if page.status != 401:
                raise _manual("Bazarr basic authentication did not reject anonymous access")
        elif page.status != 200 or not isinstance(page.data, str) or key in page.data:
            raise _manual("Bazarr UI unexpectedly exposes administrator access without a login")
        return "Existing Bazarr UI authentication and API-key readback verified; existing accounts and passwords preserved"
    except _Manual:
        raise
    except Exception:
        raise SafetyError("Existing Bazarr authentication could not be verified; no account or private file was changed") from None
