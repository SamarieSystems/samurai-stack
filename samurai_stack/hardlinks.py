"""Narrow existing-install repair: two Starr settings, no deployment or media work."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys

from . import catalog, engine, integrations, naming
from .safety import SafetyError, load_config, no_symlinks, private_read, validate_config


def check_targets(config, runner):
    """Prove the API ports belong to this installation's local running apps."""
    engine.linux_only()
    validate_config(config)
    engine.stage_allowed(config, 2)
    root = no_symlinks(Path(config['install_root']))
    marker = json.loads(private_read(root / '.samurai-install.json'))
    if not isinstance(marker, dict) or marker.get('deployment_id') != config['deployment_id']:
        raise SafetyError('Application directory belongs to another installation')
    address = config['bind_address']
    interfaces = json.loads(runner(['ip', '-j', 'address', 'show']).stdout)
    if (not isinstance(interfaces, list) or any(not isinstance(iface, dict)
            or not isinstance(iface.get('addr_info', []), list)
            or any(not isinstance(entry, dict) for entry in iface.get('addr_info', [])) for iface in interfaces)):
        raise SafetyError('Cannot verify the local interface inventory')
    if address not in {entry.get('local') for iface in interfaces for entry in iface.get('addr_info', [])}:
        raise SafetyError('The saved media address is not on this VM; keep the original configuration and review its address')
    engine.check_docker(runner)
    for service in ('sonarr', 'radarr'):
        ids = engine.docker(runner, 'ps', '--filter', 'status=running',
                            '--filter', 'label=com.docker.compose.project=' + naming.project_name(config, 2),
                            '--filter', 'label=com.docker.compose.service=' + service,
                            '--format', '{{.ID}}').stdout.split()
        if len(ids) != 1:
            raise SafetyError('Start this installation\'s Sonarr and Radarr before checking hardlinks')
        labels = json.loads(engine.docker(runner, 'inspect', ids[0], '--format', '{{json .Config.Labels}}').stdout)
        if (not isinstance(labels, dict) or labels.get('systems.samarie.deployment') != config['deployment_id']
                or labels.get('systems.samarie.stage') != '2'
                or labels.get('com.docker.compose.service') != service):
            raise SafetyError('Refusing an application container owned by another installation')
        ports = json.loads(engine.docker(runner, 'inspect', ids[0], '--format', '{{json .NetworkSettings.Ports}}').stdout)
        port = str(catalog.PORTS[service])
        if (not isinstance(ports, dict) or not isinstance(ports.get(port + '/tcp'), list)
                or any(not isinstance(binding, dict) for binding in ports[port + '/tcp'])):
            raise SafetyError('Cannot verify the application port bindings')
        if not any(binding.get('HostIp') == address and binding.get('HostPort') == port
                   for binding in (ports.get(port + '/tcp') or [])):
            raise SafetyError('The saved API address does not match this installation\'s published application port')


def run(config, *, apply=False, runner=None, request=None):
    runner = runner or engine.run
    check_targets(config, runner)
    with engine.deployment_lock(config) if apply else nullcontext():
        if apply:
            check_targets(config, runner)
        operation = integrations.configure_hardlinks if apply else integrations.verify_hardlinks
        return operation(config, request=request or engine.request,
                         secret_resolver=engine.secret_resolver(config))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Enable or verify hardlinks in existing Samurai Sonarr/Radarr; no scans or media changes.')
    parser.add_argument('--config', '-c', default='site.json', help='Existing private site.json; do not create a new installation')
    parser.add_argument('--apply', action='store_true', help='Enable copyUsingHardlinks and read both settings back; default is read-only')
    args = parser.parse_args(argv)
    try:
        result = run(load_config(args.config), apply=args.apply)
        for item in result.get('checks', []):
            print(item['name'] + ': ' + item['detail'])
        for item in result.get('pending', []):
            print(item['name'] + ': ' + item['action'])
        for item in result.get('errors', []):
            print(item['name'] + ': ' + item['error'], file=sys.stderr)
        return 0 if result.get('ok') is True else 2
    except SafetyError as exc:
        print('Hardlink check stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Keep vendor output, full URLs, keys and private filesystem contents out
        # of the terminal even when an API/configuration parser fails.
        print('Hardlink check stopped. Verify this is the intended Linux VM, the existing site.json and owned running Sonarr/Radarr. No deployment was run.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
