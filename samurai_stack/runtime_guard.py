#!/usr/bin/env python3
"""Installed standalone before-start guard. No imports from a moving checkout."""
import json
from pathlib import Path
import subprocess
import sys

DIRECTORIES = ['library/movies', 'library/tv', 'library/music',
               'downloads/usenet/complete', 'downloads/usenet/incomplete',
               'downloads/torrents/movies', 'downloads/torrents/tv', 'downloads/torrents/manual', 'downloads/torrents/incomplete',
               'downloads/music/complete', 'downloads/music/incomplete', 'photos/upload', 'photos/external']


def clean_path(path):
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError('managed path contains a symlink')


def main():
    try:
        c = json.loads(Path(sys.argv[1]).read_text())
        app = Path(c['install_root'])
        clean_path(app)
        install_marker = app / '.samurai-install.json'
        clean_path(install_marker)
        if json.loads(install_marker.read_text()).get('deployment_id') != c['deployment_id']:
            raise ValueError('installation ownership mismatch')
        if len(sys.argv) > 2 and sys.argv[2] == '4':
            return 0  # Management deliberately remains available during a NAS outage.
        root = Path(c['storage']['data_root'])
        for p in [root, *root.parents]:
            if p.is_symlink():
                raise ValueError('data path contains a symlink')
        marker = root / '.samurai-storage.json'
        if marker.is_symlink():
            raise ValueError('storage marker is a symlink')
        expected = c['storage']['expected_mount']
        result = subprocess.run(['findmnt','--json','--target',str(root),'--output','TARGET,SOURCE,FSTYPE,OPTIONS'], capture_output=True, text=True, check=True, timeout=10)
        rows = json.loads(result.stdout)['filesystems']
        if len(rows) != 1 or any(rows[0][k] != expected[k] for k in ['target','source','fstype']):
            raise ValueError('mount identity mismatch')
        if 'rw' not in rows[0]['options'].split(','):
            raise ValueError('data filesystem is read-only')
        ownership = json.loads(marker.read_text())
        if ownership.get('deployment_id') != c['deployment_id'] or ownership.get('expected_mount') != expected:
            raise ValueError('storage marker mismatch')
        for relative in DIRECTORIES:
            path = root / relative
            clean_path(path)
            if not path.is_dir():
                raise ValueError('required data directory missing')
        # Permission readback must use the application identity, not root.
        import os
        if os.geteuid() == 0:
            child = os.fork()
            if child == 0:
                os.setgroups([])
                os.setgid(c['pgid'])
                os.setuid(c['puid'])
                good = all(os.access(root / p, os.R_OK | os.W_OK | os.X_OK) for p in DIRECTORIES)
                os._exit(0 if good else 1)
            _, status = os.waitpid(child, 0)
            if status:
                raise ValueError('media identity permissions denied')
    except Exception:
        print('Storage guard refused startup: check the exact mount and deployment marker. No local fallback.',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
