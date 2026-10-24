#!/usr/bin/env python3
"""Validate catalog scenarios using Compose itself; never start containers."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from samurai_stack.cli import default_config
from samurai_stack import catalog


def scenarios():
    basic = default_config()
    for acceleration in ['cpu', 'intel', 'amd', 'nvidia']:
        c = deepcopy(basic)
        c['hardware']['acceleration'] = acceleration
        yield 'media-' + acceleration, c, 2
    c = deepcopy(basic)
    c['features']['usenet'] = False
    c['downloads']['prefer'] = 'torrents'
    yield 'media-torrent-only', c, 2
    for provider, protocol, forwarding in [('protonvpn', 'wireguard', False), ('protonvpn', 'wireguard', True),
                                         ('protonvpn', 'openvpn', True), ('mullvad', 'wireguard', False),
                                         ('custom', 'wireguard', False), ('custom', 'openvpn', False)]:
        c = deepcopy(basic)
        c['vpn'].update(provider=provider, type=protocol, port_forwarding=forwarding)
        if provider == 'mullvad':
            c['vpn']['wireguard_addresses'] = '10.64.1.2/32'
        yield f'torrent-{provider}-{protocol}-{forwarding}', c, 3
    for dashboard in [False, True]:
        c = deepcopy(basic)
        c['features']['dashboard'] = dashboard
        yield 'management-' + str(dashboard), c, 4
    c = deepcopy(basic)
    c['features']['photos'] = True
    yield 'photos-cpu', c, 5
    for music_assistant in [False, True]:
        c = deepcopy(basic)
        c['features'].update(music=True, music_assistant=music_assistant)
        yield 'music-' + str(music_assistant), c, 6


def main():
    if not shutil.which('docker'):
        print('Docker CLI/Compose is required for this parser check. No service deployment was attempted.', file=sys.stderr)
        return 1
    passed = []
    with tempfile.TemporaryDirectory(prefix='samurai-compose-check-') as folder:
        for name, config, stage in scenarios():
            project = Path(folder) / name
            project.mkdir()
            config['install_root'] = str(project / 'install')
            config['storage']['data_root'] = str(project / 'data')
            document = catalog.render_stage(config, stage)
            for relative in catalog.stage_directories(config, stage):
                Path(relative).mkdir(parents=True, exist_ok=True)
            for secret in catalog.expected_secret_names(config, stage):
                path = Path(config['install_root']) / 'secrets' / secret
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic-fixture-only\n')
            for service, fields in catalog.environment_secrets(config, stage).items():
                path = Path(config['install_root']) / 'stages' / f"{config['deployment_name']}-{stage}" / (service + '.env')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(''.join(k + '=synthetic-fixture-only\n' for k in fields))
            compose = project / 'compose.yaml'
            compose.write_text(json.dumps(document, indent=2))
            result = subprocess.run(['docker', '--context', 'default', 'compose', '--project-directory', str(project), '--file', str(compose), 'config', '--quiet'], capture_output=True, text=True, timeout=30)
            if result.returncode:
                print('Compose rejected scenario ' + name + '. Inspect its source locally; no containers were started.', file=sys.stderr)
                return 1
            passed.append(name)
    print(json.dumps({'parser': 'docker compose config --quiet', 'passed': passed, 'scenarios': len(passed), 'containers_started': 0}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
