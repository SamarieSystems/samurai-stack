#!/usr/bin/env python3
"""Installed standalone before-start guard. No imports from a moving checkout."""
import json
from pathlib import Path
import re
import subprocess
import sys

DIRECTORIES = ['library/movies', 'library/tv', 'library/music',
               'downloads/usenet/complete', 'downloads/usenet/incomplete',
               'downloads/torrents/movies', 'downloads/torrents/tv', 'downloads/torrents/manual', 'downloads/torrents/incomplete',
               'downloads/music/complete', 'downloads/music/incomplete', 'photos/upload', 'photos/external']


def stable_mount(row):
    """Canonical local block filesystems by UUID; keep subvolume/export identity."""
    result = {key: row[key] for key in ('target', 'source', 'fstype')}
    if result['fstype'] in {'ext4', 'xfs', 'btrfs'} and result['source'].startswith('/dev/'):
        identifier = row.get('uuid')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', identifier):
            raise ValueError('local block filesystem UUID unavailable')
        suffix = result['source'][result['source'].find('['):] if '[' in result['source'] else ''
        result['source'] = 'UUID=' + identifier + suffix
    return result


class NetworkPending(ValueError):
    pass


def network_ready(config, *, require_default=False):
    """network-online.target does not prove the configured LAN is usable yet."""
    try:
        result = subprocess.run(['ip', '-j', '-4', 'address', 'show'], capture_output=True,
                                text=True, check=True, timeout=10)
        rows = json.loads(result.stdout)
        interfaces = [row for row in rows
                      if 'UP' in row.get('flags', []) and any(
                          addr.get('local') == config['bind_address'] for addr in row.get('addr_info', []))]
        if len(interfaces) != 1 or not isinstance(interfaces[0].get('ifname'), str):
            raise ValueError('configured LAN address unavailable')
        if not require_default:
            return
        routable = {row.get('ifname') for row in rows if row.get('ifname') != 'lo'
                    and 'UP' in row.get('flags', []) and any(
                        isinstance(addr.get('local'), str) and not addr['local'].startswith('127.')
                        for addr in row.get('addr_info', []))}
        result = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'default'], capture_output=True,
                                text=True, check=True, timeout=10)
        if not any(row.get('dst') == 'default' and row.get('dev') in routable
                   and row.get('type', 'unicast') == 'unicast' and row.get('table', 'main') in {'main', 254}
                   and 'linkdown' not in row.get('flags', []) for row in json.loads(result.stdout)):
            raise ValueError('configured LAN default route unavailable')
    except Exception:
        raise NetworkPending('configured LAN address/default IPv4 route is not ready') from None


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
        network_ready(c, require_default=len(sys.argv) > 2 and sys.argv[2] == '3')
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
        result = subprocess.run(['findmnt','--json','--target',str(root),'--output','TARGET,SOURCE,FSTYPE,OPTIONS,UUID'], capture_output=True, text=True, check=True, timeout=10)
        rows = json.loads(result.stdout)['filesystems']
        if len(rows) != 1:
            raise ValueError('mount identity ambiguous')
        found = stable_mount(rows[0]) if expected['source'].startswith('UUID=') else rows[0]
        if any(found[k] != expected[k] for k in ['target','source','fstype']):
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
    except NetworkPending:
        print('Network guard delayed startup: the configured LAN address or required VPN IPv4 route is not ready. The owned service will retry.', file=sys.stderr)
        return 1
    except Exception:
        print('Storage guard refused startup: check the exact mount and deployment marker. No local fallback.',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
