#!/usr/bin/env python3
"""Build audited, reproducible release assets from one clean Git commit.

No tag, commit, push, package installation or service operation is performed.
The output must be a new directory outside the source checkout.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


MAX_SOURCE_FILE = 2 * 1024 * 1024
MAX_SOURCE_TOTAL = 64 * 1024 * 1024
MAX_SOURCE_FILES = 4096
REQUIRED_EXECUTABLES = frozenset({'setup', 'stack', 'scripts/release.sh'})
TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|rc)\.([1-9][0-9]*))?\Z')


class ReleaseError(RuntimeError):
    """An actionable release refusal with no private file contents."""


def _git(root, *arguments, data=None, check=True):
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith('GIT_'):
            environment.pop(name)
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(root), *arguments],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    if check and result.returncode:
        raise ReleaseError('A local Git read failed. Check that this is an initialized, readable source checkout.')
    return result


def _safe_name(name):
    if (not name or '\\' in name or ':' in name or len(name) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or name.startswith('/') or str(PurePosixPath(name)) != name
            or any(part in {'', '.', '..', '.git'} or part.endswith((' ', '.')) for part in name.split('/'))):
        raise ReleaseError('The Git tree contains an unsafe or unsupported source path.')
    return name


def _snapshot(root):
    """Read bounded canonical Git blob bytes, never checkout filters or archives."""
    commit = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ReleaseError('The source commit identity is invalid.')
    records = {}
    tree = _git(root, 'ls-tree', '-rz', '--full-tree', commit).stdout
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        try:
            header, raw_name = entry.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split(' ')
            name = _safe_name(raw_name.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ReleaseError('The Git tree contains an unsupported source entry.') from None
        if mode not in {'100644', '100755'} or kind != 'blob' or name in records:
            raise ReleaseError('Release sources must be regular files. Symlinks and submodules are refused.')
        records[name] = {'git_blob': oid, 'mode': 0o755 if mode == '100755' else 0o644}
    if not records or len(records) > MAX_SOURCE_FILES:
        raise ReleaseError('The source tree has an invalid or excessive file count.')
    for name in REQUIRED_EXECUTABLES:
        if name not in records or records[name]['mode'] != 0o755:
            raise ReleaseError('The Git tree must mark setup, stack and scripts/release.sh executable.')
    ordered = sorted(records)
    query = ''.join(records[name]['git_blob'] + '\n' for name in ordered).encode('ascii')
    sizes = _git(root, 'cat-file', '--batch-check', data=query).stdout.splitlines()
    if len(sizes) != len(ordered):
        raise ReleaseError('Git object inventory is incomplete.')
    total = 0
    for name, line in zip(ordered, sizes):
        try:
            oid, kind, size = line.decode('ascii').split(' ')
            size = int(size)
        except (ValueError, UnicodeError):
            raise ReleaseError('Git object inventory is invalid.') from None
        if oid != records[name]['git_blob'] or kind != 'blob' or not 0 <= size <= MAX_SOURCE_FILE:
            raise ReleaseError('A source file exceeds the bounded release file size.')
        records[name]['size'] = size
        total += size
    if total > MAX_SOURCE_TOTAL:
        raise ReleaseError('The source tree exceeds the bounded release size.')
    stream = io.BytesIO(_git(root, 'cat-file', '--batch', data=query).stdout)
    values = {}
    for name in ordered:
        record = records[name]
        expected = f"{record['git_blob']} blob {record['size']}\n".encode('ascii')
        if stream.readline(256) != expected:
            raise ReleaseError('Git object read did not match the reviewed source inventory.')
        value = stream.read(record['size'])
        if len(value) != record['size'] or stream.read(1) != b'\n':
            raise ReleaseError('Git returned an incomplete source object.')
        record['sha256'] = hashlib.sha256(value).hexdigest()
        values[name] = value
    if stream.read(1):
        raise ReleaseError('Git returned unexpected extra source data.')
    return commit, records, values


def _version(values, tag):
    match = TAG.fullmatch(tag)
    if match is None:
        raise ReleaseError('Use a release tag such as v0.2.0-rc.1 or v0.2.0.')
    major, minor, patch, phase, number = match.groups()
    expected = f'{major}.{minor}.{patch}'
    if phase:
        expected += {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[phase] + number
    literal = re.search(rb'^__version__ = "([A-Za-z0-9.+-]+)"\r?$', values.get('samurai_stack/__init__.py', b''), re.MULTILINE)
    if literal is None or literal.group(1).decode('ascii') != expected:
        raise ReleaseError('The requested tag does not match __version__ in the committed Git source.')
    return expected


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseError('A required committed release helper is unavailable.')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _new_output(root, output):
    path = Path(output).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReleaseError('The output directory and its parents cannot use symbolic links.')
    path = path.resolve()
    if path.exists():
        raise ReleaseError('The output directory already exists. Choose a new directory; no existing files are overwritten.')
    if path.is_relative_to(root):
        raise ReleaseError('Choose a new output directory outside the source checkout.')
    if not path.parent.is_dir():
        raise ReleaseError('The output parent directory must already exist.')
    return path


def _write(path, value, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)


def _archives(output, prefix, records, values, manifest):
    files = dict(values, **{'SOURCE-MANIFEST.json': manifest})
    with zipfile.ZipFile(output / (prefix + '.zip'), 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(files.items()):
            mode = records[name]['mode'] if name in records else 0o644
            info = zipfile.ZipInfo(prefix + '/' + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    fd = os.open(output / (prefix + '.tar.gz'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name, value in sorted(files.items()):
                info = tarfile.TarInfo(prefix + '/' + name)
                info.size = len(value)
                info.mode = records[name]['mode'] if name in records else 0o644
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                archive.addfile(info, io.BytesIO(value))


def build_release(root, output, tag, *, deny_file=None):
    root = Path(root).resolve()
    actual_root = Path(os.fsdecode(_git(root, 'rev-parse', '--show-toplevel').stdout).strip()).resolve()
    if actual_root != root:
        raise ReleaseError('Run the release builder against the top-level source checkout.')
    output = _new_output(root, output)
    if _git(root, 'status', '--porcelain=v1', '--untracked-files=no').stdout:
        raise ReleaseError('The tracked source tree is dirty. Commit the reviewed source before building release assets.')
    commit, records, values = _snapshot(root)
    version = _version(values, tag)
    reference = 'refs/tags/' + tag
    if _git(root, 'show-ref', '--verify', '--quiet', reference, check=False).returncode == 0:
        tagged = _git(root, 'rev-parse', '--verify', reference + '^{commit}').stdout.decode('ascii').strip()
        if tagged != commit:
            raise ReleaseError('That existing local tag identifies another commit. No tag was changed.')
    if 'SOURCE-MANIFEST.json' in values:
        raise ReleaseError('SOURCE-MANIFEST.json is generated for each release and cannot shadow a tracked source file.')
    with tempfile.TemporaryDirectory(prefix='samurai-release-snapshot-') as temporary:
        snapshot = Path(temporary)
        for name, value in values.items():
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, value, records[name]['mode'])
        audit = _module(snapshot / 'scripts/check-release.py', '_samurai_committed_release_audit')
        deny_terms = audit.load_deny_terms(Path(deny_file)) if deny_file is not None else ()
        report = audit.audit_tree(snapshot, deny_terms=deny_terms)
        if not report.ok:
            locations = ', '.join(f'{item.path}:{item.line}:{item.rule}' for item in report.findings[:12])
            raise ReleaseError('Committed source failed the redacted release audit: ' + locations)
        # The builders themselves come from the same Git snapshot as their
        # payload. A newer/older local source file cannot leak into an asset.
        setup = _module(snapshot / 'scripts/build-setup-bundle.py', '_samurai_committed_setup_builder')
        hardlinks = _module(snapshot / 'scripts/build-hardlink-repair.py', '_samurai_committed_hardlink_builder')
        manifest = (json.dumps({'schema_version': 1, 'project': 'samurai-stack', 'tag': tag,
            'version': version, 'commit': commit, 'files': records}, sort_keys=True, indent=2) + '\n').encode('utf-8')
        # Publish only after every source, version and hygiene check passes.
        # mkdir is exclusive, including when another process races this build.
        output.mkdir(mode=0o700)
        _write(output / 'SOURCE-MANIFEST.json', manifest)
        _archives(output, 'samurai-stack-' + tag, records, values, manifest)
        setup.build(output / 'samurai-setup.pyz', root=snapshot)
        setup.build_shell(output / 'samurai-setup.sh', root=snapshot)
        hardlinks.build(output / 'samurai-hardlinks.pyz', root=snapshot)
        sums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
                       for path in sorted(output.iterdir()))
        _write(output / 'SHA256SUMS', sums.encode('ascii'))
    return {'tag': tag, 'version': version, 'commit': commit,
            'output': str(output), 'source_files': len(records), 'artifacts': sorted(path.name for path in output.iterdir())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True, help='Version-matched label, for example v0.2.0-rc.1')
    parser.add_argument('--output', required=True, type=Path, help='New directory outside this checkout; parent must exist')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1], help='Advanced: source checkout root')
    parser.add_argument('--deny-file', type=Path, help='Optional private operator literal deny list, never copied to the release')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_release(args.repo, args.output, args.tag, deny_file=args.deny_file), indent=2, sort_keys=True))
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        print('Release build stopped: ' + (str(exc) if isinstance(exc, ReleaseError)
              else 'A bounded local file or source check failed. Existing files were kept.'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
