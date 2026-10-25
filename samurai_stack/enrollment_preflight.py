"""Read-only proof that an existing single-account service has no credentials.

Old alphas started app directories before account reservations existed. These
authenticated API checks let the caller reserve only genuinely credentialless
services, without inferring freshness from directory age or a matching name.
The caller must hold the deployment/account locks, prove local container, port
and mount ownership, durably record any new reservation and independently
recheck the same blank account state in the actual enrollment implementation.
"""
from __future__ import annotations

from .safety import SafetyError


_STARR = frozenset(("sonarr", "radarr", "prowlarr"))
_ERROR = "Account absence could not be proven through the owned service API; existing accounts were preserved"


def _key(operation, name):
    value = operation.secret_resolver(name)
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise SafetyError(_ERROR)
    return value


def _blank_fields(value, *, nullable=False):
    if not isinstance(value, dict) or not {"username", "password"} <= set(value):
        return False
    # Never use truthiness: 0, False, missing fields, whitespace, a masked
    # password or one blank half of an existing account are not absence proof.
    return all((nullable and value[name] is None) or type(value[name]) is str and value[name] == ""
               for name in ("username", "password"))


def _claimable(operation, app):
    if app in _STARR:
        prefix = "/api/v1" if app == "prowlarr" else "/api/v3"
        current = operation.call("GET", app, prefix + "/config/host",
                                 headers={"X-Api-Key": _key(operation, app + "_api_key")})
        if not isinstance(current, dict):
            raise SafetyError(_ERROR)
        return _blank_fields(current, nullable=True)
    if app == "bazarr":
        key = _key(operation, "bazarr_api_key")
        current = operation.call("GET", app, "/api/system/settings", headers={"X-API-KEY": key})
        auth = current.get("auth") if isinstance(current, dict) else None
        if not isinstance(auth, dict) or auth.get("apikey") != key:
            raise SafetyError(_ERROR)
        return "type" in auth and auth["type"] is None and _blank_fields(auth)
    if app == "sabnzbd":
        key = _key(operation, "sab_api_key")
        response = operation.session_factory(operation.endpoints[app]).send("POST", "/api", form={
            "mode": "get_config", "output": "json", "apikey": key, "section": "misc"})
        if (response.status != 200 or not isinstance(response.data, dict)
                or response.data.get("status") is False):
            raise SafetyError(_ERROR)
        config = response.data.get("config")
        current = config.get("misc") if isinstance(config, dict) else None
        if not isinstance(current, dict):
            raise SafetyError(_ERROR)
        return _blank_fields(current)
    # Jellyfin and Seerr are multi-user applications. Blank/missing browser
    # fields cannot prove that no owner exists; their stronger, app-specific
    # proofs belong in the caller's reviewed upgrade path.
    return False


def claimable(operation, app):
    """Return absence proof only, never credentials or a write authorization.

    No receipt, local file, cookie login, account, provider or media setting is
    changed. SAB's POST is only its supported read-only get_config operation;
    secrets remain in headers or the request body, never a URL or public error.
    Unexpected API, key or transport errors fail closed with a fixed message.
    """
    try:
        return _claimable(operation, app)
    except Exception:
        raise SafetyError(_ERROR) from None
