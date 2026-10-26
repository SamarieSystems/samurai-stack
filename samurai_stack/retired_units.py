"""Retire an obsolete generated unit only after its containers are gone.

This does not remove application data, media, images, volumes or containers.
Only an exact conflicting unit for the requested project may be stopped,
disabled and moved to a private archive. Any stopped container also blocks it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from . import naming
from .safety import SafetyError, absolute_path, no_symlinks, run, validate_config

UNIT_ROOT = Path('/etc/systemd/system')
ARCHIVE_ROOT = Path('/var/lib/samurai-stack/retired-units')
MAX_UNIT_BYTES = 64 * 1024


def _guard():
    if not sys.platform.startswith('linux') or os.geteuid() != 0 or Path('/etc/pve').exists():
        raise SafetyError('Obsolete service retirement requires root inside the target Linux guest.')


def _owner(info):
    if os.name == 'posix' and info.st_uid != 0:
        raise SafetyError('Obsolete service retirement requires root-owned files and directories.')


def _unit(path):
    path = no_symlinks(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_UNIT_BYTES:
            raise SafetyError('An obsolete service unit must be a bounded regular file without hard links.')
        _owner(info)
        if os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o644:
            raise SafetyError('An obsolete service unit must retain its generated mode 644; it was not changed.')
        encoded = path.read_bytes()
        if len(encoded) > MAX_UNIT_BYTES:
            raise SafetyError('An obsolete service unit exceeds the safe size limit.')
        text = encoded.decode('utf-8').replace('\r\n', '\n')
    except (OSError, UnicodeError) as exc:
        raise SafetyError('The obsolete service unit could not be read safely; it was not changed.') from exc
    return encoded, text, info


def _uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError('The obsolete service unit has no valid ownership marker.') from exc
    if value != parsed:
        raise SafetyError('The obsolete service ownership marker is not canonical.')
    return parsed


def _generated(c, stage, text):
    markers = re.findall(r'^# Managed by Samurai Stack: ([^\r\n]+)$', text, re.M)
    if len(markers) != 1:
        raise SafetyError('The obsolete service unit is unmanaged or has ambiguous ownership; it was retained.')
    old = _uuid(markers[0])
    if old == c['deployment_id']:
        return None
    root = c['install_root']
    project = naming.project_name(c, stage)
    folder = root + '/stages/' + project
    command = '/usr/bin/docker --context default compose --project-name ' + project + ' --file ' + folder + '/compose.yaml'
    expected = {
        'ExecStartPre': '/usr/bin/python3 ' + root + '/runtime_guard.py ' + folder + '/site.json ' + str(stage),
        'ExecStart': command + ' up --no-color --abort-on-container-failure --pull never',
        'ExecStop': command + ' stop --timeout 45',
    }
    allowed = {
        'Unit': {'Description': 'Samurai Stack stage ' + str(stage), 'Requires': 'docker.service',
                 'PartOf': 'docker.service',
                 'After': 'docker.service network-online.target', 'Wants': 'network-online.target',
                 'StartLimitIntervalSec': '300', 'StartLimitBurst': '3'},
        'Service': {'Type': 'simple', 'UMask': '0077', **expected,
                    'ExecStartPost': '/usr/bin/python3 ' + root + '/runtime_startup.py ' + folder + '/site.json ' + str(stage),
                    'TimeoutStartSec': '180',
                    'Restart': 'on-failure', 'RestartSec': '15', 'TimeoutStopSec': '75'},
        'Install': {'WantedBy': 'multi-user.target'},
    }
    found, keys, sections, section = {}, set(), set(), None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            if section not in allowed or section in sections:
                raise SafetyError('The obsolete service unit has custom section directives; it was retained.')
            sections.add(section)
            continue
        if '=' not in line or section not in allowed:
            raise SafetyError('The obsolete service unit has unrecognized directives; it was retained.')
        key, value = line.split('=', 1)
        if section == 'Service' and key == 'ExecStart' and value == expected['ExecStart'].replace(
                ' up --no-color ', ' up --no-color --force-recreate '):
            value = expected['ExecStart']  # Current guarded restart policy, same owned recipe.
        if (section, key) in keys:
            raise SafetyError('The obsolete service unit has duplicate directives; it was retained.')
        keys.add((section, key))
        if section == 'Unit' and key == 'RequiresMountsFor':
            mounts = value.split(' ')
            try:
                if mounts[-1] != root or len(mounts) != (1 if stage == 4 else 2):
                    raise ValueError()
                for mount in mounts:
                    absolute_path(mount, 'Old managed mount')
            except (ValueError, SafetyError):
                raise SafetyError('The obsolete service has custom mount requirements; it was retained.') from None
        elif section == 'Unit' and key == 'StartLimitIntervalSec' and value == '0':
            pass  # Reboot-safe generated units retry guarded failures indefinitely.
        elif key not in allowed[section] or allowed[section][key] != value:
            raise SafetyError('The obsolete service unit has custom directives; it was retained.')
        if section == 'Service' and key in expected:
            found[key] = value
    if found != expected:
        raise SafetyError('The obsolete service unit does not match this generated project and application path; it was retained.')
    return old


def _call(runner, args):
    try:
        result = runner(args, timeout=45)
    except Exception as exc:
        raise SafetyError('Obsolete service retirement could not verify a prerequisite; existing files were retained.') from exc
    if getattr(result, 'returncode', 0) != 0 or not isinstance(getattr(result, 'stdout', None), str):
        raise SafetyError('Obsolete service retirement could not verify a prerequisite; existing files were retained.')
    if len(result.stdout) > 64 * 1024:
        raise SafetyError('Obsolete service retirement returned an unexpected inventory; existing files were retained.')
    return result.stdout.strip()


def _empty(runner, project, old):
    for selector in ('com.docker.compose.project=' + project, 'systems.samarie.deployment=' + old):
        found = _call(runner, ['docker', '--context', 'default', 'ps', '--all', '--filter',
                               'label=' + selector, '--format', '{{.ID}}'])
        if found:
            raise SafetyError('Obsolete service retirement is blocked by existing stopped or running containers. No unit was archived and no container was removed.')


def _manager_matches(runner, unit):
    fragment = _call(runner, ['systemctl', 'show', unit.name, '--property=FragmentPath', '--value'])
    overrides = _call(runner, ['systemctl', 'show', unit.name, '--property=DropInPaths', '--value'])
    stale = _call(runner, ['systemctl', 'show', unit.name, '--property=NeedDaemonReload', '--value'])
    if fragment != str(unit) or overrides or stale != 'no':
        raise SafetyError('The obsolete service has overrides or differs from the loaded systemd configuration; it was retained.')


def _archive_directory(path):
    path = no_symlinks(Path(path).absolute())
    missing, parent = [], path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for ancestor in [parent, *parent.parents]:
        info = ancestor.stat()
        _owner(info)
        if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise SafetyError('The obsolete service archive parent has unsafe ownership or permissions.')
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    info = path.stat()
    _owner(info)
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and info.st_mode & 0o077):
        raise SafetyError('The obsolete service archive directory must be private; its permissions were not changed.')
    return path


def retire_conflicting(c, stage, *, runner=run, unit_root=UNIT_ROOT, archive_root=ARCHIVE_ROOT):
    """Archive one generated foreign-UUID unit whose project is entirely empty."""
    _guard()
    validate_config(c)
    _uuid(c['deployment_id'])
    if type(stage) is not int or stage not in {2, 3, 4, 5, 6}:
        raise SafetyError('Only an application stage can retire an obsolete generated service.')
    unit_root = no_symlinks(Path(unit_root))
    if not unit_root.exists():
        return None
    info = unit_root.stat()
    _owner(info)
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and info.st_mode & 0o022):
        raise SafetyError('The system service directory has unsafe ownership or permissions.')
    project = naming.project_name(c, stage)
    unit = no_symlinks(unit_root / ('samurai-' + c['deployment_name'] + '-' + str(stage) + '.service'))
    if not unit.exists():
        return None
    encoded, text, original = _unit(unit)
    old = _generated(c, stage, text)
    if old is None:
        return None
    endpoint = _call(runner, ['docker', '--context', 'default', 'context', 'inspect', 'default',
                              '--format', '{{.Endpoints.docker.Host}}'])
    if not endpoint.startswith('unix://'):
        raise SafetyError('Obsolete service retirement requires the local Unix-socket Docker engine.')
    if _call(runner, ['docker', '--context', 'default', 'info', '--format', '{{.OSType}}']) != 'linux':
        raise SafetyError('Obsolete service retirement requires a reachable local Linux Docker engine.')
    _empty(runner, project, old)
    _manager_matches(runner, unit)
    # Verify the archive destination before stopping even this one service.
    archive_root = _archive_directory(archive_root)
    _call(runner, ['systemctl', 'stop', unit.name])
    _call(runner, ['systemctl', 'disable', unit.name])
    if _call(runner, ['systemctl', 'show', unit.name, '--property=ActiveState', '--value']) not in {'inactive', 'failed'}:
        raise SafetyError('The obsolete service did not become inactive; its unit was not archived.')
    _empty(runner, project, old)
    current, current_text, current_info = _unit(unit)
    if (current != encoded or current_info.st_ino != original.st_ino or current_info.st_dev != original.st_dev
            or _generated(c, stage, current_text) != old):
        raise SafetyError('The obsolete service changed during retirement; its current unit was retained.')
    name = old + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex
    archive = archive_root / name
    archive.mkdir(mode=0o700)
    destination = archive / unit.name
    try:
        os.rename(unit, destination)
    except OSError as exc:
        raise SafetyError('The obsolete service was stopped and disabled, but archival failed. Its original unit was retained; no application files were removed.') from exc
    _call(runner, ['systemctl', 'daemon-reload'])
    try:
        _call(runner, ['systemctl', 'reset-failed', unit.name])
    except SafetyError:
        # An inactive unit can be garbage-collected after its file is archived.
        # Reset was attempted only for this exact name; do not reset all units.
        if _call(runner, ['systemctl', 'show', unit.name, '--property=LoadState', '--value']) != 'not-found':
            raise SafetyError('The obsolete unit was archived, but its failed state could not be cleared. The private archive was retained.') from None
    return {'status': 'obsolete_unit_archived', 'unit_name': unit.name, 'old_deployment_id': old,
            'archive_path': str(destination), 'containers_removed': 0, 'application_files_removed': 0}
