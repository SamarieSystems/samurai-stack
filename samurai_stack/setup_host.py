"""Friendly host preparation over the reviewed, non-destructive bootstrap.

The guided interface keeps the complete plan/hash gate in bootstrap-host.sh.
Only its presentation changes: one confirmation, private evidence, progress.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time
import uuid

from .safety import SafetyError, atomic_write, digest, no_symlinks, write_json


class SetupHostError(SafetyError):
    """Preparation failed; private evidence is retained for recovery."""


def _new_attempt(root: Path) -> Path:
    state = no_symlinks(root / 'var/lib/samurai-stack/setup-host')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        info = state.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SetupHostError('Host setup evidence directory must be private and owned by this administrator')
    attempt = state / ('attempt-' + uuid.uuid4().hex)
    attempt.mkdir(mode=0o700)
    return attempt


def _run_command(argv, attempt: Path, phase: str, emit=print, *, heartbeat=15):
    """Keep vendor output private while showing that a long apply is alive."""
    paths = [attempt / (phase + suffix) for suffix in ('.stdout', '.stderr')]
    handles = []
    started = time.monotonic()
    try:
        for path in paths:
            fd = os.open(no_symlinks(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handles.append(os.fdopen(fd, 'wb'))
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handles[0], stderr=handles[1], start_new_session=True) as process:
            try:
                while True:
                    try:
                        code = process.wait(timeout=heartbeat)
                        break
                    except subprocess.TimeoutExpired:
                        emit('Preparing guest prerequisites, still running (' + str(int(time.monotonic() - started)) + ' seconds)...')
            except KeyboardInterrupt:
                # Forward the normal terminal interruption to the complete child
                # group, including an active package manager. Do not force-kill it.
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGINT)
                else:
                    process.terminate()
                process.wait()
                raise
        for handle in handles:
            handle.flush()
        if paths[0].stat().st_size > 4 * 1024 * 1024:
            raise SetupHostError('Bootstrap returned unexpectedly large output; inspect its private evidence')
        return subprocess.CompletedProcess(argv, code, paths[0].read_text(encoding='utf-8'), paths[1].read_text(encoding='utf-8'))
    finally:
        for handle in handles:
            handle.close()


def _refusal_hint(stderr):
    """Translate known bootstrap refusals without displaying vendor payloads.

    Even a prefixed stderr line is untrusted. Match only known fixed reasons
    and return installer-owned prose; never interpolate its suffix or raw text.
    """
    prefix = 'bootstrap refused: '
    reasons = [line[len(prefix):] for line in (stderr or '').splitlines() if line.startswith(prefix)]
    for reason in reversed(reasons):
        if reason.startswith('Active APT installation-media source detected;'):
            return ('APT is still configured to read the installation DVD/CD-ROM. In /etc/apt/sources.list '
                    'or /etc/apt/sources.list.d, disable only the active cdrom source: comment its deb line '
                    'or set Enabled: no in its .sources stanza. Keep the online OS mirrors, run apt-get update, '
                    'then rerun Prepare host. Setup has not changed the sources')
        if reason.startswith(('Guest OS, machine, script, packages or apt sources changed;',
                              'Guest state changed while waiting for the apply lock;')):
            return ('The guest changed after plan review. Rerun Prepare host to generate a fresh plan. '
                    'The stale plan was not applied')
        if reason.startswith('Plan integrity or --accept-plan does not match'):
            return ('The saved review failed its integrity check. Rerun Prepare host to generate a new plan; '
                    'the previous private evidence was kept')
        if reason.startswith('apt-get failed with exit 100;'):
            return ('APT returned exit 100 during a repository or package operation. That code alone does not '
                    'identify the cause. Review the private package-manager diagnostic before changing '
                    'repository settings or dependencies')
        if reason.startswith('APT would remove or upgrade an existing package;'):
            return ('APT proposed removing or upgrading an existing package, so that installation was refused. '
                    'Review the private dependency simulation and resolve the package or repository conflict '
                    'before rerunning Prepare host')
        if reason.startswith('Docker key differs from the pinned official artifact;'):
            return ('The downloaded Docker signing key did not match the pinned official artifact and was not '
                    'trusted. Verify the current official key against the installer pin; if the key rotated, '
                    'use a reviewed installer update')
        if reason.startswith('Another bootstrap apply is already running'):
            return ('Another host preparation process is running. Let it finish before rerunning Prepare host')
        if reason.startswith('Another Docker repository is already configured;'):
            return ('An existing Docker repository needs review. Setup did not add a duplicate source; '
                    'inspect that repository configuration before rerunning Prepare host')
    return None


def _object(result, label):
    if result.returncode:
        hint = _refusal_hint(result.stderr)
        if hint:
            # Expose only the exact installer-owned diagnostic namespace. Raw
            # subprocess output can contain repository credentials or terminal
            # control sequences and must stay private.
            diagnostic = re.search(
                r' Private command diagnostic: '
                r'(/var/lib/samurai-stack/host-bootstrap/backup-[0-9a-f]{32}'
                r'/commands/[0-9]{4,}-(?:apt-get|curl|systemctl|docker|command)'
                r'-(?:update|install|simulate-install|download-signing-key|enable|start|is-active|engine-info|compose-version|other)\.log)$',
                result.stderr or '', re.MULTILINE)
            if diagnostic:
                hint += '. Read the command output with: cat ' + diagnostic.group(1)
            raise SetupHostError(label + ' stopped: ' + hint)
        raise SetupHostError(label + ' was refused; inspect the private bootstrap logs before retrying')
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise SetupHostError(label + ' did not return a valid report') from exc
    if not isinstance(value, dict):
        raise SetupHostError(label + ' did not return a valid report')
    return value


def _prepare_host(repo: Path, root: Path, execute, confirm, emit) -> bool:
    script = no_symlinks(repo / 'scripts/bootstrap-host.sh')
    if not script.is_file():
        raise SetupHostError('The reviewed host bootstrap is missing from this checkout')
    attempt = _new_attempt(root)
    result_path = attempt / 'result.json'
    summary = {'started': datetime.now(timezone.utc).isoformat(), 'status': 'inspecting'}
    command = ['bash', str(script)]

    def finish(status, **values):
        summary.update(status=status, **values)
        write_json(result_path, summary, replace=result_path.exists())

    try:
        document = _object(execute([*command, '--plan'], attempt, 'plan', emit), 'Host inspection')
        signed = dict(document)
        accepted = signed.pop('review_hash', None)
        if document.get('kind') != 'guest-bootstrap' or not accepted or digest(signed) != accepted:
            raise SetupHostError('Host inspection returned an invalid reviewed plan')
        plan_path = attempt / 'plan.json'
        write_json(plan_path, document)
        summary['review_hash'] = accepted
        emit('Guest: ' + str(document.get('os')) + ' ' + str(document.get('version')) + ', ' + str(document.get('architecture')) + '.')

        channel = root / 'dev/virtio-ports/org.qemu.guest_agent.0'
        if not channel.exists():
            emit('Preparation is waiting for the QEMU Guest Agent channel. No packages have been changed.')
            emit('In Proxmox, select this VM, open Options > QEMU Guest Agent, and enable Use QEMU Guest Agent. Shut the VM down cleanly, wait until it is stopped, then Start it again. A guest reboot alone is not enough.')
            emit('After it starts, run setup again and choose Prepare host. No manual shell commands are needed for this prerequisite.')
            finish('pending', prerequisite='qemu_guest_agent_channel')
            return False

        installed = document.get('installed_packages', {})
        packages = document.get('packages', [])
        if not isinstance(installed, dict) or not isinstance(packages, list) or not packages or any(not isinstance(p, str) for p in packages):
            raise SetupHostError('Host inspection returned an invalid package inventory')
        missing = [name for name in packages if name not in installed]
        if not missing:
            checked = execute([*command, '--verify'], attempt, 'verify-before', emit)
            if checked.returncode == 0 and _object(checked, 'Runtime verification').get('state') == 'ready':
                emit('Host is already ready. Docker, Compose and the guest agent passed live verification; no package installation was run.')
                finish('ready', method='existing_runtime_verified')
                return True

        emit('Missing packages: ' + ', '.join(missing) if missing else 'Packages are installed; Docker or the guest agent needs preparation or a version check.')
        emit('This installs missing prerequisites from the OS and official Docker repositories. Existing packages are not upgraded or removed. NAS shares, disks and media are not changed.')
        if not confirm('Prepare this new VM now? [y/N] '):
            emit('Host preparation skipped. The reviewed plan was retained privately.')
            finish('declined')
            return False
        emit('Preparing the guest. Package output is saved privately; progress will appear while it runs.')
        applied = _object(execute([*command, '--apply', '--plan-file', str(plan_path), '--accept-plan', accepted, '--ack-new-guest'], attempt, 'apply', emit), 'Host preparation')
        if applied.get('state') != 'ready':
            raise SetupHostError('Host preparation did not confirm runtime readiness')
        verified = _object(execute([*command, '--verify'], attempt, 'verify-after', emit), 'Final runtime verification')
        if verified.get('state') != 'ready':
            raise SetupHostError('Final runtime verification did not confirm readiness')
        emit('Host ready. Docker ' + str(verified.get('docker_server', 'verified')) + ', Compose ' + str(verified.get('compose', 'verified')) + ', guest agent active.')
        finish('ready', method='applied_and_verified', runtime=verified)
        return True
    except KeyboardInterrupt:
        finish('interrupted')
        emit('Preparation interrupted. Existing data and private attempt evidence were retained: ' + str(attempt))
        raise
    except Exception as exc:
        finish('failed', error_type=type(exc).__name__)
        message = str(exc) if isinstance(exc, SetupHostError) else 'Host preparation could not finish safely'
        raise SetupHostError(message + '. Private evidence: ' + str(attempt) + '. Fix the prerequisite and rerun Prepare host for a fresh reviewed plan; no data is deleted.') from exc


def _confirm(prompt):
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return False
        if answer in {'y', 'yes'}:
            return True
        if answer in {'', 'n', 'no'}:
            return False
        print('Enter yes or no.')


def prepare_host(repo: Path) -> bool:
    """Prepare this new Linux guest, or return False for a skipped prerequisite."""
    if platform.system() != 'Linux' or getattr(os, 'geteuid', lambda: -1)() != 0:
        raise SetupHostError('Run guided host preparation as root inside the new Linux VM')
    if not sys.stdin.isatty():
        raise SetupHostError('Guided preparation needs an interactive terminal; use the reviewed bootstrap CLI for unattended operation')
    return _prepare_host(Path(repo), Path('/'), _run_command, _confirm, print)
