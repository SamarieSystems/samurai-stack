"""Native Immich owner enrollment and bounded resource policy, never recovery.

Only an empty datastore reserved before its first Compose start can create an
owner. Existing owners authenticate with their current password. No assets,
libraries, other users, passwords or storage templates are changed.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import os
import re
import stat
import uuid

from . import catalog, engine, extras, naming, onboarding
from .safety import SafetyError, digest, no_symlinks, run, validate_config, write_json


def _paths(c):
    engine.linux_only()
    if os.name == "posix" and os.geteuid() != 0:
        raise SafetyError("Photo enrollment requires root inside the target Linux guest")
    validate_config(c)
    engine.stage_allowed(c, 5)
    root, _ = onboarding._paths(c)
    return root, root / "state" / "photos-onboarding.json"


def _binding(c):
    return {"deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
            "bind_address": c["bind_address"]}


def _receipt(c):
    root, path = _paths(c)
    if not path.exists():
        return None
    value = onboarding._private_json(path)
    if (value.get("schema_version") != 1 or any(value.get(k) != v for k, v in _binding(c).items())
            or not onboarding.same_identity(value.get("directory"), onboarding._identity(root / "appdata/immich-postgres"))):
        raise SafetyError("Photo account reservation belongs to another host or datastore; nothing was reset")
    return value


def reserve_fresh(c, *, runner=run):
    """Called under the deployment lock before creating Compose or starting it."""
    root, path = _paths(c)
    if path.exists():
        return _receipt(c)
    directory = no_symlinks(root / "appdata/immich-postgres")
    containers = engine.docker(runner, "ps", "--all", "--filter",
        "label=com.docker.compose.project=" + naming.project_name(c, 5), "--format", "{{.ID}}").stdout.strip()
    eligible = not containers and not engine.compose_path(c, 5).exists() and not any(directory.iterdir())
    value = {"schema_version": 1, **_binding(c), "directory": onboarding._identity(directory),
             "eligible": eligible, "state": "reserved" if eligible else "existing"}
    write_json(path, value)
    return value


def save_credentials(c, supplied, *, replace=False):
    root, _ = _paths(c)
    if (not isinstance(supplied, dict) or set(supplied) != {"email", "password", "name"}
            or any(not isinstance(v, str) or not v or len(v) > 256
                   or any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in supplied.values())
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", supplied["email"])):
        raise SafetyError("Provide an Immich email login, display name and password through private input")
    path = root / "secrets/photos-owner.json"
    value = {"schema_version": 1, **_binding(c), "credentials": supplied}
    if path.exists():
        if onboarding._private_json(path) != value:
            if not replace:
                raise SafetyError("Saved photo owner input differs; use explicit private-input correction, not an account reset")
            write_json(root / "backups" / ("photos-owner-input-" + uuid.uuid4().hex + ".json"), onboarding._private_json(path))
            write_json(path, value, replace=True)
    else:
        write_json(path, value)


def _credentials(c):
    root, _ = _paths(c)
    value = onboarding._private_json(root / "secrets/photos-owner.json")
    if value.get("schema_version") != 1 or any(value.get(k) != v for k, v in _binding(c).items()):
        raise SafetyError("Photo owner input belongs to another installation")
    supplied = value.get("credentials")
    save_credentials(c, supplied)  # Validate without replacing or disclosing it.
    return supplied


def _live_bind(row, source, destination):
    source = no_symlinks(source)
    host = source.stat()
    if not stat.S_ISDIR(host.st_mode):
        raise SafetyError('Photo persistent bind is not a directory')
    if os.name == 'posix':
        pid = row.get('State', {}).get('Pid')
        if type(pid) is not int or not 0 < pid < 2**31:
            raise SafetyError('Cannot prove the live photo persistent bind')
        try:
            live = os.stat('/proc/' + str(pid) + '/root' + destination)
            after = source.stat()
        except OSError:
            raise SafetyError('Cannot inspect the live photo persistent bind') from None
        identity = lambda value: (value.st_dev, value.st_ino)
        if not stat.S_ISDIR(live.st_mode) or identity(host) != identity(live) or identity(host) != identity(after):
            raise SafetyError('Photo persistent bind points to a replaced directory; preserve the running service and inspect recovery')


def _image_process_matches(actual, image):
    # Docker normalizes an unset image working directory (null) to an empty
    # container field. Both mean /; arbitrary overrides remain ineligible.
    return (all(actual.get(key) == image.get(key) for key in ('Cmd', 'Entrypoint'))
            and (actual.get('WorkingDir') or '/') == (image.get('WorkingDir') or '/'))


def check_target(c, *, runner=run):
    """Prove the exact owned private listener before sending any credentials."""
    root, _ = _paths(c)
    engine.preflight(c, 5, runner)
    extras.free_space(c, 'photos')
    expected = engine.recipe(c, 5)["services"]["immich-server"]
    ids = engine.docker(runner, "ps", "--filter", "status=running", "--filter",
        "label=com.docker.compose.project=" + naming.project_name(c, 5), "--filter",
        "label=com.docker.compose.service=immich-server", "--format", "{{.ID}}").stdout.split()
    if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", ids[0]):
        raise SafetyError("Photo target is not one running owned Immich service")
    rows = json.loads(engine.docker(runner, "inspect", ids[0]).stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise SafetyError("Cannot prove the photo container identity")
    row = rows[0]
    pinned = json.loads(engine.docker(runner, "image", "inspect", expected["image"]).stdout)
    if (not isinstance(pinned, list) or len(pinned) != 1 or row.get("Image") != pinned[0].get("Id")
            or not _image_process_matches(row.get('Config', {}), pinned[0].get('Config', {}))):
        raise SafetyError("Photo image or command overrides differ from the reviewed upstream image")
    labels = row["Config"].get("Labels") or {}
    wanted = {"systems.samarie.deployment": c["deployment_id"], "systems.samarie.stage": "5",
              "com.docker.compose.project": naming.project_name(c, 5),
              "com.docker.compose.service": "immich-server"}
    ports = row.get("NetworkSettings", {}).get("Ports", {}).get("2283/tcp")
    mounts = [m for m in row.get("Mounts", []) if m.get("Destination") == "/data"]
    env = dict(item.split('=', 1) for item in row['Config'].get('Env', []) if '=' in item)
    image_env = dict(item.split('=', 1) for item in pinned[0]['Config'].get('Env', []) if '=' in item)
    allowed_db = {k: v for k, v in {**image_env, **expected['environment']}.items() if k.startswith('DB_')}
    if (any(labels.get(k) != v for k, v in wanted.items()) or row["Config"].get("Image") != expected["image"]
            or ports != [{"HostIp": c["bind_address"], "HostPort": "2283"}]
            or len(mounts) != 1 or mounts[0].get("Type") != "bind" or mounts[0].get("RW") is not True
            or mounts[0].get("Source") != str(Path(c["storage"]["data_root"]) / "photos/upload")
            or {k: v for k, v in env.items() if k.startswith('DB_')} != allowed_db
            or any(m.get('Destination', '').startswith(('/app', '/usr/src/app', '/data/')) for m in row.get('Mounts', []))):
        raise SafetyError("Photo image, ownership, private port or originals mount differs from its reviewed deployment")
    dbids = engine.docker(runner, "ps", "--filter", "status=running", "--filter",
        "label=com.docker.compose.project=" + naming.project_name(c, 5), "--filter",
        "label=com.docker.compose.service=database", "--format", "{{.ID}}").stdout.split()
    if len(dbids) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', dbids[0]):
        raise SafetyError('Photo database is not one running owned service')
    db = json.loads(engine.docker(runner, 'inspect', dbids[0]).stdout)[0]
    dbexpected = engine.recipe(c, 5)['services']['database']
    dbimage = json.loads(engine.docker(runner, 'image', 'inspect', dbexpected['image']).stdout)[0]
    dblabels = db['Config'].get('Labels') or {}
    dbmounts = [m for m in db.get('Mounts', []) if m.get('Destination') == '/var/lib/postgresql/data']
    db_env = dict(item.split('=', 1) for item in db['Config'].get('Env', []) if '=' in item)
    db_image_env = dict(item.split('=', 1) for item in dbimage['Config'].get('Env', []) if '=' in item)
    postgres_key = lambda key: key.startswith(('PG', 'POSTGRES_'))
    allowed_postgres = {k: v for k, v in {**db_image_env, **dbexpected['environment']}.items() if postgres_key(k)}
    server_networks = set(row.get('NetworkSettings', {}).get('Networks', {}))
    db_networks = db.get('NetworkSettings', {}).get('Networks', {})
    if (any(dblabels.get(k) != v for k, v in {**wanted, 'com.docker.compose.service': 'database'}.items())
            or db['Config'].get('Image') != dbexpected['image'] or db.get('Image') != dbimage.get('Id')
            or not _image_process_matches(db['Config'], dbimage.get('Config', {}))
            or len(dbmounts) != 1 or dbmounts[0].get('Type') != 'bind' or dbmounts[0].get('RW') is not True
            or dbmounts[0].get('Source') != str(root/'appdata/immich-postgres')
            or {k: v for k, v in db_env.items() if postgres_key(k)} != allowed_postgres
            or any(m.get('Destination', '').startswith('/var/lib/postgresql/data/') for m in db.get('Mounts', []))
            or not server_networks.intersection(db_networks)
            or not any('database' in (db_networks[n].get('Aliases') or []) for n in server_networks.intersection(db_networks))):
        raise SafetyError('Photo database image, identity or datastore differs from its reserved directory')
    _live_bind(row, Path(c['storage']['data_root'])/'photos/upload', '/data')
    _live_bind(db, root/'appdata/immich-postgres', '/var/lib/postgresql/data')
    return {"container": ids[0], "image": row["Image"], "database": dbids[0],
            "directory": onboarding._identity(root/'appdata/immich-postgres')}


def _call(c, request, method, path, **kwargs):
    try:
        return request(method, catalog.stage_endpoints(c, 5)["immich-server"] + "/api" + path, **kwargs)
    except Exception:
        raise SafetyError("Immich did not confirm the request; existing accounts and assets were retained") from None


@contextmanager
def _session(c, request, value=None):
    value = _credentials(c) if value is None else value
    logged = _call(c, request, "POST", "/auth/login", json={k: value[k] for k in ("email", "password")})
    token = logged.get("accessToken") if isinstance(logged, dict) else None
    if (not isinstance(token, str) or not token or any(ord(ch) < 32 or ord(ch) == 127 for ch in token)
            or logged.get("isAdmin") is not True or logged.get("userEmail", "").lower() != value["email"].lower()
            or not isinstance(logged.get("userId"), str)):
        raise SafetyError("Immich did not authenticate the supplied existing administrator")
    headers = {"Authorization": "Bearer " + token}
    try:
        yield headers, logged["userId"]
    finally:
        _call(c, request, "POST", "/auth/logout", headers=headers)


def merge_policy(current, policy):
    """Reject schema drift; preserve every field outside the reviewed subset."""
    if not isinstance(current, dict):
        raise SafetyError("Immich configuration has an unknown schema")
    result = deepcopy(current)
    for key, value in policy.items():
        if key not in result or (isinstance(value, dict) != isinstance(result[key], dict)):
            raise SafetyError("Immich resource-policy fields differ from the supported native schema")
        if isinstance(value, dict):
            result[key] = merge_policy(result[key], value)
        else:
            if type(result[key]) is not type(value):
                raise SafetyError("Immich resource-policy field type changed")
            result[key] = value
    return result


def _owner(c, request, headers, owner_id):
    try:
        uuid.UUID(owner_id)
    except (ValueError, TypeError, AttributeError):
        raise SafetyError('Immich owner ID is invalid') from None
    value = _call(c, request, 'GET', '/admin/users/' + owner_id, headers=headers)
    if (not isinstance(value, dict) or value.get('id') != owner_id or value.get('isAdmin') is not True
            or ('quotaSizeInBytes' not in value)
            or (value['quotaSizeInBytes'] is not None and (type(value['quotaSizeInBytes']) is not int or value['quotaSizeInBytes'] < 0))):
        raise SafetyError('Immich owner or quota has an unknown native schema')
    return value


def plan(c, *, request=engine.request, runner=run):
    target = check_target(c, runner=runner)
    receipt = _receipt(c)
    credentials = _credentials(c)
    public = _call(c, request, "GET", "/server/config")
    if (not isinstance(public, dict) or type(public.get("isInitialized")) is not bool
            or type(public.get("isOnboarded")) is not bool or public.get("maintenanceMode") is True):
        raise SafetyError("Immich first-run state is unknown or maintenance is active")
    policy = extras.photo_policy(c)
    result = {"kind": "photos-owner-policy", **_binding(c), "target": target,
              "policy": policy, "initialized": public["isInitialized"], "onboarded": public["isOnboarded"],
              "private_input_hash": digest(credentials),
              "owner_quota_bytes": extras.options(c, 'photos').get('user_quota_gib', 2) * 1024 ** 3}
    if not public["isInitialized"]:
        if not receipt or receipt.get("eligible") is not True or receipt.get("state") not in {'reserved', 'claiming'}:
            raise SafetyError("Immich has no owner but its datastore was not reserved fresh; nothing was reset")
        if receipt.get('state') == 'claiming' and receipt.get('private_input_hash') != result['private_input_hash']:
            raise SafetyError('An unconfirmed photo owner claim has different input; inspect it before retrying')
        if len(credentials["password"]) < 12:
            raise SafetyError("A new photo owner requires a password of at least 12 characters; existing accounts were retained")
        result["action"] = 'retry_unconfirmed_first_owner' if receipt['state'] == 'claiming' else "create_first_owner"
    else:
        with _session(c, request, credentials) as (headers, owner):
            before = _call(c, request, "GET", "/admin/config", headers=headers)
            merge_policy(before, policy)
            account = _owner(c, request, headers, owner)
            result.update(action="verify_existing_owner", owner_id=owner, before_hash=digest(before),
                          owner_quota_before=account['quotaSizeInBytes'])
    result["review_hash"] = digest(result)
    return result


def apply(c, document, accepted_hash, *, request=engine.request, runner=run):
    if (not isinstance(document, dict) or accepted_hash != document.get("review_hash")
            or digest({k: v for k, v in document.items() if k != "review_hash"}) != accepted_hash):
        raise SafetyError("Photo setup requires the exact reviewed plan hash")
    with engine.deployment_lock(c):
        if plan(c, request=request, runner=runner) != document:
            raise SafetyError("Photo setup changed after review; make a fresh plan")
        credentials = _credentials(c)
        if digest(credentials) != document['private_input_hash']:
            raise SafetyError('Photo login input changed after review; no account was created')
        root, receipt_path = _paths(c)
        if document["action"] in {"create_first_owner", 'retry_unconfirmed_first_owner'}:
            receipt = _receipt(c)
            receipt["state"] = "claiming"
            receipt['private_input_hash'] = document['private_input_hash']
            write_json(receipt_path, receipt, replace=True)
            _call(c, request, "POST", "/auth/admin-sign-up", json=credentials)
        with _session(c, request, credentials) as (headers, owner):
            if document.get("owner_id", owner) != owner:
                raise SafetyError("Photo owner identity changed; existing accounts were retained")
            before = _call(c, request, "GET", "/admin/config", headers=headers)
            if "before_hash" in document and digest(before) != document["before_hash"]:
                raise SafetyError("Photo configuration changed during review; no settings were written")
            account = _owner(c, request, headers, owner)
            if 'owner_quota_before' in document and account['quotaSizeInBytes'] != document['owner_quota_before']:
                raise SafetyError('Photo owner quota changed during review; make a fresh plan')
            proposed = merge_policy(before, document["policy"])
            if proposed != before:
                backup = root / "backups" / ("photos-config-" + uuid.uuid4().hex + ".json")
                write_json(backup, before)
                _call(c, request, "PUT", "/admin/config", headers=headers, json=proposed)
                if _call(c, request, "GET", "/admin/config", headers=headers) != proposed:
                    raise SafetyError("Immich changed unrelated settings; inspect the private configuration backup")
            if account['quotaSizeInBytes'] != document['owner_quota_bytes']:
                write_json(root / 'backups' / ('photos-owner-quota-' + uuid.uuid4().hex + '.json'), account)
                _call(c, request, 'PUT', '/admin/users/' + owner, headers=headers,
                      json={'quotaSizeInBytes': document['owner_quota_bytes']})
                after = _owner(c, request, headers, owner)
                if (after['quotaSizeInBytes'] != document['owner_quota_bytes']
                        or any(after.get(key) != account.get(key) for key in
                               ('id', 'email', 'name', 'isAdmin', 'shouldChangePassword', 'oauthId', 'storageLabel'))):
                    raise SafetyError('Immich did not preserve the owner identity while applying its quota')
            if not document["onboarded"]:
                _call(c, request, "POST", "/system-metadata/admin-onboarding", headers=headers, json={"isOnboarded": True})
            public = _call(c, request, "GET", "/server/config")
            if public.get("isInitialized") is not True or public.get("isOnboarded") is not True:
                raise SafetyError("Immich did not retain owner onboarding")
        receipt = _receipt(c) or {"schema_version": 1, **_binding(c),
            "directory": onboarding._identity(root / "appdata/immich-postgres"), "eligible": False}
        receipt.update(state="verified", owner_id=owner)
        write_json(receipt_path, receipt, replace=receipt_path.exists())
        return {"ok": True, "owner_id": owner, "policy": document["policy"],
                'owner_quota_bytes': document['owner_quota_bytes'],
                "assets_imported": 0, "backup_restore_tested": False,
                "note": "Native login and resource defaults verified; photo upload and independent restore still require acceptance tests"}


def verify(c, *, request=engine.request, runner=run):
    document = plan(c, request=request, runner=runner)
    credentials = _credentials(c)
    if digest(credentials) != document['private_input_hash']:
        raise SafetyError('Photo login input changed during verification')
    if not document["initialized"] or not document["onboarded"]:
        raise SafetyError("Photo owner onboarding is incomplete")
    with _session(c, request, credentials) as (headers, owner):
        current = _call(c, request, "GET", "/admin/config", headers=headers)
        if merge_policy(current, document["policy"]) != current:
            raise SafetyError("Photo resource policy differs; review a fresh setup plan")
        if _owner(c, request, headers, owner)['quotaSizeInBytes'] != document['owner_quota_bytes']:
            raise SafetyError('Photo owner quota differs; review a fresh setup plan')
    return {"ok": True, "owner_id": owner, "policy": document["policy"], "assets_imported": 0,
            "backup_restore_tested": False, 'owner_quota_bytes': document['owner_quota_bytes']}
