"""Explicit reviewed upgrade from volatile block names to filesystem UUIDs.

Only current generated control files are replaced. Media, application databases,
credentials, Compose recipes and running services are never changed here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import engine, installation_registry as registry, onboarding, storage
from .safety import SafetyError, atomic_write, digest, no_symlinks, validate_config, write_json

PREDECESSORS = {
    'runtime_guard.py': {'55664c1d3d0f1718ab4b5bf977ef6b503ce3c0d98130ddf7bc0564104520c560'},
    'capacity_guard.py': {'6f1224b10b4fb0856eaecd26a48432dbafa07f2b67920bded38654f23df22a69'},
}
MAX_BYTES = 2 * 1024 * 1024


def _guard():
    engine.linux_only()
    if os.geteuid() != 0:
        raise SafetyError('Review/apply storage identity recovery as root in the intended guest')


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _owner(info):
    if os.name == 'posix' and info.st_uid != 0:
        raise SafetyError('Storage recovery control files must belong to root')


def _read(path, *, marker=False):
    path = no_symlinks(Path(path))
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
            or os.name == 'posix' and info.st_mode & 0o022):
        raise SafetyError('Storage recovery input is not a bounded owned regular control file')
    if not marker:
        _owner(info)
    return path.read_bytes().decode('utf-8')


def _json(path, *, marker=False):
    value = json.loads(_read(path, marker=marker).lstrip('\ufeff'))
    if not isinstance(value, dict):
        raise SafetyError('Storage recovery control input must be a JSON object')
    return value


def observed(config, *, runner=None):
    """Read-only evidence; observed UUIDs are not automatic recovery authority."""
    runner = runner or engine.run
    result = {}
    for label, path in [('data', config['storage']['data_root']), ('app', config['install_root'])]:
        value = storage.mount_identity(path, runner)
        stable = storage.persistent_identity(value)
        result[label] = {key: stable[key] for key in ('target', 'source', 'fstype')}
    return result


def _known_uuid(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value):
        raise SafetyError(label + ' must be an explicitly confirmed filesystem UUID')
    return value


def _identity(path, old, expected_app, runner, *, directory=True):
    """An operator-confirmed filesystem plus the original inode authorizes upgrade."""
    if not isinstance(old, dict) or any(type(old.get(key)) is not int for key in ('device', 'inode')):
        raise SafetyError('Account/subtitle receipt has an invalid original path identity')
    path = no_symlinks(path)
    info = path.stat()
    if (info.st_ino != old['inode'] or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise SafetyError('An application inode changed; verify its backup/account restore separately')
    mounted = storage.persistent_identity(storage.mount_identity(path, runner))
    current = {key: mounted[key] for key in ('target', 'source', 'fstype')}
    if current != expected_app or old.get('filesystem', current) != current:
        raise SafetyError('Application receipt is on another filesystem; no identity was rebound')
    after = path.stat()
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        raise SafetyError('Application identity changed during recovery inspection')
    return {**old, 'device': info.st_dev, 'inode': info.st_ino, 'filesystem': current}


def plan(config, config_path, *, expected_data_uuid, expected_app_uuid, runner=None, registry_root=None):
    _guard()
    validate_config(config)
    runner, registry_root = runner or engine.run, Path(registry_root or registry.REGISTRY_ROOT)
    if config['storage']['mode'] != 'local' or config['storage']['expected_mount']['fstype'] not in {'ext4', 'xfs', 'btrfs'}:
        raise SafetyError('This recovery upgrades only local block-backed Linux filesystem identities')
    data_uuid = _known_uuid(expected_data_uuid, 'Original media filesystem UUID')
    app_uuid = _known_uuid(expected_app_uuid, 'Original application filesystem UUID')
    old_mount = config['storage']['expected_mount']
    if not old_mount['source'].startswith(('/dev/', 'UUID=')):
        raise SafetyError('Expected local storage source is not a supported block identity')
    root = no_symlinks(Path(config['install_root']))
    owner = _json(root / '.samurai-install.json')
    if owner.get('deployment_id') != config['deployment_id']:
        raise SafetyError('Storage recovery does not own this installation')
    mounts = observed(config, runner=runner)
    new_mount, app_mount = mounts['data'], mounts['app']
    # A Btrfs subvolume suffix is part of the original mount identity.
    suffix = old_mount['source'][old_mount['source'].find('['):] if '[' in old_mount['source'] else ''
    if (new_mount != {**old_mount, 'source': 'UUID=' + data_uuid + suffix}
            or app_mount['source'].split('[', 1)[0] != 'UUID=' + app_uuid):
        raise SafetyError('Confirmed UUIDs do not match the currently mounted filesystems')
    marker_path = Path(config['storage']['data_root']) / '.samurai-storage.json'
    marker = _json(marker_path, marker=True)
    if marker.get('deployment_id') != config['deployment_id'] or marker.get('expected_mount') != old_mount:
        raise SafetyError('Original deployment storage marker does not match; no adoption is permitted')
    updated = deepcopy(config)
    updated['storage']['expected_mount'] = new_mount
    storage.check_mount(updated, runner)
    for relative in storage.DIRECTORIES:
        if not no_symlinks(Path(config['storage']['data_root']) / relative).is_dir():
            raise SafetyError('An owned media directory is missing; resolve storage before recovery')
    storage.as_media_identity(config, lambda: storage.check_permissions(config))
    old_digest, new_digest = digest(config), digest(updated)
    configured = no_symlinks(Path(config_path).absolute())
    if _json(configured) != config:
        raise SafetyError('Selected configuration differs from the reviewed site')
    paths = {configured, root / 'site.json', marker_path}
    bucket = registry_root / config['deployment_id']
    if bucket.exists():
        if _json(bucket / 'site.json') != config:
            raise SafetyError('Canonical recovery configuration conflicts with this selected site')
        metadata = _json(bucket / 'identity.json')
        if metadata.get('host_id') != engine.host_identity() or metadata.get('identity') != registry._identity(config):
            raise SafetyError('Canonical recovery identity belongs to another host or deployment')
        paths.update({bucket / 'site.json', bucket / 'identity.json'})
    for stage in range(2, 7):
        folder = engine.stage_path(config, stage)
        site = folder / 'site.json'
        if site.exists():
            if _json(site) != config:
                raise SafetyError('A saved stage has a different site; review the installations separately')
            paths.add(site)
        for name in ('configuration-report.json', 'manual-enrollment.json'):
            if (folder / name).exists():
                paths.add(folder / name)
    for name in ('capacity-policy.json', 'subtitle-setup.json', 'onboarding.json', 'management-onboarding.json'):
        if (root / 'state' / name).exists():
            paths.add(root / 'state' / name)
    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: rewrite(item) for key, item in value.items()}
        if result.get('expected_mount') == old_mount:
            result['expected_mount'] = new_mount
        if result.get('config_digest') == old_digest:
            result['config_digest'] = new_digest
        return result
    changes = []
    def append(path, before, after):
        if before == after:
            return
        info = path.stat()
        changes.append({'path': str(path), 'before_sha256': _sha(before), 'after_sha256': _sha(after),
                        'before': before, 'after': after, 'mode': stat.S_IMODE(info.st_mode),
                        'uid': info.st_uid, 'gid': info.st_gid})
    for path in sorted(paths):
        if not path.exists():
            continue
        before = _read(path, marker=path == marker_path)
        value = json.loads(before.lstrip('\ufeff'))
        if path.name == 'site.json' and value != config:
            raise SafetyError('A saved site alias conflicts with this selected installation')
        after = rewrite(value)
        if path.parent == root / 'state':
            if (value.get('deployment_id') != config['deployment_id']
                    or value.get('host_id') != engine.host_identity()):
                raise SafetyError('An installed receipt belongs to another deployment')
            if path.name == 'capacity-policy.json':
                if value.get('host_id') != engine.host_identity() or value.get('config_digest') != old_digest or value.get('config') != config:
                    raise SafetyError('Capacity policy is not bound to the original selected site')
                after.update(config=updated, config_digest=new_digest, app_mount=app_mount)
            elif path.name == 'subtitle-setup.json':
                after['target']['directory'] = _identity(root / 'appdata/bazarr', value['target']['directory'], app_mount, runner)
            elif path.name in {'onboarding.json', 'management-onboarding.json'}:
                for service, row in after.get('apps', {}).items():
                    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', service):
                        raise SafetyError('Account receipt service path is invalid')
                    if row.get('directory'):
                        row['directory'] = _identity(root / 'appdata' / service, row['directory'], app_mount, runner)
                    if row.get('database_identity'):
                        db = {'dockge': 'dockge.db', 'uptime-kuma': 'kuma.db'}.get(service)
                        if db is None:
                            raise SafetyError('Unsupported management database identity')
                        row['database_identity'] = _identity(root / 'appdata' / service / db, row['database_identity'], app_mount, runner, directory=False)
        if after != value:
            append(path, before, json.dumps(after, sort_keys=True, indent=2) + '\n')
    for name, predecessors in PREDECESSORS.items():
        path = root / name
        if not path.exists():
            continue
        before, after = _read(path), Path(__file__).with_name(name).read_text()
        if _sha(before) not in predecessors | {_sha(after)}:
            raise SafetyError('An installed guard has unknown edits; recovery retained it')
        append(path, before, after)
    document = {'schema_version': 1, 'kind': 'storage-uuid-upgrade', 'deployment_id': config['deployment_id'],
                'host_id': engine.host_identity(), 'config_digest': old_digest, 'config_path': str(configured),
                'expected_data_uuid': data_uuid, 'expected_app_uuid': app_uuid, 'mounts': mounts,
                'registry_root': str(registry_root), 'changes': changes,
                'effects': ['Replace only listed current generated control files with private before-byte backups.',
                            'Retain filesystem type, mountpoint, deployment marker and application inode checks.',
                            'Do not move/delete media, modify accounts, alter VPNs or start/stop services.']}
    document['review_hash'] = digest(document)
    return document


def apply(config, reviewed, *, runner=None, registry_root=None):
    _guard()
    document = deepcopy(reviewed)
    accepted = document.pop('review_hash', None)
    if not accepted or digest(document) != accepted or document.get('kind') != 'storage-uuid-upgrade':
        raise SafetyError('Apply requires an intact reviewed storage UUID upgrade plan')
    with engine.deployment_lock(config):
        current = plan(config, document['config_path'], expected_data_uuid=document['expected_data_uuid'],
                       expected_app_uuid=document['expected_app_uuid'], runner=runner, registry_root=registry_root)
        if current != reviewed:
            raise SafetyError('Recovery inputs changed after review; no control files were replaced')
        backup = no_symlinks(Path(config['install_root']) / 'backups' / ('storage-uuid-' + accepted + '.json'))
        if not backup.parent.is_dir():
            raise SafetyError('Existing private backup directory is missing')
        if backup.exists():
            if json.loads(_read(backup)) != reviewed:
                raise SafetyError('Recovery backup already exists with different content')
        else:
            write_json(backup, reviewed)
        applied = []
        try:
            for row in document['changes']:
                path = Path(row['path'])
                if _sha(_read(path, marker=path.name == '.samurai-storage.json')) != row['before_sha256']:
                    raise SafetyError('A control file changed during recovery; inspect the saved backup')
                atomic_write(path, row['after'], replace=True, mode=row['mode'])
                applied.append(row)
                if os.name == 'posix':
                    os.chown(path, row['uid'], row['gid'])
        except BaseException:
            for row in reversed(applied):
                atomic_write(row['path'], row['before'], replace=True, mode=row['mode'])
                if os.name == 'posix':
                    os.chown(row['path'], row['uid'], row['gid'])
            raise
        return {'ok': True, 'status': 'upgraded', 'control_files_replaced': len(applied), 'backup': str(backup),
                'services_changed': False, 'media_files_changed': 0,
                'next': 'Run the saved stage verification; restart its owned systemd services only after the guard passes.'}
