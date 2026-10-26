"""Explicit reattestation of verified accounts after a file-based app restore.

Normal enrollment still refuses replaced application directory inodes. This
tool authenticates the restored accounts and updates only the old receipt's
directory identity, removing first-account creation authority permanently.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from . import catalog, engine, onboarding
from .safety import SafetyError, atomic_write, digest, no_symlinks, private_read


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw(path):
    # Validate the same privacy/link rules, then decode bytes without universal
    # newline conversion. UTF-8 roundtrips exactly through atomic_write, whose
    # explicit newline='\n' preserves existing CRLF bytes as well as LF.
    private_read(path)
    with no_symlinks(path).open("rb") as stream:
        value = stream.read(1024 * 1024 + 1)
    if len(value) > 1024 * 1024:
        raise SafetyError("The original account receipt exceeds its bounded size")
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise SafetyError("The original account receipt is not valid UTF-8") from None


def _prove_accounts(c, receipt, apps, request, session_factory, secret_resolver):
    """Only read operations and credential logins; never enroll or mark/save."""
    creds = onboarding._credentials(c, None, persist=False)
    if receipt.get("identity") != {key: creds[key] for key in ("admin_username", "viewer_username")}:
        raise SafetyError("Saved account names differ from the original receipt; no restore authorization was changed")
    operation = onboarding._Enrollment(c, creds, receipt, request, session_factory, secret_resolver)
    proof = {}
    try:
        for app in apps:
            operation.inspect_existing(app)
            proof[app] = {"existing_api_authentication": True}
            if app in {"sonarr", "radarr", "prowlarr"}:
                operation.starr_login(app)
                proof[app]["administrator_password_login"] = True
            elif app == "bazarr":
                from .enroll_bazarr import _ui_login
                _ui_login(operation, operation.secret_resolver("bazarr_api_key"))
                proof[app]["administrator_password_login"] = True
            elif app == "sabnzbd":
                from .enroll_sabnzbd import _login
                _login(operation)
                proof[app]["administrator_password_login"] = True
        if "jellyfin" not in apps:
            raise SafetyError("Verified Jellyfin administrator and viewer identities are required for account restore")
        entry = receipt["apps"]["jellyfin"]
        admin, headers = operation.login_jellyfin(creds["admin_username"], creds["admin_password"], administrator=True)
        viewer, _ = operation.login_jellyfin(creds["viewer_username"], creds["viewer_password"], administrator=False)
        info = operation.call("GET", "jellyfin", "/System/Info", headers=headers)
        if (not isinstance(info, dict) or info.get("Id") != entry.get("server_id") or not info.get("Id")
                or admin.get("Id") != entry.get("admin_id") or viewer.get("Id") != entry.get("viewer_id")
                or not entry.get("admin_id") or not entry.get("viewer_id")
                or any(viewer.get("Policy", {}).get(key) != value for key, value in onboarding.VIEWER_POLICY.items())):
            raise SafetyError("Restored Jellyfin server, account IDs or viewer policy differ from the verified receipt")
        proof["jellyfin"].update(server_id=info["Id"], admin_id=admin["Id"], viewer_id=viewer["Id"],
                                  administrator_and_viewer_password_logins=True, viewer_policy_verified=True)
        if "seerr" in apps:
            from . import enroll_seerr
            entry = receipt["apps"]["seerr"]
            key = enroll_seerr.resolve_api_key(c)
            session = operation.session_factory(operation.endpoints["seerr"])
            owner = enroll_seerr._owner(enroll_seerr._get(operation, session, "/api/v1/auth/me", key=key),
                                         creds["admin_username"], admin["Id"])
            desired = {"admin_username": creds["admin_username"], "viewer_username": creds["viewer_username"],
                       "jellyfin_admin_id": admin["Id"], "jellyfin_viewer_id": viewer["Id"], "jellyfin_server_id": info["Id"]}
            if entry.get("desired_identity") != desired or owner["id"] != entry.get("owner_id"):
                raise SafetyError("Restored Seerr owner or linked server identity differs from the verified receipt")
            viewer_id = entry.get("viewer_id")
            if type(viewer_id) is not int or viewer_id <= 1:
                raise SafetyError("The original Seerr receipt has no verified viewer identity")
            user = enroll_seerr._viewer(enroll_seerr._get(operation, session, "/api/v1/user/" + str(viewer_id), key=key),
                                        creds["viewer_username"], viewer["Id"])
            if user["id"] != viewer_id:
                raise SafetyError("Restored Seerr viewer ID differs from the verified receipt")
            proof["seerr"].update(owner_id=owner["id"], viewer_id=viewer_id, jellyfin_server_id=info["Id"],
                                  linked_account_ids_verified=True)
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("Restored account authentication could not be verified; no receipt or account was changed") from None
    return proof


def _snapshot(c, *, runner, request, session_factory, secret_resolver, allow_restored):
    onboarding._guard(c)
    receipt = onboarding._load(c, allow_restored_directories=allow_restored)
    if receipt is None:
        raise SafetyError("The original verified account receipt is required; this tool cannot adopt an installation")
    root, path = onboarding._paths(c)
    apps = list(catalog.stage_endpoints(c, 2))
    # This includes the live /proc/PID/root bind inode. A matching Docker
    # Mount.Source string alone can still refer to a deleted old directory.
    engine.check_container_ownership(c, 2, runner)
    onboarding.check_targets(c, apps, runner)
    changes, directories = [], {}
    for app in apps:
        entry = receipt["apps"].get(app)
        if not isinstance(entry, dict) or entry.get("state") != "verified":
            raise SafetyError("Every enabled restored application must have an already verified account receipt")
        directory = no_symlinks(root / "appdata" / app)
        identity = onboarding._identity(directory)
        directories[app] = identity
        if not any(directory.iterdir()):
            raise SafetyError("An application directory is empty; an empty or fresh backend cannot authorize restore")
        if not onboarding.same_identity(entry.get("directory"), identity):
            if not allow_restored:
                raise SafetyError("Restored directory identities still need explicit reviewed account reauthorization")
            old = entry.get("directory")
            if (not isinstance(old, dict) or set(old) not in ({"device", "inode"}, {"device", "inode", "filesystem"})
                    or not onboarding.same_identity(old, old)):
                raise SafetyError("The original account directory identity is invalid")
            changes.append({"app": app, "old_directory": old, "new_directory": identity})
    proof = _prove_accounts(c, receipt, apps, request, session_factory, secret_resolver)
    engine.check_container_ownership(c, 2, runner)
    if any(onboarding._identity(root / "appdata" / app) != identity for app, identity in directories.items()):
        raise SafetyError("An application directory changed during account verification; review a fresh restore plan")
    raw = _raw(path)
    # A receipt changed by another operator during the login checks is stale.
    if onboarding._private_json(path) != receipt:
        raise SafetyError("Account receipt changed while verifying restored accounts; review a fresh plan")
    result = {"schema_version": 1, "kind": "verified-account-restore", "deployment_id": c["deployment_id"],
              "host_id": engine.host_identity(), "config_digest": digest(c), "receipt_sha256": _sha(raw),
              "changes": changes, "directories": directories, "account_proof": proof,
              "effect": "Only changed receipt directory identities are rebound, with eligible=false; no account creation, password reset or application setting changes"}
    result["review_hash"] = digest(result)
    return result, receipt, raw, path


def _options(runner, request, session_factory, secret_resolver):
    return {"runner": runner or engine.run, "request": request or engine.request,
            "session_factory": session_factory or onboarding.HttpSession, "secret_resolver": secret_resolver}


def plan(c, *, runner=None, request=None, session_factory=None, secret_resolver=None):
    """Read-only directory/account inspection, including real saved-password logins."""
    result, _, _, _ = _snapshot(c, allow_restored=True,
                                **_options(runner, request, session_factory, secret_resolver))
    return result


def verify(c, *, runner=None, request=None, session_factory=None, secret_resolver=None):
    result, _, _, _ = _snapshot(c, allow_restored=False,
                                **_options(runner, request, session_factory, secret_resolver))
    return {"ok": True, "status": "verified", "account_proof": result["account_proof"], "changes": []}


def apply(c, reviewed_plan, *, runner=None, request=None, session_factory=None, secret_resolver=None):
    """Recheck the exact reviewed proof under both locks, then change one receipt."""
    if not isinstance(reviewed_plan, dict) or reviewed_plan.get("kind") != "verified-account-restore":
        raise SafetyError("Review a verified-account restore plan before applying")
    options = _options(runner, request, session_factory, secret_resolver)
    onboarding._guard(c)
    onboarding._paths(c)
    with engine.deployment_lock(c), onboarding._lock(c):
        current, receipt, raw, path = _snapshot(c, allow_restored=True, **options)
        if current != reviewed_plan:
            raise SafetyError("Restore proof changed after review; no account authorization was updated. Review a fresh plan")
        if not current["changes"]:
            return {"ok": True, "status": "verified", "account_proof": current["account_proof"], "changes": []}
        root = Path(c["install_root"])
        def unchanged():
            return (_raw(path) == raw and all(onboarding._identity(root / "appdata" / app) == identity
                                                      for app, identity in current["directories"].items()))
        if not unchanged():
            raise SafetyError("Restore targets changed before backup; no authorization was updated")
        backup_dir = no_symlinks(root / "backups")
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup = no_symlinks(backup_dir / ("onboarding-before-restore-" + current["review_hash"] + ".json"))
        if backup.exists():
            if _raw(backup) != raw:
                raise SafetyError("The private original-receipt backup differs; no authorization was updated")
        else:
            atomic_write(backup, raw)
        updated = deepcopy(receipt)
        for row in current["changes"]:
            updated["apps"][row["app"]].update(directory=row["new_directory"], eligible=False, restore_verified=True)
        # Check the receipt once more immediately before its only mutation.
        if not unchanged():
            raise SafetyError("The account receipt changed before save; no authorization was updated")
        engine.check_container_ownership(c, 2, options["runner"])
        if not unchanged():
            raise SafetyError("The restore directory identity changed before save; no authorization was updated")
        onboarding._save(c, updated)
        if onboarding._load(c) != updated:
            raise SafetyError("Restored-account receipt readback failed; inspect the retained private backup")
        return {"ok": True, "status": "verified", "account_proof": current["account_proof"],
                "changes": current["changes"], "first_account_creation": "disabled for rebound applications"}
