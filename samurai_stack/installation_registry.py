"""Private, host-bound installation configuration independent of source trees.

This is a configuration registry, never an adoption or media recovery tool.
Discovery reads only explicit candidates and managed systemd references. Saving
requires a current read-only storage verification, before any registry writes.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import uuid

from . import storage, naming
from .safety import SafetyError, digest, no_symlinks, run, validate_config, write_json

REGISTRY_ROOT = Path('/var/lib/samurai-stack/installations')
SETUP_ROOT = Path('/var/lib/samurai-stack/setup')
UNIT_ROOT = Path('/etc/systemd/system')
MAX_BYTES = 256 * 1024
MAX_CANDIDATES = 512


def _guard():
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise SafetyError('Installation recovery must run as root inside the target Linux guest.')
    if Path('/etc/pve').is_dir():
        raise SafetyError('Installation recovery must run inside a guest, not the Proxmox host.')


def host_identity():
    try:
        value = Path('/etc/machine-id').read_bytes()
    except OSError as exc:
        raise SafetyError('Cannot identify this Linux host for installation recovery.') from exc
    if not value.strip() or len(value) > 4096:
        raise SafetyError('Cannot identify this Linux host for installation recovery.')
    return hashlib.sha256(value).hexdigest()


def _uuid(value):
    try:
        result = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError('Installation identity must be a valid UUID.') from exc
    if value != result:
        raise SafetyError('Installation identity must use canonical lowercase UUID formatting.')
    return result


def _owner(info):
    if os.name == 'posix' and info.st_uid != 0:
        raise SafetyError('Installation recovery files and directories must be owned by root.')


def _directory(path, *, private=True, allow_sticky=False):
    path = no_symlinks(Path(path))
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise SafetyError('An installation recovery directory is occupied by a file.')
    _owner(info)
    writable = info.st_mode & (0o077 if private else 0o022)
    if os.name == 'posix' and writable and not (allow_sticky and not private and info.st_mode & stat.S_ISVTX):
        raise SafetyError('Installation recovery directories have unsafe permissions; existing files were retained.')
    return path


def _mkdir(path):
    """Create only missing private directories; never repair existing modes."""
    path = no_symlinks(Path(path).absolute())
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    # Public system parents are allowed, writable or unowned parents are not.
    _directory(current, private=False, allow_sticky=True)
    for parent in current.parents:
        _directory(parent, private=False, allow_sticky=True)
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        _directory(item)
    return _directory(path)


def _read(path, *, private=True):
    path = no_symlinks(Path(path))
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise SafetyError('Installation recovery requires bounded regular files without hard links.')
        _owner(info)
        if os.name == 'posix' and info.st_mode & (0o077 if private else 0o022):
            raise SafetyError('Installation recovery file permissions are unsafe; existing files were retained.')
        with path.open('rb') as stream:
            encoded = stream.read(MAX_BYTES + 1)
        if len(encoded) > MAX_BYTES:
            raise SafetyError('Installation recovery configuration is too large.')
        return encoded.decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise SafetyError('Installation recovery configuration could not be read safely.') from exc


def _json(path):
    try:
        result = json.loads(_read(path))
    except ValueError as exc:
        raise SafetyError('Installation recovery configuration is not valid JSON.') from exc
    if not isinstance(result, dict):
        raise SafetyError('Installation recovery configuration must be an object.')
    return result


def _config(path):
    result = _json(path)
    try:
        validate_config(result)
        _uuid(result['deployment_id'])
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError) as exc:
        raise SafetyError('Saved installation configuration is invalid; existing files were retained.') from exc
    return result


def _identity(c):
    identity = {'deployment_id': _uuid(c['deployment_id']), 'deployment_name': c['deployment_name'],
            'install_root': c['install_root'], 'data_root': c['storage']['data_root'],
            'expected_mount': c['storage']['expected_mount'], 'storage_mode': c['storage']['mode'],
            'puid': c['puid'], 'pgid': c['pgid'], 'role': c.get('role', 'all')}
    # An absent/empty optional map must not alter legacy canonical identities.
    # Once explicitly reserved, names remain immutable with the installation.
    if c.get('compose_projects'):
        identity['compose_projects'] = dict(c['compose_projects'])
    return identity


def _metadata(path, c):
    value = _json(path)
    saved = value.get('identity', {})
    if (isinstance(saved, dict) and saved.get('deployment_id') == c['deployment_id']
            and saved.get('compose_projects', {}) != c.get('compose_projects', {})):
        raise SafetyError('Compose project names are fixed for this saved installation. Restore its original compose_projects names; renaming existing projects requires a reviewed migration.')
    if (value.get('schema_version') != 1 or value.get('host_id') != host_identity()
            or value.get('identity') != _identity(c)):
        raise SafetyError('Saved installation belongs to another host or a conflicting storage/application identity.')
    return value


@contextmanager
def _lock(directory, name='registry.lock'):
    path = no_symlinks(directory / name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SafetyError('Installation registry is busy or interrupted; inspect its private lock before retrying.') from exc
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'pid': os.getpid(), 'host_id': host_identity()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        path.unlink()


def remember_verified(c, *, registry_root=REGISTRY_ROOT, runner=run):
    """Persist verified storage identity; archive reviewed mutable config updates.

    No boolean assertion or historical receipt substitutes for live verification.
    A crash after identity publication is resumable with the same identity.
    """
    _guard()
    validate_config(c)
    deployment = _uuid(c['deployment_id'])
    expected_identity = _identity(c)
    # All storage checks precede even creation of the registry directory.
    proof = storage.verify(c, runner)
    if (not isinstance(proof, dict) or proof.get('storage') != 'verified'
            or not isinstance(proof.get('mount'), dict)
            or any(proof['mount'].get(k) != c['storage']['expected_mount'][k]
                   for k in ('target', 'source', 'fstype'))):
        raise SafetyError('Storage verification did not confirm this installation; recovery state was not saved.')
    root = _mkdir(registry_root)
    with _lock(root, '.registry.lock'):
        # Serialize different UUIDs too: two installs must not concurrently
        # reserve the same application directory under separate bucket locks.
        children = list(root.iterdir())
        if len(children) > MAX_CANDIDATES:
            raise SafetyError('Too many installation registry entries; review the private registry manually.')
        for child in children:
            if child.name.startswith('.') or child.name == deployment:
                continue
            _uuid(child.name)
            _directory(child)
            if (child / 'site.json').exists():
                other = load(child.name, registry_root=root)
                if other['install_root'] == c['install_root']:
                    raise SafetyError('An existing installation already reserves this application directory; no configuration was overwritten.')
        return _remember(c, root, deployment, expected_identity)


def _remember(c, root, deployment, expected_identity):
    directory = _mkdir(root / deployment)
    with _lock(directory):
        identity_path, site_path = directory / 'identity.json', directory / 'site.json'
        if identity_path.exists():
            _metadata(identity_path, c)
        else:
            if site_path.exists():
                raise SafetyError('An unbound installation cache exists; it was not overwritten.')
            write_json(identity_path, {'schema_version': 1, 'host_id': host_identity(),
                                       'identity': expected_identity})
        if site_path.exists():
            previous = _config(site_path)
            _metadata(identity_path, previous)
            if previous == c:
                return site_path
            history = _mkdir(directory / 'history')
            archive = history / ('site-' + digest(previous) + '.json')
            if archive.exists():
                if _config(archive) != previous:
                    raise SafetyError('Installation history conflicts with its saved configuration.')
            else:
                write_json(archive, previous)
        write_json(site_path, c, replace=site_path.exists())
        return site_path


def load(deployment_id, *, registry_root=REGISTRY_ROOT):
    """Read a registered installation on this host; never probe or write media."""
    _guard()
    directory = _directory(_directory(registry_root) / _uuid(deployment_id))
    config = _config(directory / 'site.json')
    if config['deployment_id'] != deployment_id:
        raise SafetyError('Installation cache path does not match its deployment identity.')
    _metadata(directory / 'identity.json', config)
    return config


def state_directory(deployment_id, *, registry_root=REGISTRY_ROOT):
    """Stable setup progress location for an already registered installation."""
    load(deployment_id, registry_root=registry_root)
    return _mkdir(Path(registry_root) / _uuid(deployment_id) / 'setup-state')


def draft_directory(*, setup_root=SETUP_ROOT):
    """Stable private progress before the storage/deployment identity is known."""
    _guard()
    return _mkdir(Path(setup_root) / host_identity())


def _unit_config(unit, canonical=None):
    """Accept only the exact guarded config reference of our generated units."""
    text = _read(unit, private=False).replace('\r\n', '\n')
    markers = re.findall(r'^# Managed by Samurai Stack: ([^\r\n]+)$', text, re.M)
    if not markers:
        return None
    if len(markers) != 1:
        raise SafetyError('An existing Samurai service has conflicting ownership markers.')
    deployment = _uuid(markers[0])
    lines = re.findall(r'^ExecStartPre=(.*)$', text, re.M)
    if len(lines) != 1:
        raise SafetyError('An existing Samurai service lacks its unique guarded configuration reference.')
    try:
        argv = shlex.split(lines[0])
    except ValueError as exc:
        raise SafetyError('An existing Samurai service configuration reference is invalid.') from exc
    if len(argv) != 4 or argv[0] != '/usr/bin/python3' or argv[3] not in {'2', '3', '4', '5', '6'}:
        raise SafetyError('An existing Samurai service configuration reference is not recognized.')
    path = no_symlinks(Path(argv[2]))
    missing = not path.is_file()
    if missing:
        c = (canonical or {}).get(deployment)
        if c is None:
            raise SafetyError('An existing Samurai service references missing saved configuration. Restore its private configuration before starting a new installation.')
    else:
        c = _config(path)
    stage = argv[3]
    expected_parent = Path(c['install_root']) / 'stages' / naming.project_name(c, int(stage))
    # Legacy units used the common install-root site.json; retain read-only migration.
    allowed = {expected_parent / 'site.json'}
    if not missing:
        allowed.add(Path(c['install_root']) / 'site.json')
    if (path not in allowed or argv[1] != str(Path(c['install_root']) / 'runtime_guard.py')
            or deployment != c['deployment_id']
            or unit.name != 'samurai-' + c['deployment_name'] + '-' + stage + '.service'):
        raise SafetyError('An existing Samurai service conflicts with its saved deployment configuration.')
    if not missing:
        # Historical runtime directories can be root-owned mode 755. The
        # site itself remains private; no other identity may write its parent.
        _directory(path.parent, private=False)
    return c, path, missing


def discover(*, registry_root=REGISTRY_ROOT, unit_root=UNIT_ROOT, runtime_roots=(), config_paths=()):
    """Read trusted saved candidates without choosing conflicting snapshots.

    Records contain ``config``, ``path``, ``source``, related saved ``aliases``
    and fixed ``warnings``. Aliases may be older mutable snapshots, not copies.
    A canonical record supersedes older mutable snapshots of the same immutable
    identity. Without it, distinct snapshots remain separate for user review.
    Missing managed config, foreign host state or conflicting identity refuses.
    """
    _guard()
    candidates = []
    root = no_symlinks(Path(registry_root))
    if root.exists():
        _directory(root)
        children = list(root.iterdir())
        if len(children) > MAX_CANDIDATES:
            raise SafetyError('Too many installation registry entries; review the private registry manually.')
        for child in sorted(children):
            if child.name.startswith('.'):
                continue
            _uuid(child.name)
            _directory(child)
            if not (child / 'site.json').exists():
                # An interrupted first save can leave identity.json only.
                if (child / 'identity.json').exists():
                    continue
                raise SafetyError('An incomplete unbound installation cache requires review.')
            c = load(child.name, registry_root=root)
            candidates.append({'config': c, 'path': child / 'site.json', 'source': 'registry'})
    canonical = {row['config']['deployment_id']: row['config'] for row in candidates}
    units = no_symlinks(Path(unit_root))
    if units.exists():
        _directory(units, private=False)
        paths = sorted(units.glob('samurai-*.service'))
        if len(paths) > MAX_CANDIDATES:
            raise SafetyError('Too many Samurai service references; review the saved configuration manually.')
        for unit in paths:
            found = _unit_config(unit, canonical)
            if found:
                c, path, missing = found
                candidates.append({'config': c, 'path': path, 'source': 'service',
                                   'warnings': ['A managed service runtime configuration is missing; inspect application data before any redeployment.'] if missing else []})
    explicit = list(config_paths)
    for runtime in runtime_roots:
        runtime = no_symlinks(Path(runtime))
        if runtime.exists():
            _directory(runtime)
            explicit.extend([runtime / 'site.json', *sorted(runtime.glob('stages/*/site.json'))])
    if len(explicit) + len(candidates) > MAX_CANDIDATES:
        raise SafetyError('Too many saved installation candidates; review the private paths manually.')
    for path in explicit:
        path = no_symlinks(Path(path))
        if path.exists():
            _directory(path.parent)
            candidates.append({'config': _config(path), 'path': path, 'source': 'saved'})
    by_id, by_root = {}, {}
    for candidate in candidates:
        c = candidate['config']
        identity = _identity(c)
        for table, key in ((by_id, c['deployment_id']), (by_root, c['install_root'])):
            if key in table and table[key] != identity:
                raise SafetyError('Conflicting saved installations share an identity or application directory; existing services and data were retained.')
            table[key] = identity
    authoritative = {row['config']['deployment_id']: row for row in candidates if row['source'] == 'registry'}
    result, seen = [], {}
    for row in candidates:
        preferred = authoritative.get(row['config']['deployment_id'], row)
        key = digest(preferred['config'])
        if key not in seen:
            preferred = {**preferred, 'aliases': [], 'warnings': []}
            seen[key] = preferred
            result.append(preferred)
        if row['path'] != seen[key]['path'] and row['path'] not in seen[key]['aliases']:
            seen[key]['aliases'].append(row['path'])
        for warning in row.get('warnings', []):
            if warning not in seen[key]['warnings']:
                seen[key]['warnings'].append(warning)
    return result
