"""Reviewed, narrowly scoped disabling of unused installation-media sources.

Inspection is read-only. Call apply only after the operator approves its plan.
Plans contain private source text and must never be printed or saved publicly.
No package-manager commands, repository deletion or signature-policy edits occur.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
from urllib.parse import urlsplit

from .safety import SafetyError, atomic_write, digest, no_symlinks, write_json


FALSE_VALUES = {"no", "false", "without", "off", "disable", "0"}
MAX_SOURCE_BYTES = 1024 * 1024
LEGACY_MEDIA = re.compile(r"^\s*deb(?:-src)?\s+(?:\[[^\]\r\n]*\]\s+)?(?i:cdrom):")
LEGACY_NETWORK = re.compile(
    r"^\s*deb\s+(?:\[[^\]\r\n]*\]\s+)?(https?://\S+)\s+(\S+)\s+(.+)$", re.I)


def _owner(root):
    # A fake root permits unprivileged sandbox tests. Real host files must be root-owned.
    return 0 if root == Path('/').absolute() else getattr(os, 'geteuid', lambda: 0)()


def _paths(root):
    base = no_symlinks(root / 'etc/apt')
    fragments = no_symlinks(base / 'sources.list.d')
    for directory in (base, fragments):
        if directory.exists() and os.name == 'posix':
            info = directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != _owner(root) or info.st_mode & 0o022:
                raise SafetyError('APT source directories must be administrator-owned and not writable by other users')
    candidates = [base / 'sources.list']
    if fragments.exists():
        if not fragments.is_dir():
            raise SafetyError('APT source-fragment directory is not a directory; review it manually')
        candidates.extend(path for path in fragments.iterdir()
                          if not path.name.startswith('.')
                          and re.fullmatch(r'[A-Za-z0-9_.-]+\.(?:list|sources)', path.name))
    return sorted(path for path in candidates if path.exists() or path.is_symlink())


def _snapshot(root, path):
    no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SafetyError('APT sources must be regular files without hard links; review them manually')
    if os.name == 'posix' and (info.st_uid != _owner(root) or info.st_mode & 0o022):
        raise SafetyError('APT sources must be administrator-owned and not writable by other users')
    if info.st_size > MAX_SOURCE_BYTES:
        raise SafetyError('APT source file is unexpectedly large; review it manually')
    content = path.read_bytes()
    if len(content) > MAX_SOURCE_BYTES:
        raise SafetyError('APT source file is unexpectedly large; review it manually')
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise SafetyError('APT source encoding needs manual review') from None
    return {'path': path.relative_to(root).as_posix(),
            'sha256': hashlib.sha256(content).hexdigest(),
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'device': info.st_dev, 'inode': info.st_ino, 'mtime_ns': info.st_mtime_ns,
            'size': len(content)}, text


def _codename(root):
    values = {}
    # /etc/os-release commonly links to ../usr/lib/os-release on stock guests.
    release = (root / 'etc/os-release').resolve(strict=True)
    if not release.is_relative_to(root):
        raise SafetyError('OS release information escaped the selected guest root')
    no_symlinks(release)
    info = release.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
            or (os.name == 'posix' and (info.st_uid != _owner(root) or info.st_mode & 0o022))):
        raise SafetyError('OS release information needs administrator review')
    for line in release.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            name, raw = line.split('=', 1)
            try:
                pieces = shlex.split(raw)
            except ValueError:
                raise SafetyError('OS release information needs manual review') from None
            values[name] = pieces[0] if len(pieces) == 1 else ''
    name = values.get('VERSION_CODENAME', '')
    if values.get('ID') not in {'debian', 'ubuntu'} or not re.fullmatch(r'[a-z][a-z0-9]+', name):
        raise SafetyError('A supported Debian/Ubuntu OS identity is required before APT source repair')
    return name


def _os_network(uris, suites, components, codename):
    # Positive evidence of a binary OS archive; Docker's "stable" component does
    # not qualify. This checks configured mirrors, not their live availability.
    return ('main' in components and any(s == codename or s.startswith(codename + '-') for s in suites)
            and any(urlsplit(uri).scheme.lower() in {'http', 'https'} and urlsplit(uri).netloc
                    for uri in uris))


def _fields(lines):
    fields, locations, current, malformed = {}, {}, None, False
    for index, line in enumerate(lines):
        if line.lstrip().startswith('#'):
            continue
        if line[:1].isspace():
            if current is None:
                malformed = True
            else:
                fields[current].append(line.strip())
                locations[current][-1].append(index)
            continue
        name, colon, value = line.partition(':')
        if not colon or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*', name):
            malformed, current = True, None
            continue
        current = name.lower()
        fields.setdefault(current, []).append(value.strip())
        locations.setdefault(current, []).append([index])
    return {name: ' '.join(values) for name, values in fields.items()}, locations, malformed


def _rewrite_deb822(text, codename):
    chunks, current = [], []
    for line in text.splitlines(keepends=True):
        if not line.strip():
            if current:
                chunks.append(current)
                current = []
            chunks.append([line])
        else:
            current.append(line)
    if current:
        chunks.append(current)
    rewritten, count, networks = [], 0, 0
    for lines in chunks:
        if len(lines) == 1 and not lines[0].strip():
            rewritten.extend(lines)
            continue
        fields, locations, malformed = _fields(lines)
        enabled = fields.get('enabled', '').strip().lower() not in FALSE_VALUES
        types = fields.get('types', '').split()
        uris = fields.get('uris', '')
        schemes = set(re.findall(r'(?:^|\s)([A-Za-z][A-Za-z0-9+.-]*):', uris.lower()))
        media = enabled and bool(set(types) & {'deb', 'deb-src'}) and 'cdrom' in schemes
        if media:
            if malformed or any(len(items) != 1 for items in locations.values()):
                raise SafetyError('Installation-media APT stanza is ambiguous; review that stanza manually')
            if schemes != {'cdrom'}:
                raise SafetyError('APT stanza mixes installation media with other sources; split it manually before repair')
            changed = list(lines)
            if 'enabled' in locations:
                indices = locations['enabled'][0]
                ending = '\r\n' if changed[indices[0]].endswith('\r\n') else '\n' if changed[indices[0]].endswith('\n') else ''
                changed[indices[0]] = 'Enabled: no' + ending
                for index in indices[1:]:
                    changed[index] = ''
            else:
                ending = '\r\n' if any(line.endswith('\r\n') for line in changed) else '\n'
                if changed and not changed[-1].endswith('\n'):
                    changed[-1] += ending
                changed.append('Enabled: no' + ending)
            rewritten.extend(changed)
            count += 1
        else:
            rewritten.extend(lines)
            if (enabled and not malformed and 'deb' in types
                    and _os_network(uris.split(), fields.get('suites', '').split(),
                                    fields.get('components', '').split(), codename)):
                networks += 1
    return ''.join(rewritten), count, networks


def _rewrite_legacy(text, codename):
    rewritten, count, networks = [], 0, 0
    for line in text.splitlines(keepends=True):
        active = line.split('#', 1)[0]
        if LEGACY_MEDIA.match(active):
            rewritten.append('# ' + line)
            count += 1
        else:
            rewritten.append(line)
            match = LEGACY_NETWORK.match(active.strip())
            if match and _os_network([match[1]], [match[2]], match[3].split(), codename):
                networks += 1
    return ''.join(rewritten), count, networks


def inspect_install_media(root=Path('/')):
    """Return a private review plan without changing sources or running APT."""
    root = no_symlinks(Path(root).absolute())
    codename = _codename(root)
    plan = {'schema': 1, 'kind': 'apt-install-media', 'root': str(root),
            'sources': [], 'changes': [], 'network_sources': 0}
    for path in _paths(root):
        snapshot, text = _snapshot(root, path)
        replacement, count, networks = (_rewrite_deb822(text, codename)
                                        if path.suffix == '.sources' else _rewrite_legacy(text, codename))
        plan['sources'].append(snapshot)
        plan['network_sources'] += networks
        if count:
            plan['changes'].append({'path': snapshot['path'], 'before_sha256': snapshot['sha256'],
                                    'after_text': replacement, 'disabled_entries': count})
    if plan['changes'] and not plan['network_sources']:
        raise SafetyError('No active HTTP(S) OS mirror was confirmed; configure an online OS mirror manually before disabling installation media')
    plan['review_hash'] = digest(plan)
    return plan


def _replace_source(root, path, snapshot, content):
    fd, temporary = tempfile.mkstemp(prefix='.samurai-apt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if os.name == 'posix':
                os.fchown(stream.fileno(), snapshot['uid'], snapshot['gid'])
            os.fchmod(stream.fileno(), snapshot['mode']) if hasattr(os, 'fchmod') else os.chmod(temporary, snapshot['mode'])
            stream.write(content.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        current, _ = _snapshot(root, path)
        if current != snapshot:
            raise SafetyError('APT sources changed after review; inspect a fresh repair plan')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_install_media(plan, backup_dir, *, root=Path('/')):
    """Apply only an unchanged, operator-approved plan; retain private originals."""
    root = no_symlinks(Path(root).absolute())
    supplied = dict(plan)
    recorded = supplied.pop('review_hash', None)
    if (supplied.get('schema') != 1 or supplied.get('kind') != 'apt-install-media'
            or supplied.get('root') != str(root) or recorded != digest(supplied)):
        raise SafetyError('APT repair review failed its integrity check; inspect a fresh plan')
    if inspect_install_media(root) != plan:
        raise SafetyError('APT sources changed after review; inspect a fresh repair plan')
    if not plan['changes']:
        return {'changed': [], 'backup_directory': None}
    if os.name == 'posix' and root == Path('/').absolute() and os.geteuid() != 0:
        raise SafetyError('APT source repair requires root in the new guest')
    backup_dir = no_symlinks(Path(backup_dir))
    parent = backup_dir.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or (os.name == 'posix' and (parent.st_uid != _owner(root) or parent.st_mode & 0o077)):
        raise SafetyError('APT repair backup parent must be private and administrator-owned')
    if backup_dir.exists():
        raise SafetyError('APT repair requires a fresh backup directory; retain the existing evidence')
    backup_dir.mkdir(mode=0o700)
    snapshots = {item['path']: item for item in plan['sources']}
    manifest = {'schema': 1, 'kind': 'apt-install-media-backup', 'review_hash': recorded,
                'state': 'prepared', 'files': [], 'changed': []}
    for index, change in enumerate(plan['changes'], 1):
        path = root / change['path']
        snapshot, text = _snapshot(root, path)
        if snapshot != snapshots[change['path']]:
            raise SafetyError('APT sources changed after review; inspect a fresh repair plan')
        name = f'source-{index:04d}.txt'
        atomic_write(backup_dir / name, text)
        manifest['files'].append({'path': change['path'], 'backup': name,
                                  'original': snapshot,
                                  'after_sha256': hashlib.sha256(change['after_text'].encode('utf-8')).hexdigest()})
    write_json(backup_dir / 'manifest.json', manifest)
    # All backups exist before the first source replacement; recheck the entire
    # inventory so an added/edited mirror invalidates the reviewed proposal.
    if inspect_install_media(root) != plan:
        raise SafetyError('APT sources changed after review; private backups were retained')
    try:
        for change in plan['changes']:
            _replace_source(root, root / change['path'], snapshots[change['path']], change['after_text'])
            manifest['changed'].append(change['path'])
            manifest['state'] = 'applying'
            write_json(backup_dir / 'manifest.json', manifest, replace=True)
        manifest['state'] = 'complete'
        write_json(backup_dir / 'manifest.json', manifest, replace=True)
    except Exception:
        manifest['state'] = 'failed'
        try:
            write_json(backup_dir / 'manifest.json', manifest, replace=True)
        except Exception:
            pass
        raise SafetyError('APT source repair did not finish; private originals and progress were retained for review') from None
    return {'changed': manifest['changed'], 'backup_directory': str(backup_dir)}
