"""Owned installation of the bounded Sonarr monitor-change search timer.

The installed worker is standalone. Updating a source checkout neither removes
its private event state nor restarts any media application.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from . import engine, monitor_search, naming
from .safety import SafetyError, atomic_write, digest, no_symlinks, validate_config, write_json

UNIT_ROOT = Path('/etc/systemd/system')
MAX_BYTES = 2 * 1024 * 1024


def paths(c):
    root = Path(c['install_root'])
    return {'script': root / 'sonarr_monitor_search.py',
            'policy': root / 'state/sonarr-monitor-policy.json',
            'state': root / 'state/sonarr-monitor-state.json',
            'receipt': root / 'state/sonarr-monitor-install.json'}


def units(c, unit_root=None):
    root = Path(unit_root if unit_root is not None else UNIT_ROOT)
    name = 'samurai-' + c['deployment_name'] + '-sonarr-monitor-search'
    marker = '# Managed by Samurai Stack: ' + c['deployment_id'] + '\n'
    saved = paths(c)
    service = (marker + '[Unit]\nDescription=Search newly monitored Sonarr episodes\n'
        'After=network-online.target samurai-' + c['deployment_name'] + '-2.service\n'
        'Wants=network-online.target\n\n[Service]\nType=oneshot\nUMask=0077\n'
        'ExecStart=/usr/bin/python3 ' + str(saved['script']) + ' --policy ' + str(saved['policy']) + '\n'
        'TimeoutStartSec=120\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=full\n')
    timer = (marker + '[Unit]\nDescription=Check Sonarr monitoring changes\n\n'
        '[Timer]\nOnBootSec=90s\nOnUnitInactiveSec=30s\nAccuracySec=5s\n'
        'Unit=' + name + '.service\n\n[Install]\nWantedBy=timers.target\n')
    return {root / (name + '.service'): service, root / (name + '.timer'): timer}


def _sha(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _owner(info):
    if os.name == 'posix' and info.st_uid != 0:
        raise SafetyError('Sonarr monitor control files must belong to root')


def _read(path, *, private=True):
    path = no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise SafetyError('Sonarr monitor control input must be a bounded regular file without hard links')
    _owner(info)
    if os.name == 'posix' and info.st_mode & (0o077 if private else 0o022):
        raise SafetyError('Sonarr monitor control input has unsafe permissions; existing files were retained')
    try:
        body = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise SafetyError('Sonarr monitor control input could not be read safely') from None
    if len(body.encode('utf-8')) > MAX_BYTES:
        raise SafetyError('Sonarr monitor control input exceeded its safe bound')
    return body


def _guard(c):
    engine.linux_only()
    validate_config(c)
    engine.stage_allowed(c, 2)
    naming.check_installed_names(c)
    if os.name == 'posix' and os.geteuid() != 0:
        raise SafetyError('Install the Sonarr monitor timer with sudo/root inside the media VM')
    for folder in (Path(c['install_root']), Path(c['install_root']) / 'state'):
        info = no_symlinks(folder).stat()
        _owner(info)
        if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and info.st_mode & 0o077):
            raise SafetyError('Sonarr monitor control directories must be private to root')
    try:
        marker = json.loads(_read(Path(c['install_root']) / '.samurai-install.json'))
    except (ValueError, OSError):
        raise SafetyError('Cannot prove this installation owns the Sonarr monitor directory') from None
    if not isinstance(marker, dict) or marker.get('deployment_id') != c['deployment_id']:
        raise SafetyError('This installation does not own the Sonarr monitor directory')


@contextmanager
def _lock(c):
    path = no_symlinks(Path(c['install_root']) / 'state/sonarr-monitor-install.lock')
    if path.exists():
        _read(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        _read(path)
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyError('Another Sonarr monitor installation is active; retry after it finishes') from None
        yield
    finally:
        os.close(descriptor)


def _receipt(c):
    target = paths(c)['receipt']
    if not target.exists():
        return None
    try:
        saved = json.loads(_read(target))
    except (ValueError, OSError):
        raise SafetyError('The Sonarr monitor installation receipt is unreadable; it was retained') from None
    if (not isinstance(saved, dict) or saved.get('schema_version') != 1
            or saved.get('kind') != 'sonarr-monitor-install'
            or saved.get('deployment_id') != c['deployment_id']
            or saved.get('deployment_name') != c['deployment_name']
            or saved.get('project') != naming.project_name(c, 2)
            or saved.get('host_id') != engine.host_identity()
            or saved.get('state') not in {'installing', 'installed'}
            or not isinstance(saved.get('artifacts'), dict)
            or not isinstance(saved.get('previous_artifacts', {}), dict)):
        raise SafetyError('The Sonarr monitor receipt belongs to another installation or host; it was retained')
    return saved


def _live_unit(path, runner, *, complete=False):
    args = ['systemctl', 'show', path.name, '--property=LoadState', '--property=FragmentPath',
            '--property=DropInPaths', '--property=Transient', '--property=NeedDaemonReload',
            '--property=Result', '--property=ExecMainStatus', '--property=ExecMainStartTimestampMonotonic']
    raw = runner(args).stdout
    values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if values.get('LoadState') == 'not-found' and not complete:
        return
    if (values.get('LoadState') != 'loaded' or values.get('FragmentPath') != str(path)
            or values.get('DropInPaths') or values.get('Transient') != 'no'
            or (complete and values.get('NeedDaemonReload') != 'no')):
        raise SafetyError('The Sonarr monitor unit has an unexpected live fragment or override; no takeover was performed')
    if complete and path.suffix == '.service':
        if (values.get('Result') != 'success' or values.get('ExecMainStatus') != '0'
                or not values.get('ExecMainStartTimestampMonotonic', '').isdigit()
                or int(values['ExecMainStartTimestampMonotonic']) < 1):
            raise SafetyError('The Sonarr monitor service has not completed a successful guarded poll; inspect its private journal')


def _documents(c, policy, unit_root=None):
    saved = paths(c)
    return {saved['script']: Path(monitor_search.__file__).read_text(encoding='utf-8'),
            saved['policy']: json.dumps(policy, indent=2, sort_keys=True) + '\n',
            **units(c, unit_root)}


def _check_artifacts(c, documents, previous):
    snapshots = {}
    for path, expected in documents.items():
        no_symlinks(path)
        if path.exists():
            body = _read(path, private=path.suffix not in {'.service', '.timer'})
            actual = _sha(body)
            allowed = ({previous['artifacts'].get(str(path)),
                        previous.get('previous_artifacts', {}).get(str(path))} if previous else set())
            if actual not in allowed:
                raise SafetyError('An existing Sonarr monitor artifact is unowned or locally edited; it was retained')
            snapshots[str(path)] = actual
        else:
            snapshots[str(path)] = None
    if previous is None and paths(c)['state'].exists():
        raise SafetyError('Existing Sonarr monitor event state has no owned installation receipt; it was retained')
    return snapshots


def _state(c, policy):
    # This validator is read-only. Running the normal worker here could submit
    # pending EpisodeSearch commands, which verification must never do.
    try:
        return monitor_search.load_state(paths(c)['state'], policy)
    except (monitor_search.MonitorError, OSError, ValueError, TypeError):
        raise SafetyError('Sonarr monitor baseline/event state did not pass validation; it was retained') from None


def _policy(c, runner):
    try:
        return monitor_search.build_policy(c, runner=runner)
    except monitor_search.MonitorError:
        raise SafetyError('The owned Sonarr monitor target could not be verified; no search was submitted') from None


def install(c, *, runner=None, unit_root=None):
    """Install/upgrade only this companion; never restart or reconfigure apps."""
    runner = runner or engine.run
    _guard(c)
    with _lock(c):
        policy = _policy(c, runner)
        previous = _receipt(c)
        documents = _documents(c, policy, unit_root)
        before = _check_artifacts(c, documents, previous)
        # Target/policy changes must never strand existing state behind a new
        # binding. Reject before replacing even one owned runtime artifact.
        if paths(c)['state'].exists():
            _state(c, policy)
        for path in units(c, unit_root):
            _live_unit(path, runner)
        changed = [path for path, body in documents.items() if before[str(path)] != _sha(body)]
        backup = None
        existing_changes = [path for path in changed if before[str(path)] is not None]
        if existing_changes:
            backup = no_symlinks(Path(c['install_root']) / 'backups' / ('sonarr-monitor-before-' + str(time.time_ns())))
            backup.mkdir(mode=0o700)
            for path in existing_changes:
                atomic_write(backup / path.name, _read(path, private=path.suffix not in {'.service', '.timer'}))
        receipt = {'schema_version': 1, 'kind': 'sonarr-monitor-install',
                   'deployment_id': c['deployment_id'], 'deployment_name': c['deployment_name'],
                   'project': naming.project_name(c, 2), 'host_id': engine.host_identity(),
                   'state': 'installing', 'policy_digest': digest(policy),
                   'artifacts': {str(path): _sha(body) for path, body in documents.items()},
                   'previous_artifacts': before}
        target = paths(c)['receipt']
        write_json(target, receipt, replace=target.exists())
        for path in changed:
            atomic_write(path, documents[path], replace=path.exists(),
                         mode=0o644 if path.suffix in {'.service', '.timer'} else 0o600)
        first = not paths(c)['state'].exists()
        if first:
            runner(['/usr/bin/python3', str(paths(c)['script']), '--policy', str(paths(c)['policy']), '--baseline'], timeout=130)
        _state(c, policy)
        receipt.update(state='installed', previous_artifacts={})
        write_json(target, receipt, replace=True)
        runner(['systemctl', 'daemon-reload'])
        service = next(path.name for path in units(c, unit_root) if path.suffix == '.service')
        # Baseline runs outside systemd. This additional owned oneshot proves
        # the real sandbox, mount checks and native readback before success.
        runner(['systemctl', 'start', service], timeout=130)
        timer = next(path.name for path in units(c, unit_root) if path.suffix == '.timer')
        runner(['systemctl', 'enable', '--now', timer])
        report = verify(c, runner=runner, unit_root=unit_root)
        report.update(first_activation_baseline=first, changed_artifacts=len(changed))
        if backup is not None:
            report['private_backup'] = str(backup)
        return report


def verify(c, *, runner=None, unit_root=None):
    """Read back target, private state and enabled timer without searching."""
    runner = runner or engine.run
    _guard(c)
    previous = _receipt(c)
    if previous is None or previous['state'] != 'installed':
        raise SafetyError('Sonarr monitor search is not installed; rerun guided step 3 to install its companion')
    policy = _policy(c, runner)
    documents = _documents(c, policy, unit_root)
    _check_artifacts(c, documents, previous)
    if previous.get('policy_digest') != digest(policy):
        raise SafetyError('The owned Sonarr monitor target changed; reinstall its companion to review the new target')
    for path, expected in documents.items():
        if not path.exists() or _read(path, private=path.suffix not in {'.service', '.timer'}) != expected:
            raise SafetyError('Sonarr monitor generated runtime differs; rerun step 3 without removing its event state')
    _state(c, policy)
    for path in units(c, unit_root):
        _live_unit(path, runner, complete=True)
        if path.suffix == '.timer':
            runner(['systemctl', 'is-enabled', '--quiet', path.name])
            runner(['systemctl', 'is-active', '--quiet', path.name])
    return {'ok': True, 'status': 'verified', 'timer': timer_name(c),
            'scope': 'future_monitor_changes_only', 'existing_backlog_search': False}


def timer_name(c):
    return 'samurai-' + c['deployment_name'] + '-sonarr-monitor-search.timer'
