#!/usr/bin/env python3
"""Opt-in real first-boot test using a local qBit image and no external network."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from samurai_stack import engine


def docker(*args, check=True):
    result = subprocess.run(['docker', '--context', 'default', *map(str, args)], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('Temporary qBit test command failed; inspect its private scratch directory')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Existing local LinuxServer qBittorrent image; never pulls')
    parser.add_argument('--acknowledge-temporary-container', action='store_true', required=True)
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Run this optional smoke test on Linux')
    docker('image', 'inspect', args.image)
    token = uuid.uuid4().hex
    name = 'samurai-qbit-smoke-' + token[:12]
    scratch = Path(tempfile.mkdtemp(prefix='samurai-qbit-smoke-'))
    c = {'install_root': str(scratch), 'puid': os.getuid(), 'pgid': os.getgid()}
    secret_root = scratch / 'secrets'
    secret_root.mkdir(mode=0o700)
    for key, value in [('qbit_username', 'stackadmin'), ('qbit_password', secrets.token_urlsafe(32))]:
        path = secret_root / key
        path.write_text(value + '\n')
        path.chmod(0o600)
    config = scratch / 'appdata/qbittorrent'
    config.mkdir(parents=True)
    engine.seed_qbittorrent(c)
    created = False
    try:
        docker('run', '--detach', '--rm', '--pull', 'never', '--name', name,
               '--label', 'io.samurai.smoke=' + token, '--network', 'none',
               '--cpus', '0.5', '--memory', '512m', '--pids-limit', '128',
               '--env', 'PUID=' + str(c['puid']), '--env', 'PGID=' + str(c['pgid']),
               '--env', 'WEBUI_PORT=8081', '--mount', 'type=bind,src=' + str(config) + ',dst=/config', args.image)
        created = True
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            check = docker('exec', name, 'curl', '--silent', '--fail', 'http://127.0.0.1:8081/api/v2/app/version', check=False)
            # Unauthenticated API must fail, but the HTTP server must respond.
            probe = docker('exec', name, 'curl', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}', 'http://127.0.0.1:8081/api/v2/app/version', check=False)
            if probe.stdout.strip() == '403':
                break
            if probe.stdout.strip() == '200':
                raise RuntimeError('Unauthenticated qBit API unexpectedly allowed access')
            time.sleep(2)
        else:
            raise RuntimeError('qBit did not become ready within the smoke-test deadline')
        creds = (secret_root / 'qbit_password').read_text().rstrip('\r\n')
        # Supply the synthetic test password through stdin, not an argv value.
        import urllib.parse
        body = urllib.parse.urlencode(dict(username='stackadmin', password=creds))
        login = subprocess.run(['docker','--context','default','exec','-i',name,'curl','--silent','--fail','--cookie-jar','/tmp/samurai-cookie','--data-binary','@-','http://127.0.0.1:8081/api/v2/auth/login'], input=body, capture_output=True, text=True, timeout=15)
        # qBit 5.2 returns 204 with a session cookie; older versions return Ok.
        if login.returncode or login.stdout.strip() not in ('', 'Ok.'):
            raise RuntimeError('Seeded qBit password did not authenticate')
        response = docker('exec',name,'curl','--silent','--fail','--cookie','/tmp/samurai-cookie','http://127.0.0.1:8081/api/v2/app/preferences')
        prefs = json.loads(response.stdout)
        if prefs.get('current_network_interface') != 'tun0' or prefs.get('bypass_local_auth') is not False:
            raise RuntimeError('qBit startup binding/authentication preferences were not honored')
        result = {'image':args.image, 'seeded_password_verified':True, 'unauthenticated_api':'refused',
                  'binding':prefs['current_network_interface'], 'webui_port':prefs.get('web_ui_port'),
                  'network':'none', 'host_ports':[], 'personal_data_mounted':False, 'scratch_directory':str(scratch)}
        (scratch / 'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        return 0
    finally:
        if created:
            actual = docker('inspect', name, '--format', '{{index .Config.Labels "io.samurai.smoke"}}', check=False)
            if actual.returncode == 0 and actual.stdout.strip() == token:
                docker('stop','--time','10',name,check=False)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('ERROR: ' + str(exc),file=sys.stderr)
        raise SystemExit(1)
