#!/usr/bin/env python3
"""Installed bounded startup completion check; Docker reads only, no credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

MAX_BYTES = 2 * 1024 * 1024
INSPECT = ('{"id":{{json .Id}},"labels":{{json .Config.Labels}},'
           '"running":{{json .State.Running}},"status":{{json .State.Status}},'
           '"health":{{if index .State "Health"}}{{json (index .State "Health").Status}}{{else}}null{{end}},'
           '"network":{{json .HostConfig.NetworkMode}},"configured_ports":{{json .HostConfig.PortBindings}},'
           '"published_ports":{{json .NetworkSettings.Ports}},"networks":{{json .NetworkSettings.Networks}}}')


def _owner(info):
    if os.name == 'posix' and info.st_uid != 0:
        raise ValueError('startup control ownership differs')


def _read(path):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('startup control path is a symlink')
    info = path.stat()
    _owner(info)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
            or os.name == 'posix' and info.st_mode & 0o022):
        raise ValueError('startup control input is unsafe')
    value = path.read_bytes()
    if len(value) > MAX_BYTES:
        raise ValueError('startup control input exceeded its bound')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load(config_path, stage):
    c = json.loads(_read(config_path))
    root = Path(c['install_root'])
    project = c.get('compose_projects', {}).get(str(stage), c['deployment_name'] + '-' + str(stage))
    if not isinstance(project, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', project):
        raise ValueError('startup project is invalid')
    folder = root / 'stages' / project
    if Path(config_path).absolute() != (folder / 'site.json').absolute():
        raise ValueError('startup configuration is outside its owned stage')
    if json.loads(_read(root / '.samurai-install.json')).get('deployment_id') != c['deployment_id']:
        raise ValueError('startup installation differs')
    proof = json.loads(_read(folder / 'startup.json'))
    if (not isinstance(proof, dict) or set(proof) != {'schema_version', 'deployment_id', 'stage', 'project',
            'config_digest', 'compose_sha256', 'services'} or proof['schema_version'] != 1
            or proof['deployment_id'] != c['deployment_id'] or proof['stage'] != stage
            or proof['project'] != project or proof['config_digest'] != _digest(c)
            or proof['compose_sha256'] != hashlib.sha256(_read(folder / 'compose.yaml')).hexdigest()
            or not isinstance(proof['services'], list) or not 1 <= len(proof['services']) <= 32
            or any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name)
                   for name in proof['services']) or proof['services'] != sorted(set(proof['services']))):
        raise ValueError('startup proof differs from the owned configuration')
    return c, proof


def _call(runner, arguments, remaining):
    result = runner(['docker', '--context', 'default', *arguments], capture_output=True, text=True,
                    check=True, timeout=min(10, max(0.1, remaining)))
    if not isinstance(result.stdout, str) or len(result.stdout) > MAX_BYTES:
        raise ValueError('startup Docker output exceeded its bound')
    return result.stdout


def _bindings(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError('startup port inventory is invalid')
    result = {}
    for port, bindings in value.items():
        if not isinstance(port, str) or not re.fullmatch(r'[0-9]{1,5}/(?:tcp|udp)', port):
            raise ValueError('startup port inventory is invalid')
        if bindings is None:
            continue  # Exposed container port without a host publication.
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 16:
            raise ValueError('startup port bindings are invalid')
        chosen = []
        for row in bindings:
            if (not isinstance(row, dict) or set(row) != {'HostIp', 'HostPort'}
                    or not isinstance(row['HostIp'], str) or not isinstance(row['HostPort'], str)
                    or not row['HostPort'].isdigit() or not 1 <= int(row['HostPort']) <= 65535):
                raise ValueError('startup port binding is invalid')
            chosen.append((row['HostIp'], row['HostPort']))
        if len(chosen) != len(set(chosen)):
            raise ValueError('startup port binding is ambiguous')
        result[port] = sorted(chosen)
    return result


def _network_ready(row):
    if _bindings(row.get('configured_ports')) != _bindings(row.get('published_ports')):
        return False
    mode, networks = row.get('network'), row.get('networks')
    if not isinstance(mode, str) or not isinstance(networks, dict):
        raise ValueError('startup network inventory is invalid')
    if mode in {'host', 'none'} or mode.startswith('container:'):
        return True
    return bool(networks) and all(isinstance(value, dict)
        and all(isinstance(value.get(key), str) and re.fullmatch(r'[a-f0-9]{64}', value[key])
                for key in ('NetworkID', 'EndpointID')) for value in networks.values())


def inspect_ready(config, proof, runner, remaining):
    raw = _call(runner, ['ps', '--all', '--filter', 'label=com.docker.compose.project=' + proof['project'],
                         '--format', '{{.ID}}'], remaining)
    ids = raw.split()
    if not ids:
        return False
    if len(ids) > 64 or len(ids) != len(set(ids)) or any(not re.fullmatch(r'[a-f0-9]{12,64}', value) for value in ids):
        raise ValueError('startup container inventory is ambiguous')
    raw = _call(runner, ['inspect', *ids, '--format', INSPECT], remaining)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(rows) != len(ids):
        raise ValueError('startup container inventory is incomplete')
    services, full_ids = {}, set()
    for row in rows:
        labels = row.get('labels', {})
        identifier, service = row.get('id'), labels.get('com.docker.compose.service')
        if (not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier)
                or identifier in full_ids or sum(identifier.startswith(value) for value in ids) != 1
                or labels.get('systems.samarie.deployment') != config['deployment_id']
                or labels.get('systems.samarie.stage') != str(proof['stage'])
                or labels.get('com.docker.compose.project') != proof['project']
                or service not in proof['services'] or service in services
                or type(row.get('running')) is not bool
                or row.get('status') not in {'created', 'restarting', 'running', 'removing', 'paused', 'exited', 'dead'}
                or row.get('health') not in {None, 'starting', 'healthy', 'unhealthy'}):
            raise ValueError('startup target ownership or state differs')
        full_ids.add(identifier)
        services[service] = row
    if set(services) != set(proof['services']):
        return False
    if any(row['running'] is not True or row['status'] != 'running' or row['health'] not in {None, 'healthy'}
           for row in services.values()):
        return False
    if proof['stage'] == 3:
        if ('gluetun' not in services or 'qbittorrent' not in services
                or services['qbittorrent'].get('network') != 'container:' + services['gluetun']['id']):
            raise ValueError('startup torrent namespace differs')
    if any(not _network_ready(row) for row in services.values()):
        return False
    return True


def wait_ready(config, proof, *, runner=None, clock=None, sleep=None, timeout=120):
    runner, clock, sleep = runner or subprocess.run, clock or time.monotonic, sleep or time.sleep
    deadline = clock() + timeout
    for _ in range(31):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            if inspect_ready(config, proof, runner, remaining):
                return True
        except (OSError, subprocess.SubprocessError):
            pass  # Bounded Docker readiness reads may be retried, never writes.
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(4, remaining))
    return False


def main():
    try:
        if len(sys.argv) != 3 or sys.argv[2] not in {'2', '3', '4', '5', '6'}:
            raise ValueError('startup stage is invalid')
        config, proof = load(sys.argv[1], int(sys.argv[2]))
        if wait_ready(config, proof):
            return 0
    except Exception:
        pass
    print('Startup guard refused readiness: expected owned containers did not become running/healthy. '
          'The failed service will retry through its storage and network guards.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
