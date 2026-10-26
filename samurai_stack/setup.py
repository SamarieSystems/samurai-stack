"""Guided operator interface over the existing reviewed deployment engine."""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
import warnings

from . import __version__, catalog, engine
from .safety import SafetyError, atomic_write, load_config, no_symlinks, private_read, write_json

UNIT_ROOT = Path('/etc/systemd/system')
STATE_ROOT = Path('/var/lib/samurai-stack/setup/progress')
REGISTRY_ROOT = Path('/var/lib/samurai-stack/installations')

STEPS = {1: 'Prepare Debian', 2: 'Connect storage', 3: 'Media applications',
         4: 'VPN and torrents', 5: 'Management', 6: 'Optional extras'}
APP_NAMES = {'jellyfin': 'Jellyfin', 'seerr': 'Seerr', 'sonarr': 'Sonarr',
             'radarr': 'Radarr', 'prowlarr': 'Prowlarr', 'bazarr': 'Bazarr',
             'sabnzbd': 'SABnzbd', 'qbittorrent': 'qBittorrent',
             'dockge': 'Dockge', 'uptime-kuma': 'Uptime Kuma', 'homarr': 'Homarr',
             'immich-server': 'Immich', 'navidrome': 'Navidrome', 'slskd': 'slskd',
             'music-assistant': 'Music Assistant'}


class GuidedSetup:
    def __init__(self, repo, config_path, *, input_fn=input, output_fn=print, secret_fn=None,
                 state_base=None, registry_root=None):
        self.repo = Path(repo)
        self.config_path = no_symlinks(Path(config_path).absolute())
        self.read, self.say = input_fn, output_fn
        self.secret_fn = secret_fn
        self.state_base = no_symlinks(Path(state_base) if state_base is not None else STATE_ROOT)
        self.registry_root = no_symlinks(Path(registry_root) if registry_root is not None else REGISTRY_ROOT)
        self._prior_state_roots = []
        self._offer_media_profiles = False
        identity = hashlib.sha256(str(self.config_path).encode()).hexdigest()[:16]
        legacy = no_symlinks(self.config_path.parent / '.setup-state' / identity)
        if legacy.exists():
            self._prior_state_roots.append(legacy)
        self.state_root = no_symlinks(self.state_base / ('draft-' + identity))
        self._refresh_state_root()

    def _refresh_state_root(self):
        if self.config_path.exists():
            # Progress is advisory. Full runtime configuration validation stays
            # in config(); here only a bounded private installation ID is read.
            try:
                c = json.loads(private_read(self.config_path))
                identifier = str(uuid.UUID(c['deployment_id']))
                if c['deployment_id'] != identifier:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise SafetyError('The saved site has no valid installation identity. Keep it and run ./setup --recover.') from None
            current = no_symlinks(self.state_base / identifier)
            if current != self.state_root:
                self._prior_state_roots.append(self.state_root)
                self.state_root = current
            self._legacy_source_progress(c)

    def _legacy_source_progress(self, canonical):
        """Read advisory pre-registry progress only for the same installation."""
        from . import installation_registry as registry
        # A portable bundle runs from its extracted directory while the caller
        # can remain in the original source checkout.
        sources = dict.fromkeys(((self.repo / 'site.json').absolute(), Path.cwd() / 'site.json'))
        for candidate in sources:
            try:
                source = no_symlinks(candidate)
                if source == self.config_path or not source.exists() or source.stat().st_size > registry.MAX_BYTES:
                    continue
                registry._owner(source.stat())
                previous = json.loads(private_read(source))
                from .safety import validate_config
                validate_config(previous)
                if registry._identity(previous) != registry._identity(canonical):
                    continue
                identifier = hashlib.sha256(str(source).encode()).hexdigest()[:16]
                legacy = no_symlinks(source.parent / '.setup-state' / identifier)
                if legacy.is_dir() and legacy not in self._prior_state_roots:
                    self._prior_state_roots.append(legacy)
            except (SafetyError, OSError, ValueError, KeyError, TypeError, AttributeError):
                # An unrelated, unsafe or damaged checkout never overrides the
                # canonical site or blocks its valid current progress.
                continue

    def _remember_configuration(self, c):
        from .installation_registry import remember_verified
        saved = remember_verified(c, registry_root=self.registry_root)
        self.say('Installation configuration saved outside the source folder: ' + str(saved))
        return saved

    def _save_configuration(self, c):
        # The registry validates current storage and archives the prior version
        # before replacement. Never overwrite its authoritative site first.
        canonical = self._remember_configuration(c)
        if self.config_path != Path(canonical):
            write_json(self.config_path, c, replace=self.config_path.exists())

    def yes(self, question, default=False):
        while True:
            value = self.read(question + (' [Y/n]: ' if default else ' [y/N]: ')).strip().lower()
            if not value:
                return default
            if value in {'y', 'yes'}:
                return True
            if value in {'n', 'no'}:
                return False
            self.say('Enter y or n. Nothing has changed.')

    def _state_dir(self):
        self._refresh_state_root()
        no_symlinks(self.state_root)
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == 'posix' and self.state_root.stat().st_mode & 0o077:
            raise SafetyError('The setup state directory must be private (mode 700). Existing files were retained.')
        return self.state_root

    def state(self):
        self._refresh_state_root()
        for root in [self.state_root, *reversed(self._prior_state_roots)]:
            path = root / 'progress.json'
            if path.exists():
                result = json.loads(private_read(path))
                if not isinstance(result, dict):
                    raise SafetyError('Saved setup progress is invalid. Keep it for diagnosis; application data was not changed.')
                return result
        return {}

    def mark(self, number, status):
        state = self.state()
        state[str(number)] = {'status': status, 'checked': engine.now()}
        path = self._state_dir() / 'progress.json'
        write_json(path, state, replace=path.exists())

    def config(self):
        if not self.config_path.exists():
            raise SafetyError('First choose step 2, Connect storage. It saves your answers for later steps.')
        return load_config(self.config_path)

    def upgrade_storage_identity(self):
        """One reviewed recovery action; never guess a prior disk from sdX."""
        from . import storage_upgrade
        c = self.config()
        current = storage_upgrade.observed(c)
        self.say('Upgrade local storage identity after a reboot changed Linux disk names.')
        self.say('Current mounted media: ' + json.dumps(current['data'], sort_keys=True))
        self.say('Current mounted applications: ' + json.dumps(current['app'], sort_keys=True))
        self.say('Confirm the ORIGINAL filesystem UUIDs from your fstab, earlier inventory or backup. Do not infer them from the current /dev/sdX letters.')
        data_uuid = self.read('Original media filesystem UUID, or Enter to pause: ').strip()
        app_uuid = self.read('Original application filesystem UUID, or Enter to pause: ').strip()
        if not data_uuid or not app_uuid:
            self.say('Paused. No configuration or media was changed.')
            return False
        document = storage_upgrade.plan(c, self.config_path, expected_data_uuid=data_uuid,
            expected_app_uuid=app_uuid, registry_root=self.registry_root)
        path = self._state_dir() / ('storage-identity-' + document['review_hash'] + '.json')
        if not path.exists():
            write_json(path, document)
        self.say('Private reviewed plan and exact before-file backup content: ' + str(path))
        for row in document['changes']:
            self.say('  Update control file: ' + row['path'])
        self.say('Media, accounts, databases, VPN settings and running services are retained.')
        if not self.yes('Apply this exact control-file recovery?', False):
            return False
        result = storage_upgrade.apply(c, document, registry_root=self.registry_root)
        self.say('Filesystem UUID identities upgraded. Backup: ' + result['backup'])
        self.say('Resume the existing numbered application/torrent steps to verify and restart their owned services. No fresh setup or data deletion is needed.')
        return True

    def invoke(self, arguments, activity):
        """Keep advanced JSON/vendor errors private; never put secret values in argv."""
        attempt = self._state_dir() / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        # Isolated children must not import sitecustomize or modules from a
        # caller-supplied PYTHONPATH. The launcher validated this source root.
        entry = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from samurai_stack.cli import main; raise SystemExit(main())'
        command = [sys.executable, '-I', '-c', entry, str(self.repo), '--config', str(self.config_path), *arguments]
        stdout, stderr = attempt / 'result.json', attempt / 'diagnostic.txt'
        self.say(activity)
        with stdout.open('xb') as out, stderr.open('xb') as err:
            if os.name == 'posix':
                stdout.chmod(0o600); stderr.chmod(0o600)
            process = subprocess.Popen(command, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       start_new_session=True)
            started = time.monotonic()
            try:
                while True:
                    try:
                        code = process.wait(timeout=15)
                        break
                    except subprocess.TimeoutExpired:
                        self.say('  Still working (' + str(int(time.monotonic() - started)) + 's). Keep this terminal open.')
            except KeyboardInterrupt:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        # Do not leave package/deployment descendants running
                        # unseen, or force-kill them during receipt/lock cleanup.
                        self.say('Waiting for this interrupted operation to finish safely. Private attempt logs were retained.')
                        process.wait()
                raise
        try:
            result = json.loads(stdout.read_text()) if stdout.stat().st_size <= 4 * 1024 * 1024 else {}
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if code not in {0, 2}:
            # The existing CLI sanitizes SafetyError messages. Never dump a
            # vendor traceback, raw JSON, configuration or private credentials.
            diagnostic = stderr.read_text(errors='replace')
            line = next((s[7:] for s in diagnostic.splitlines() if s.startswith('ERROR: ')), None)
            if result.get('error_code') == 'EXISTING_INSTALLATION_IDENTITY_CONFLICT' and isinstance(result.get('details'), dict):
                self.say('Paused: ' + engine.format_project_collision(result['details']))
            else:
                self.say('Paused: ' + (line or 'This operation did not complete.'))
            self.say('Private diagnostic: ' + str(stderr))
            self.say('Your existing files were kept. Fix the cause, then select this step again.')
        return code, result

    def endpoints(self, c, stage):
        for service, url in catalog.stage_endpoints(c, stage).items():
            self.say('  ' + APP_NAMES.get(service, service) + ': ' + url)

    def install(self, c, stage):
        if stage:
            engine.check_docker()
            engine.check_container_ownership(c, stage)
            try:
                engine.preflight(c, stage)
                engine.service_health(c, stage)
                if (json.loads(engine.compose_path(c, stage).read_text()) == engine.recipe(c, stage)
                        and self.guarded_runtime(c, stage)):
                    self.say('The existing applications are already running. Continuing with their setup.')
                    return True
            except engine.ProjectCollision:
                raise
            except (SafetyError, OSError, ValueError):
                pass
        attempt = self._state_dir() / ('review-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        document = engine.plan(c, stage)
        path = attempt / 'plan.json'
        write_json(path, document)
        if stage == 0:
            self.say('Create missing media folders and test access/hardlinks at ' + c['storage']['data_root'] + '.')
            self.say('Existing files and permissions will be kept. No data will be moved.')
        else:
            names = [APP_NAMES.get(name, name) for name in engine.recipe(c, stage)['services']]
            self.say('Install/check: ' + ', '.join(names))
            self.say('Application state: ' + c['install_root'])
            self.say('Private LAN address: ' + c['bind_address'])
            if stage != 4:
                self.say('Media storage: ' + c['storage']['data_root'])
        self.say('Detailed review saved privately: ' + str(path))
        if not self.yes('Proceed with this step?', True):
            self.say('Paused. Select this step again whenever ready.')
            return False
        # The same reviewed hash/host/config validation remains inside the
        # engine. The operator approves the readable summary, not hash copying.
        code, result = self.invoke(['apply', str(stage), '--plan', str(path),
                               '--accept-plan', document['review_hash']],
                              'Applying this step. Image downloads can take several minutes...')
        for action in result.get('recovery_actions', []):
            if isinstance(action, dict) and isinstance(action.get('archive_path'), str):
                self.say('Archived the obsolete service definition: ' + action['archive_path'])
        return code == 0

    def guarded_runtime(self, c, stage):
        """Running containers alone cannot establish the intended boot guard."""
        try:
            root = Path(c['install_root'])
            saved = engine.stage_path(c, stage) / 'site.json'
            guard = root / 'runtime_guard.py'
            unit_name = f"samurai-{c['deployment_name']}-{stage}.service"
            unit = no_symlinks(UNIT_ROOT / unit_name).read_text()
            if (load_config(saved) != c or guard.read_bytes() != Path(engine.__file__).with_name('runtime_guard.py').read_bytes()
                    or '# Managed by Samurai Stack: ' + c['deployment_id'] not in unit
                    or f'ExecStartPre=/usr/bin/python3 {guard} {saved} {stage}\n' not in unit):
                return False
            command = ' '.join(['/usr/bin/docker', '--context', 'default', *engine.compose_args(c, stage)])
            if 'ExecStart=' + command + ' up --no-color --abort-on-container-failure --pull never\n' not in unit:
                return False
            engine.run(['systemctl', 'is-active', '--quiet', unit_name])
            engine.run(['systemctl', 'is-enabled', '--quiet', unit_name])
            return engine.run(['systemctl', 'show', '--property=NeedDaemonReload', '--value', unit_name]).stdout.strip() == 'no'
        except (SafetyError, OSError, ValueError):
            return False

    def _valid_secret(self, c, name, value):
        if name == 'vpn_wireguard_private_key':
            try:
                return len(base64.b64decode(value.strip(), validate=True)) == 32
            except (ValueError, binascii.Error):
                return False
        if name not in {'jellyfin_api_key', 'seerr_api_key', 'bazarr_api_key'}:
            return bool(value.strip())
        from .integrations import jellyfin_headers
        endpoints = catalog.stage_endpoints(c, 2)
        service = name.removesuffix('_api_key')
        path = {'jellyfin': '/System/Info', 'seerr': '/api/v1/settings/main', 'bazarr': '/api/system/status'}[service]
        headers = jellyfin_headers(value) if service == 'jellyfin' else {'X-Api-Key': value}
        try:
            result = engine.request('GET', endpoints[service] + path, headers=headers, timeout=10)
            return isinstance(result, dict)
        except (SafetyError, OSError, ValueError):
            return False

    def secret(self, c, name, instruction, *, replace=False):
        candidate = None
        try:
            candidate = engine.secret_resolver(c)(name)
        except (SafetyError, OSError, ValueError):
            pass
        path = engine.secret_path(c, name)
        if candidate is not None and self._valid_secret(c, name, candidate) and not replace:
            return True
        if path.exists() and candidate is None:
            raise SafetyError('An existing credential could not be read: ' + name + '. It was not overwritten.')
        if candidate is not None:
            self.say('The saved credential needs review, or its service is not reachable yet: ' + name + '.')
            self.say('Finish its application setup first. Replacement updates only the installer copy, keeps a private backup, and does not rotate the service account.')
            if not self.yes('Replace this saved credential?', False):
                return False
        self.say(instruction)
        self.say('Input is hidden. Press Enter to pause. Nothing is saved until this value passes its check.')
        while True:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    value = (self.secret_fn or getpass.getpass)('Private value: ')
                except getpass.GetPassWarning:
                    raise SafetyError('Hidden input needs an interactive terminal. No credential was saved.') from None
            if not value.strip():
                return False
            if self._valid_secret(c, name, value):
                break
            self.say('That value could not be confirmed. Check the value and that the service is ready; it was not saved.')
            if not self.yes('Try entering it again?', True):
                return False
        engine.initialize_install(c)
        if path.exists():
            backup = self._state_dir() / ('credential-before-' + uuid.uuid4().hex)
            atomic_write(backup, engine.read_secret(c, name))
        atomic_write(path, value.rstrip('\r\n') + '\n', replace=path.exists())
        self.say('Saved privately.')
        return True

    def report(self, result):
        for item in result.get('pending', [])[:8]:
            self.say('  Needs attention: ' + item.get('action', 'Finish application enrollment.'))
        for item in result.get('errors', [])[:8]:
            self.say('  ' + item.get('name', 'Application') + ': ' + item.get('error', 'Connection check failed.'))

    def wire(self, stage):
        retries = 0
        while True:
            code, result = self.invoke(['configure', str(stage)], 'Connecting applications and checking saved settings...')
            manual_pending = code == 2 and stage in {4, 6} and result.get('status') == 'pending'
            if code == 0 or manual_pending:
                code, proof = self.invoke(['verify', str(stage)], 'Verifying this step...')
                if code == 0 and proof.get('status') in {'verified', 'verified_with_manual_checks'}:
                    self.say('Application connections verified; source readiness is checked separately.'
                             if stage == 2 else 'Verified. This step is complete.')
                    return True
                result = proof
            self.report(result)
            if stage == 2:
                if code == 1 and retries < 2:
                    retries += 1
                    self.say('An application connection is not ready yet. Rechecking saved state automatically...')
                    time.sleep(retries)
                    continue
                self.say('Progress and diagnostics saved. Run ./setup 3 to resume; application accounts and media were retained.')
                return False
            if not self.yes('After fixing the items above, check again?', False):
                self.say('Progress saved. Select this step again to continue; no reinstall is needed.')
                return False

    def prepare(self):
        from .setup_host import prepare_host
        return prepare_host(self.repo)

    def hardlinks(self):
        self.say('Check Sonarr and Radarr hardlink settings in this existing installation.')
        self.say('This does not deploy containers, import media, move files or change library paths.')
        code, result = self.invoke(['hardlinks'], 'Reading both hardlink settings...')
        if code == 0 and result.get('ok') is True:
            self.say('Hardlinks are already enabled and verified in both applications.')
            return True
        self.report(result)
        if code != 2 or not self.yes('Enable hardlinks in both applications and verify the saved settings?', False):
            return False
        code, result = self.invoke(['hardlinks', '--apply'], 'Enabling hardlinks and reading the saved settings back...')
        if code != 0 or result.get('ok') is not True:
            self.report(result)
            return False
        code, result = self.invoke(['hardlinks'], 'Independently verifying both settings...')
        self.report(result)
        if code == 0 and result.get('ok') is True:
            self.say('Sonarr and Radarr hardlinks are enabled and verified. Existing media was not imported or changed.')
            return True
        return False

    def restored_accounts(self):
        self.say('Verify restored account identities before accepting replacement application directories.')
        self.say('Only previously verified accounts qualify. This updates a private receipt; it never creates or resets an account.')
        directory = self._state_dir() / ('account-restore-review-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        path = directory / 'plan.json'
        code, document = self.invoke(['restore-accounts', 'plan', '--out', str(path)],
                                     'Checking local application ownership and restored account identities...')
        if code != 0 or not isinstance(document.get('review_hash'), str):
            return False
        for item in document.get('changes', []):
            self.say('  Verified restored account directory: ' + APP_NAMES.get(item['app'], item['app']))
        if document.get('changes'):
            self.say('Private reviewed restore: ' + str(path))
            if not self.yes('Accept these verified restored directories and retire their first-user enrollment authority?', False):
                self.say('Restore receipt kept unchanged. No accounts were changed.')
                return False
            code, result = self.invoke(['restore-accounts', 'apply', '--plan', str(path), '--accept-plan', document['review_hash']],
                                       'Saving the original receipt and accepting the verified restore...')
            if code != 0 or result.get('ok') is not True:
                return False
        code, result = self.invoke(['restore-accounts', 'verify'], 'Verifying the restored receipt and existing accounts...')
        if code == 0 and result.get('ok') is True:
            self.say('Restored accounts verified. Resume the media applications with ./setup 3.')
            return True
        return False

    def storage(self):
        from .setup_storage import ensure_site_config
        c = ensure_site_config(self.config_path, input_fn=self.read, output_fn=self.say)
        # A replacement checkout must not prepare another storage identity for
        # the same still-existing Docker project, even before media is applied.
        engine.check_docker()
        engine.check_container_ownership(c, 2)
        if not self.install(c, 0):
            return False
        code, proof = self.invoke(['verify', '0'], 'Checking the mounted storage and media identity...')
        if code == 0 and proof.get('status') == 'verified':
            self._remember_configuration(c)
            self.say('Storage verified. Downloads and libraries use the same data tree.')
            return True
        return False

    def media_profiles(self):
        """Offer a reviewed policy without assigning profiles to existing titles."""
        self.say('Optional media policy: 720p/1080p HDTV, WEB and Blu-ray; original audio; parsed HDR/Dolby Vision names rejected.')
        self.say('English-origin titles keep English. Foreign titles keep their original language. Release metadata can be wrong.')
        self.say('Existing titles, files, queues and Seerr defaults are kept. New Seerr connections prefer this profile.')
        directory = self._state_dir() / ('profile-review-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)

        def reviewed_apply(include_sizes):
            path = directory / ('global-sizes.json' if include_sizes else 'profiles.json')
            arguments = ['profiles', 'plan', '--out', str(path)]
            if include_sizes:
                arguments.append('--global-sizes')
            code, document = self.invoke(arguments, 'Reading live Sonarr/Radarr policy schemas...')
            if code != 0 or not isinstance(document.get('review_hash'), str):
                return False
            for service, item in document.get('services', {}).items():
                self.say('  ' + APP_NAMES.get(service, service) + ': ' + str(item.get('action')) + ' ' + str(item.get('profile')))
                for change in item.get('size_changes', []):
                    limits = change['after']
                    self.say('    ' + str(change['quality']) + ': min/preferred/max ' + '/'.join(str(limits[key]) for key in ('minSize', 'preferredSize', 'maxSize')) + ' MB/minute')
            self.say('Private reviewed policy: ' + str(path))
            question = ('Apply these shared size limits to ALL profiles for the listed qualities?' if include_sizes
                        else 'Create/verify these optional profiles without changing existing titles?')
            if not self.yes(question, False):
                self.say('Policy kept unchanged. Use p in ./setup to review it later.')
                return None
            code, result = self.invoke(['profiles', 'apply', '--plan', str(path), '--accept-plan', document['review_hash']],
                                       'Saving the reviewed policy and verifying live readback...')
            if code != 0 or result.get('ok') is not True:
                return False
            self.say('Verified profile: ' + str(result.get('profile_name')))
            return True

        applied = reviewed_apply(False)
        if applied is not True:
            return applied is None
        self.say('File-size limits are shared application settings. Existing files stay in place; future grabs using other profiles are affected too.')
        if self.yes('Review the optional runtime-scaled size limits?', False):
            return reviewed_apply(True) is not False
        return True

    def media_naming(self, c):
        self.say('Optional future-import naming: Jellyfin TVDb/TMDb folder IDs and filenames retaining quality, audio, codec and release group.')
        self.say('This changes naming settings only. Existing title paths and files are not renamed, moved, imported or searched.')
        directory = self._state_dir() / ('naming-review-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        path = directory / 'plan.json'
        code, document = self.invoke(['naming', 'plan', '--out', str(path)], 'Checking owned Starr settings and rendered naming examples...')
        if code != 0 or not isinstance(document.get('review_hash'), str):
            return False
        services = document.get('services', {})
        if services and all(item.get('action') == 'verify' for item in services.values()):
            self.say('Future-import naming settings are already verified. Existing files were kept.')
            return True
        for service, item in services.items():
            self.say('  ' + APP_NAMES.get(service, service) + ': ' + ', '.join(item.get('changes', {})))
            for name, example in item.get('examples', {}).items():
                if name in {'seriesFolderExample', 'singleEpisodeExample', 'movieFolderExample', 'movieExample'}:
                    self.say('    ' + str(example))
        self.say('Private reviewed naming settings: ' + str(path))
        if not self.yes('Apply these naming settings for future imports and new title folders?', False):
            self.say('Naming settings were kept. Resume ./setup 3 to review them later; no existing-file rename was requested.')
            return True
        code, result = self.invoke(['naming', 'apply', '--plan', str(path), '--accept-plan', document['review_hash']],
                                   'Backing up the full original naming settings and applying the reviewed formats...')
        if code != 0 or result.get('ok') is not True:
            return False
        code, result = self.invoke(['naming', 'verify'], 'Independently reading back future-import naming settings...')
        if code != 0 or result.get('ok') is not True:
            return False
        self.say('Future-import naming settings saved and verified. Existing titles and files were kept.')
        return True

    def capacity_policy(self, c=None, *, automatic=False, after_torrents=False):
        """Review native floors and, once local qBit exists, its pause watcher."""
        from . import capacity
        c = c or self.config()
        saved = capacity._saved(c)
        data_gib = saved['data_reserve_bytes'] // capacity.GIB if saved else 50
        app_gib = saved['app_reserve_bytes'] // capacity.GIB if saved else 20
        enabled = saved['qbit_guard'] if saved else False
        ready = (c.get('features', {}).get('torrents') and c['hosts']['torrent'] == c['bind_address']
                 and engine.compose_path(c, 3).exists())
        if saved:
            code, report = self.invoke(['capacity', 'verify'], 'Checking the saved free-space safeguards...')
            if code == 0 and report.get('ok') is True:
                self.say('Free-space safeguards verified: data ' + str(data_gib) + ' GiB, application storage '
                         + str(app_gib) + ' GiB; qBit watcher ' + ('enabled.' if enabled else 'not enabled.'))
                if automatic and not (after_torrents and ready and not enabled):
                    return True
        self.say('Optional free-space safeguards. Reserves are pause/import thresholds, not quotas; running work can overshoot.')
        self.say('SAB pauses below its data reserve without automatic resume. Starr keeps free-space checks enabled; existing files are kept.')
        self.say('The optional qBit watcher also checks application storage, stops only this installation\'s incomplete downloads and preserves seeders.')
        if not self.yes('Review these safeguards now?', True):
            self.say('Capacity settings were kept. Return with ./setup --capacity; no downloads were resumed.')
            return True
        def reserve(label, default):
            while True:
                value = self.read(label + ' in GiB [' + str(default) + ']: ').strip() or str(default)
                if value.isascii() and value.isdigit() and 1 <= int(value) <= 1048576:
                    return int(value)
                self.say('Enter a whole GiB value from 1 to 1048576.')
        data_gib = reserve('Media/download storage reserve', data_gib)
        app_gib = reserve('Application storage reserve', app_gib)
        if ready:
            enabled = self.yes('Enable the owned qBit pause watcher for both reserves?', enabled or after_torrents)
        elif not enabled:
            self.say('The qBit watcher can be enabled after VPN/torrent setup. Application reserve is not enforced by native Starr/SAB floors.')
        directory = self._state_dir() / ('capacity-review-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        path = directory / 'plan.json'
        arguments = ['capacity', 'plan', '--out', str(path), '--data-reserve-gib', str(data_gib),
                     '--app-reserve-gib', str(app_gib), '--qbit-guard' if enabled else '--no-qbit-guard']
        code, document = self.invoke(arguments, 'Measuring the backing filesystems and reviewing native application settings...')
        if code != 0 or not isinstance(document.get('review_hash'), str):
            return False
        for role, item in document.get('capacity', {}).items():
            self.say('  ' + role + ' storage: ' + format(item['available_bytes'] / capacity.GIB, '.1f') + ' GiB available of '
                     + format(item['size_bytes'] / capacity.GIB, '.1f') + ' GiB.')
        for service, item in document.get('native', {}).items():
            self.say('  ' + APP_NAMES.get(service, service) + ': ' + json.dumps(item.get('after', {}), sort_keys=True))
        self.say('  Data reserve: ' + str(data_gib) + ' GiB. Application reserve: ' + str(app_gib)
                 + ' GiB. qBit watcher: ' + ('enabled.' if enabled else 'disabled.'))
        if document.get('watcher_source', {}).get('action') == 'upgrade_known_predecessor':
            self.say('  Upgrade the exact released capacity watcher after preserving its source in a private backup.')
            self.say('  Existing thresholds, incomplete-download scope and VPN safeguards remain enforced.')
        self.say('Private review: ' + str(path))
        if not self.yes('Apply these reviewed free-space safeguards and verify the saved settings?', False):
            self.say('Capacity settings were kept. No downloads were resumed or deleted.')
            return True
        code, result = self.invoke(['capacity', 'apply', '--plan', str(path), '--accept-plan', document['review_hash']],
                                   'Saving a private backup and applying only the reviewed safeguards...')
        if code != 0 or result.get('ok') is not True:
            return False
        code, result = self.invoke(['capacity', 'verify'], 'Independently verifying native floors and the optional pause timer...')
        if code != 0 or result.get('ok') is not True:
            return False
        self.say('Free-space safeguards verified. Freeing space never automatically resumes downloads; review and resume them deliberately.')
        return True

    def choose_storage(self):
        from .setup_local_storage import choose_replacement
        c = self.config()
        choose_replacement(c, self.config_path, input_fn=self.read, output_fn=self.say,
                           registry_root=self.registry_root)
        return self.storage()

    def account_password(self, label):
        while True:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    password = (self.secret_fn or getpass.getpass)(label + ' password (Enter to pause): ')
                    if not password:
                        return None
                    repeated = (self.secret_fn or getpass.getpass)('Repeat ' + label.lower() + ' password: ')
                except getpass.GetPassWarning:
                    raise SafetyError('Hidden input needs an interactive terminal. No credential was saved.') from None
            if password != repeated:
                self.say('Passwords differ. Try again; nothing was saved.')
            elif not 12 <= len(password) <= 256 or any(ord(char) < 32 or ord(char) == 127 for char in password):
                self.say('Use 12-256 characters without control characters. Spaces are preserved.')
            else:
                return password

    def account_name(self, label, *, different_from=None):
        while True:
            value = self.read(label + ' username (Enter to pause): ').strip()
            if not value:
                return None
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}', value):
                self.say('Use 1-64 letters, digits, dot, underscore, @, + or hyphen; start with a letter or digit.')
            elif different_from and value.casefold() == different_from.casefold():
                self.say('Choose a different username for everyday viewing.')
            else:
                return value

    def enroll_accounts(self, c):
        from . import onboarding
        self._offer_media_profiles = False
        self.say('Checking existing accounts and proving which applications still have no credentials...')
        claimed = onboarding.claim_unconfigured(c)
        if claimed['errors']:
            self.report(claimed)
            return None
        eligible = onboarding.eligible_apps(c)
        self._offer_media_profiles = bool(set(onboarding.pending_apps(c)).intersection({'sonarr', 'radarr'}))
        saved = engine.secret_path(c, 'onboarding_credentials.json')
        credentials = None
        needed = onboarding.requires_credentials(c)
        if needed and saved.exists():
            self.say('Checking the saved application administrator and viewer accounts. Passwords will not be replaced.')
        elif needed:
            self.say('Set one application administrator login for the media apps, plus a separate everyday Jellyfin/Seerr viewer.')
            self.say('These are application accounts, separate from the Linux root or SSH login.')
            self.say('If Jellyfin already has an account, enter its EXISTING administrator login. Choose a new login only for a fresh server.')
            self.say('For an existing everyday viewer, use its current password; otherwise choose a new, separate viewer login.')
            self.say('Leave their browser setup wizards alone until this automatic enrollment finishes; do not create the same accounts in parallel.')
            credentials = {}
            for role in ('admin', 'viewer'):
                label = 'Administrator' if role == 'admin' else 'Everyday viewer'
                username = self.account_name(label, different_from=credentials.get('admin_username'))
                if username is None:
                    return None
                while True:
                    password = self.account_password(label)
                    if password is None:
                        return None
                    if role == 'viewer' and password == credentials['admin_password']:
                        self.say('Use a different password for everyday viewing.')
                        continue
                    break
                credentials[role + '_username'], credentials[role + '_password'] = username, password
        self.say('Enrolling supported accounts and testing real logins and permissions...')
        result = onboarding.onboarding(c, credentials)
        write_json(self._state_dir() / ('accounts-' + uuid.uuid4().hex + '.json'), result)
        verified = {item['name'].split(':')[0] for item in result['checks'] if item.get('status') == 'passed'}
        for app in sorted(verified):
            self.say('  ' + APP_NAMES.get(app, app) + ': accounts and access checks passed.')
        unresolved = {'pending': result['pending'], 'errors': result['errors']}
        if unresolved['pending'] or unresolved['errors']:
            self.report(unresolved)
            self.say('Account setup paused. The private credentials and application state were retained for retry.')
            return None
        return verified

    def private_input(self, label):
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                return (self.secret_fn or getpass.getpass)(label + ' (hidden, Enter to pause): ') or None
            except getpass.GetPassWarning:
                raise SafetyError('Hidden input needs an interactive terminal. No credential was saved.') from None

    def usenet_provider(self, c):
        if not c['features'].get('usenet'):
            return True
        from . import usenet
        providers = usenet.providers(c)
        if providers:
            self.say('Testing the existing Usenet servers with their saved credentials. Settings will be kept.')
            report = usenet.verify(c)
            for item in report['results']:
                self.say('  ' + item['host'] + ': ' + item['status'])
            if not report['ok']:
                self.say('Usenet connection verification failed. Existing servers and credentials were retained; rerun step 3 after checking the provider.')
            return report['ok']
        provider = None
        saved = usenet.saved_provider(c)
        replace_unverified = False
        if saved and saved['safely_editable']:
            self.say('Saved, unverified NNTP input: ' + saved['host'] + ':' + str(saved['port']) + '.')
            while True:
                choice = self.read('Enter to retry saved details, edit to correct a typo, or q to pause: ').strip().lower()
                if choice in {'', 'edit', 'q'}:
                    break
                self.say('Press Enter, type edit, or type q.')
            if choice == 'q':
                return False
            replace_unverified = choice == 'edit'
        if not saved or replace_unverified:
            self.say('Enter your Usenet provider NNTP service details. The installer will configure SAB and test the connection.')
            self.say('TLS certificate verification stays enabled. Use the NNTP service password, which may differ from your website password.')
            host = self.read('NNTP server hostname, or Enter to pause: ').strip()
            if not host:
                return False
            while True:
                port = self.read('NNTP TLS port [563]: ').strip() or '563'
                connections = self.read('Maximum NNTP connections [8]: ').strip() or '8'
                if port.isascii() and port.isdigit() and 1 <= int(port) <= 65535 and connections.isascii() and connections.isdigit() and 1 <= int(connections) <= 100:
                    break
                self.say('Use a port from 1 to 65535 and a connection count from 1 to 100.')
            username = self.read('NNTP service username, or Enter to pause: ').strip()
            if not username:
                return False
            password = self.private_input('NNTP service password')
            if password is None:
                return False
            provider = {'host': host, 'port': int(port), 'username': username, 'password': password,
                        'connections': int(connections), 'ssl': True}
        credentials = None
        if usenet.needs_login(c):
            self.say('SAB already has its own login. Enter it once to finish the provider setup; the account will not be changed.')
            username = self.read('Existing SAB administrator username, or Enter to pause: ').strip()
            if not username:
                return False
            password = self.private_input('Existing SAB administrator password')
            if password is None:
                return False
            credentials = {'admin_username': username, 'admin_password': password}
        self.say('Testing NNTP authentication, saving the provider and verifying SAB setup...')
        result = usenet.configure(c, provider, credentials=credentials, replace_unverified=replace_unverified)
        if result.get('connection_verified') is not True or result.get('wizard_complete') is not True:
            raise SafetyError('Usenet setup did not confirm both the saved connection and wizard completion')
        self.say('Usenet provider authentication passed. SAB is configured and its setup wizard is complete.')
        return True

    def _indexer_report(self, c, report):
        self.say('Indexer source checks, separate from container health:')
        for source in report.get('sources', []):
            self.say('  ' + source['name'] + ': ' + source['status'] + ' (' + source['protocol'] + ').')
        for protocol, feature in (('usenet', 'usenet'), ('torrent', 'torrents')):
            if not c['features'].get(feature):
                continue
            row = report.get('protocols', {}).get(protocol, {})
            capabilities = ', '.join(name + '=' + ('yes' if row.get(key) else 'no')
                                     for name, key in (('movies', 'movies'), ('TV', 'tv'), ('anime', 'anime')))
            self.say('  ' + protocol + ': ' + str(row.get('tested_sources', 0)) + ' tested source(s); ' + capabilities + '.')
        for protocol in report.get('missing_protocols', []):
            self.say('  Needs attention: add or repair an enabled ' + protocol + ' source in Prowlarr.')
        if report.get('missing_categories'):
            self.say('  Needs attention: tested sources must advertise ' + ', '.join(report['missing_categories']) + ' categories.')
        self.say('These checks do not download media or prove downstream sync, playback or the VPN kill switch.')

    def indexer_sources(self, c):
        from . import indexer_setup
        self.say('Testing saved Prowlarr sources. Existing source settings and credentials will be kept.')
        document = indexer_setup.plan(c)
        report = document['readiness']
        self._indexer_report(c, report)
        if report.get('ok') is True:
            self.say('Media accounts, connections and source checks passed. This setup step is complete.')
            return True
        if not report.get('missing_protocols') and not report.get('missing_categories'):
            self.say('A saved enabled source is unhealthy or search-disabled. Repair it, or deliberately disable an unwanted source in Prowlarr, then rerun ./setup 3. No source was replaced.')
            return False
        saved = indexer_setup.saved_inputs(c)
        options = {}
        if c['features'].get('usenet'):
            options.update({'1': 'NZBGeek', '2': 'Generic Newznab'})
        if c['features'].get('torrents'):
            options['3'] = 'Generic Torznab'
        self.say('Optional source intake. NNTP is your download provider; this is the separate indexer/search account.')
        for key, label in options.items():
            self.say('  ' + key + '. Add ' + label)
        for number, source in enumerate(saved, 1):
            self.say('  r' + str(number) + '. Retry saved ' + source['label'] + ' at ' + source['base_url'])
        choice = self.read('Choose one source or saved retry, Enter to pause: ').strip().lower()
        input_id = None
        if choice.startswith('r') and choice[1:].isascii() and choice[1:].isdigit():
            number = int(choice[1:])
            if 1 <= number <= len(saved):
                input_id = saved[number - 1]['input_id']
        if input_id is None and choice not in options:
            self.say('Source setup remains pending. Existing services stay installed. Run ./setup 3 after adding/testing your authorized sources in Prowlarr, or to retry this intake.')
            return False
        if input_id is None:
            if choice == '1':
                source = {'preset': 'nzbgeek'}
            else:
                implementation = 'Newznab' if choice == '2' else 'Torznab'
                label = self.read('Short source label, or Enter to pause: ').strip()
                base_url = self.read('HTTPS indexer base URL, or Enter to pause: ').strip()
                if not label or not base_url:
                    return False
                api_path = self.read('API path [/api]: ').strip() or '/api'
                source = {'implementation': implementation, 'label': label, 'base_url': base_url, 'api_path': api_path}
            needs_key = choice != '3' or self.yes('Does this Torznab source require an API key?', True)
            source['api_key'] = self.private_input('Indexer API key') if needs_key else ''
            if needs_key and source['api_key'] is None:
                return False
            profiles = [row for row in document.get('app_profiles', [])
                        if row.get('enableAutomaticSearch') is True and row.get('enableInteractiveSearch') is True]
            if len(profiles) != 1:
                if not profiles:
                    self.say('Enable automatic and interactive searching in your intended Prowlarr application profile, then rerun ./setup 3. No source was added.')
                    return False
                self.say('Choose an existing Prowlarr application profile; its settings will be kept:')
                for row in profiles:self.say('  ' + str(row['id']) + '. ' + row['name'])
                selected = self.read('Application profile ID, or Enter to pause: ').strip()
                if not selected.isascii() or not selected.isdigit() or int(selected) not in {row['id'] for row in profiles}:
                    return False
                source['app_profile_id'] = int(selected)
            input_id = indexer_setup.save_input(c, source)
        document = indexer_setup.plan(c, input_id)
        action = document['action']
        self.say('Reviewed source: ' + action['name'] + ' at ' + action['base_url'] + action['api_path'] + '.')
        self.say('Only this source may be added. Existing indexers, application profiles and media are kept.')
        write_json(self._state_dir() / ('indexer-plan-' + uuid.uuid4().hex + '.json'), document)
        if not self.yes('Apply this reviewed source setup?', True):
            self.say('Private input saved for retry; no source was added. Run ./setup 3 to resume.')
            return False
        report = indexer_setup.apply(c, document)
        write_json(self._state_dir() / ('indexer-check-' + uuid.uuid4().hex + '.json'), report)
        self._indexer_report(c, report)
        if report.get('ok') is not True:
            self.say('Source checks remain pending. Run ./setup 3 to add the next missing source or review the specific failed source above. Nothing was removed.')
            return False
        self.say('Media accounts, connections and source checks passed. Test one legal download/import and playback next.')
        return True

    def subtitles(self, c):
        from . import subtitle_setup
        self.say('Optional English subtitles for newly added media, separate from existing-library backfill.')
        receipt = no_symlinks(Path(c['install_root']) / 'state/subtitle-setup.json')
        document = None
        if receipt.exists():
            saved = subtitle_setup._receipt(c, receipt)
            if saved.get('state') == 'verified':
                document = subtitle_setup.plan(c, saved['providers'])
                if not document.get('repair_profile'):
                    report = subtitle_setup.verify(c)
                    if report.get('ok') is not True or report.get('scope') != 'future_additions_only':
                        raise SafetyError('Saved subtitle policy did not pass future-only verification')
                    self.say('Your saved subtitle policy passed readback. Its provider choices were kept.')
                    for warning in report.get('notifications', []):self.say('  Needs attention: ' + warning)
                    self.say('English embedded tracks satisfy the profile. Existing SRT/ASS files are kept; no existing titles were assigned or searched.')
                    self.say('Provider registration was checked. A subtitle download has not been tested.')
                    return True
            providers = saved['providers']
        else:
            providers = subtitle_setup.DEFAULT_PROVIDERS
        document = document or subtitle_setup.plan(c, providers)
        if document.get('repair_profile'):
            self.say('This repairs the exact earlier installer-created English profile item keys. A complete private before snapshot is saved first; other profiles and assigned title IDs are kept.')
        self.say('Selected providers: ' + ', '.join(document['providers']) + '. The default Gestdown/ YIFY selection is account-free and covers TV and films.')
        self.say('This sets the English profile only for future additions, counts embedded subtitles, and uses one job at a time.')
        self.say('Existing profiles and subtitle files are kept. Upgrades, AI, postprocessing and scheduled backlog searches are disabled.')
        self.say('The selected provider list replaces the enabled-provider selection; saved provider credentials are kept.')
        self.say('Requires 50 GiB free on media storage and 20 GiB on application storage. No current title will be assigned or searched.')
        write_json(self._state_dir() / ('subtitle-plan-' + uuid.uuid4().hex + '.json'), document)
        if not self.yes('Apply this optional future-only English subtitle policy?', True):
            self.say('Subtitle policy was deferred. Media setup can continue; rerun ./setup 3 to review it later.')
            return True
        report = subtitle_setup.apply(c, document)
        if report.get('ok') is not True or report.get('scope') != 'future_additions_only':
            raise SafetyError('Subtitle setup did not confirm the reviewed future-only policy')
        write_json(self._state_dir() / ('subtitle-check-' + uuid.uuid4().hex + '.json'), report)
        self.say('Future English subtitle policy saved and verified. Existing media and subtitle files were kept.')
        for warning in report.get('notifications', []):self.say('  Needs attention: ' + warning)
        self.say('Provider registration was checked. A subtitle download has not been tested, and existing-library backfill was not run.')
        return True

    def media(self):
        c = self.config()
        if not self.install(c, 2):
            return False
        self._remember_configuration(c)
        enrolled = self.enroll_accounts(c)
        if enrolled is None:
            return False
        if not self.usenet_provider(c):
            return False
        self.say('\nYour media application addresses:')
        self.endpoints(c, 2)
        self.say('Accounts and API keys are ready. Seerr uses Jellyfin sign-in; use the everyday viewer for watching and requesting.')
        if self._offer_media_profiles:
            if not self.media_profiles():
                return False
        else:
            self.say('Optional HD/original-audio profiles are available from p in ./setup; existing profiles were kept.')
        self.say('Connecting the media applications automatically...')
        if not self.wire(2):
            return False
        if not self.media_naming(c):
            return False
        if not self.capacity_policy(c, automatic=True):
            return False
        if not self.subtitles(c):
            return False
        return self.indexer_sources(c)

    def torrents(self):
        c = self.config()
        if not c['features'].get('torrents'):
            self.say('Torrents were not selected in this site configuration. Existing settings were kept.')
            return False
        c = self.vpn_profile(c)
        vpn = c['vpn']
        replacement = False
        if engine.compose_path(c, 3).exists():
            try:
                engine.service_health(c, 3)
            except SafetyError:
                replacement = self.yes('The saved VPN is not healthy. Do you need to correct its saved credential?', False)
        if vpn['provider'] == 'custom':
            path = engine.secret_path(c, 'vpn_custom.conf')
            if not path.exists() or replacement:
                self.say('Keep the reviewed custom VPN profile in a private file on this guest (mode 600).')
                name = self.read('Path to that file, or Enter to pause: ').strip()
                if not name:
                    return False
                value = private_read(Path(name))
                if not value.strip():
                    raise SafetyError('The custom VPN profile is empty. Nothing was imported.')
                engine.initialize_install(c)
                if path.exists():
                    backup = self._state_dir() / ('credential-before-' + uuid.uuid4().hex)
                    atomic_write(backup, engine.read_secret(c, 'vpn_custom.conf'))
                atomic_write(path, value.rstrip('\r\n') + '\n', replace=path.exists())
                self.say('Profile imported privately; the source file was retained.')
            else:
                engine.read_secret(c, 'vpn_custom.conf')
        elif vpn['type'] == 'wireguard':
            instruction = 'Create a WireGuard configuration in your VPN account; copy only the PrivateKey value.'
            if vpn['provider'] == 'protonvpn':
                self.say('Proton: https://account.protonvpn.com > Downloads > WireGuard configuration.')
                if vpn.get('port_forwarding'):
                    self.say('Enable NAT-PMP (Port Forwarding) when generating that Proton configuration.')
            if not self.secret(c, 'vpn_wireguard_private_key', instruction, replace=replacement):
                return False
        else:
            for name in ('vpn_openvpn_username', 'vpn_openvpn_password'):
                if not self.secret(c, name, 'Enter the VPN provider service credential for ' + name + ', not a website password.', replace=replacement):
                    return False
        if vpn['type'] == 'openvpn' and vpn['provider'] == 'custom':
            for name in ('vpn_openvpn_username', 'vpn_openvpn_password'):
                if not self.secret(c, name, 'Enter the OpenVPN service credential for ' + name + '.', replace=replacement):
                    return False
        if replacement and engine.compose_path(c, 3).exists():
            if not self.yes('Restart this torrent stage to read the corrected VPN credential?', True):
                return False
            engine.run(['systemctl', 'restart', f"samurai-{c['deployment_name']}-3.service"])
        if not self.install(c, 3):
            return False
        self.endpoints(c, 3)
        if self.yes('View the generated qBittorrent login in this private terminal?', False):
            self.say('Username: ' + engine.read_secret(c, 'qbit_username').strip())
            self.say('Password: ' + engine.read_secret(c, 'qbit_password').strip())
        passed = self.wire(3)
        if passed:
            if not self.capacity_policy(c, automatic=True, after_torrents=True):
                return False
            self.say('Paste a legal test magnet in qBit. Manual files stay in downloads/torrents/manual on the data share.')
            self.say('Before bulk use, test a real download, import/playback and controlled VPN interruption/recovery.')
            self.say('Configuration verification does not claim an active kill-switch test.')
        return passed

    def vpn_profile(self, c):
        path = self._state_dir() / 'vpn-profile.json'
        prior = json.loads(private_read(path)) if path.exists() else {}
        if (engine.compose_path(c, 3).exists()
                or (prior.get('deployment_id') == c['deployment_id'] and prior.get('vpn') == c['vpn'])):
            self.say('Using the saved VPN selection: ' + c['vpn']['provider'] + ', ' + c['vpn']['type'] + '.')
            return c
        self.say('VPN provider: 1 Proton VPN, 2 Mullvad, 3 Custom profile.')
        default = {'protonvpn': '1', 'mullvad': '2', 'custom': '3'}[c['vpn']['provider']]
        while True:
            choice = self.read('Choose your VPN [' + default + ']: ').strip() or default
            if choice in {'1', '2', '3'}:
                break
            self.say('Choose 1, 2 or 3.')
        vpn = dict(c['vpn'])
        vpn['provider'] = {'1': 'protonvpn', '2': 'mullvad', '3': 'custom'}[choice]
        if vpn['provider'] != c['vpn']['provider']:
            # Assigned tunnel addresses belong to the provider that issued
            # them. In particular, Proton must not inherit a Mullvad address.
            vpn.pop('wireguard_addresses', None)
        if choice == '2':
            vpn.update(type='wireguard', port_forwarding=False)
            while True:
                assigned = self.read('Mullvad assigned IPv4 address including /prefix, or Enter to pause: ').strip()
                if not assigned:
                    raise SafetyError('The provider-issued WireGuard address is required. VPN choices were not saved.')
                try:
                    if '/' not in assigned or ipaddress.ip_interface(assigned).version != 4:
                        raise ValueError()
                    vpn['wireguard_addresses'] = assigned
                    break
                except ValueError:
                    self.say('Copy the assigned IPv4 address including its prefix, for example 10.64.0.2/32.')
        else:
            self.say('Protocol: 1 WireGuard (recommended), 2 OpenVPN.')
            while True:
                protocol = self.read('Protocol [1]: ').strip() or '1'
                if protocol in {'1', '2'}:
                    break
                self.say('Choose 1 or 2.')
            vpn['type'] = 'wireguard' if protocol == '1' else 'openvpn'
            vpn['port_forwarding'] = False
            if choice == '1':
                self.say('Port forwarding improves incoming peer reachability. Your Proton plan/configuration must support it.')
                vpn['port_forwarding'] = self.yes('Enable Proton port forwarding?', c['vpn'].get('port_forwarding', False))
                if vpn['port_forwarding'] and vpn['type'] == 'openvpn':
                    self.say('Use the Proton OpenVPN service username with the required +pmp suffix.')
        updated = dict(c, vpn=vpn)
        from .safety import validate_config
        validate_config(updated)
        backup = self._state_dir() / ('site-before-vpn-' + uuid.uuid4().hex + '.json')
        write_json(backup, c)
        self._save_configuration(updated)
        write_json(path, {'deployment_id': c['deployment_id'], 'vpn': vpn}, replace=path.exists())
        self.say('VPN selection saved. The private credential is entered next.')
        return updated

    def management(self):
        from . import management_setup
        c = self.config()
        if not self.install(c, 4):
            return False
        self.endpoints(c, 4)
        document = management_setup.plan(c, claim_unconfigured=True)
        creating = [name for name, row in document['apps'].items() if row['action'] == 'create_first_administrator']
        supplied = None
        saved = engine.secret_path(c, 'management_credentials.json')
        if saved.exists():
            self.say('Verifying the saved management login. Existing accounts will not be reset.')
        else:
            media = engine.secret_path(c, 'onboarding_credentials.json')
            if len(creating) == len(management_setup.SERVICES) and media.exists():
                from .onboarding import _private_json
                value = _private_json(media)
                supplied = {'username': value.get('admin_username'), 'password': value.get('admin_password')}
                self.say('Using the saved media administrator for these new management accounts.')
            else:
                self.say('Enter the management administrator login. Existing owners must use their current password; no password is reset.')
                username = self.account_name('Management administrator')
                if username is None:
                    return False
                while True:
                    password = self.private_input('Management administrator password')
                    if password is None:
                        return False
                    repeated = self.private_input('Repeat management administrator password')
                    if password != repeated:
                        self.say('Passwords differ; nothing was saved.')
                    elif not 8 <= len(password) <= 256 or any(ord(char) < 32 or ord(char) == 127 for char in password):
                        self.say('Use 8-256 characters without control characters. Spaces are preserved.')
                    else:
                        supplied = {'username': username, 'password': password}
                        break
        if creating:
            self.say('Create only the verified unconfigured owners: ' + ', '.join(creating) + '.')
            self.say('Add missing named service-listener monitors. Existing owners and custom monitors are preserved.')
            if not self.yes('Complete this reviewed management setup?', True):
                return False
        result = management_setup.apply(c, document, document['review_hash'], supplied)
        write_json(self._state_dir() / ('management-' + uuid.uuid4().hex + '.json'), result)
        if result.get('ok') is not True:
            return False
        self.say('Management logins and owned stack/monitor readback passed. These checks monitor private TCP listeners.')
        self.say('Notifications are optional and are not configured or delivery-tested by this step.')
        self.say('Interfaces remain private; Dockge controls this guest only. Homarr, if enabled, still needs its own account.')
        return self.wire(4)

    def extras(self):
        c = self.config()
        self.say('Optional extras: 1 Photos (Immich), 2 Music (Navidrome/slskd), Enter to skip.')
        choice = self.read('Choose an extra: ').strip()
        if choice not in {'1', '2'}:
            self.say('Skipped. The core stack does not need extras.')
            return False
        stage, feature = (5, 'photos') if choice == '1' else (6, 'music')
        if feature == 'photos':
            self.say('Photos need separate RAM headroom and an independent originals-plus-database backup/restore plan.')
        else:
            self.say('Music needs your Soulseek account and deliberate download/library import choices.')
        if not c['features'].get(feature):
            if not self.yes('Enable ' + feature + ' in this installation?', False):
                return False
            c['features'][feature] = True
            backup = self._state_dir() / ('site-before-' + uuid.uuid4().hex + '.json')
            write_json(backup, load_config(self.config_path))
            self._save_configuration(c)
        if stage == 6:
            for name in ('soulseek_username', 'soulseek_password'):
                if not self.secret(c, name, 'Enter your own ' + name + '.'):
                    return False
        if not self.install(c, stage):
            return False
        self.endpoints(c, stage)
        if stage == 5:
            self.say('Create the Immich owner, finish onboarding, then test an upload and an isolated backup restore.')
        else:
            self.say('Enroll Navidrome, play a real track, verify Soulseek connectivity and review the paths.')
        if not self.yes('Ready to check the extra?', False):
            return False
        if stage == 6:
            for check in engine.MANUAL_CHECKS[6]:
                self.say('  - ' + check)
            if not self.yes('Have ALL applicable checks actually been tested?', False):
                return False
            engine.attest(c, 6, 'ENROLLMENT COMPLETE')
        return self.wire(stage)

    def step(self, number):
        self.mark(number, 'paused')
        self.say('\nStep ' + str(number) + ': ' + STEPS[number])
        complete = {1: self.prepare, 2: self.storage, 3: self.media,
                    4: self.torrents, 5: self.management, 6: self.extras}[number]()
        self.mark(number, 'verified' if complete else 'paused')
        if not complete:
            self.say('Paused here. Run ./setup ' + str(number) + ' to resume this step.')
        return complete

    def menu(self):
        while True:
            self.say('\nSamurai Stack ' + __version__ + ' | Guided setup')
            state = self.state()
            for number, label in STEPS.items():
                status = state.get(str(number), {}).get('status', 'not started')
                self.say('  ' + str(number) + '. ' + label + ' [' + status + ']')
            self.say('  h. Check/enable hardlinks in existing Sonarr and Radarr')
            self.say('  p. Review optional quality, audio and file-size policy')
            self.say('  c. Review free-space safeguards and optional qBit pause watcher')
            self.say('  a. Verify and accept account directories after a real appdata restore')
            self.say('  r. Show read-only installation recovery inventory')
            self.say('Saved statuses show the last setup check, not live service monitoring.')
            default = next((i for i in range(1, 6) if state.get(str(i), {}).get('status') != 'verified'), 0)
            value = self.read('Choose a step, q to quit' + (' [' + str(default) + ']' if default else '') + ': ').strip().lower()
            if value in {'q', 'quit', 'exit'} or (not value and not default):
                self.say('Saved. Run ./setup whenever you want to continue.')
                return 0
            if value in {'h', 'hardlinks'}:
                try:
                    self.hardlinks()
                except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                    self.say('Hardlink check paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A local prerequisite failed.'))
                continue
            if value in {'r', 'recover', 'recovery'}:
                from .recovery import inventory
                self.say(json.dumps(inventory(), indent=2, sort_keys=True))
                continue
            if value in {'p', 'profiles'}:
                try:
                    self.media_profiles()
                except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                    self.say('Profile setup paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A local prerequisite failed.'))
                continue
            if value in {'c', 'capacity'}:
                try:
                    self.capacity_policy()
                except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                    self.say('Capacity setup paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A local prerequisite failed.'))
                continue
            if not value:
                value = str(default)
            if value in {'a', 'restore-accounts'}:
                try:
                    self.restored_accounts()
                except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                    self.say('Account restore paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A local prerequisite failed.'))
                continue
            if value not in {'1', '2', '3', '4', '5', '6'}:
                self.say('Choose a number from 1 to 6, h for hardlinks, p for profiles, c for capacity, a for restored accounts, r for recovery, or q.')
                continue
            try:
                self.step(int(value))
            except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                self._failure(int(value), exc)

    def _failure(self, number, exc):
        self.say('Paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A prerequisite or local operation failed.'))
        self.say('Existing application data was retained. Select the same step to retry.')
        try:
            self.mark(number, 'paused')
        except (SafetyError, OSError, ValueError, RuntimeError):
            self.say('Saved progress could not be updated. Keep the original site file and private setup state for review; no old progress file was removed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Guided setup. Plans, credentials and progress are handled for you.')
    parser.add_argument('step', nargs='?', type=int, choices=range(1, 7))
    parser.add_argument('--config', default=None, help='Advanced: explicit existing private site file; otherwise recover the host configuration')
    parser.add_argument('--recover', action='store_true', help='Read-only container, mount and configuration recovery inventory')
    parser.add_argument('--fresh', action='store_true', help='Start a new blank phases 1-6 wizard after checking obsolete installer metadata; never delete application or media data')
    parser.add_argument('--choose-storage', action='store_true', help='Correct an empty, unregistered installation storage choice without deleting media')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--hardlinks', action='store_true', help='Check/enable hardlinks only; no deployment or media import')
    parser.add_argument('--quality-profiles', action='store_true', help='Review optional HD/original-audio and size policy only')
    parser.add_argument('--capacity', action='store_true', help='Review native free-space thresholds and the optional owned qBit pause watcher')
    parser.add_argument('--restore-accounts', action='store_true', help='Review account directory identity after a verified backup restore')
    parser.add_argument('--upgrade-storage-identity', action='store_true', help='Review known original filesystem UUIDs after local block-device names changed; no media/account changes')
    args = parser.parse_args(argv)
    if args.upgrade_storage_identity and (args.fresh or args.recover or args.choose_storage or args.step or args.hardlinks or args.quality_profiles or args.restore_accounts or args.capacity):
        parser.error('Choose --upgrade-storage-identity alone, with an optional explicit --config')
    if args.fresh and (args.config is not None or args.step or args.recover or args.choose_storage or args.hardlinks or args.quality_profiles or args.restore_accounts or args.capacity):
        parser.error('Use --fresh alone to start a new blank phases 1-6 wizard')
    if args.recover and (args.step or args.hardlinks or args.choose_storage or args.quality_profiles or args.restore_accounts or args.capacity):
        parser.error('Choose --recover alone; it never runs an installation step')
    if args.choose_storage and (args.step or args.hardlinks or args.quality_profiles or args.restore_accounts or args.capacity):
        parser.error('Choose --choose-storage alone, then resume the numbered step')
    if args.choose_storage and args.config is None:
        parser.error('--choose-storage requires --config /path/to/site.json so automatic recovery cannot register the old storage choice first')
    if args.hardlinks and args.step:
        parser.error('Choose a numbered setup step or --hardlinks, not both')
    if args.quality_profiles and (args.step or args.hardlinks):
        parser.error('Choose --quality-profiles alone, not with another maintenance action')
    if args.restore_accounts and (args.step or args.hardlinks or args.quality_profiles):
        parser.error('Choose --restore-accounts alone, not with another maintenance action')
    if args.capacity and (args.step or args.hardlinks or args.quality_profiles or args.restore_accounts):
        parser.error('Choose --capacity alone, not with another maintenance action')
    if not sys.platform.startswith('linux') or os.geteuid() != 0 or Path('/etc/pve').exists():
        print('Run ./setup as administrator inside the intended Linux VM, not on the Proxmox host.', file=sys.stderr)
        return 1
    if not args.recover and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print('Use an interactive terminal for guided setup. No changes were made.', file=sys.stderr)
        return 1
    try:
        if args.recover:
            from .recovery import inventory
            print(json.dumps(inventory(), indent=2, sort_keys=True))
            return 0
        from .setup_recovery import resolve_configuration
        if args.fresh:
            from .setup_fresh import begin_fresh
            config_path = begin_fresh()
            print('Fresh setup: all six phases start as not started. No previous choices, credentials or progress were reused.')
            print('Existing packages and mounted filesystems will be checked in their normal phases. Existing media stays in place.')
        elif args.step == 1 and args.config is None:
            # Repairing Docker/host prerequisites must remain possible while
            # Docker is unavailable. No deployment UUID or site is created.
            from .installation_registry import draft_directory
            config_path = draft_directory() / 'site.json'
        else:
            config_path = resolve_configuration(args.config)
        ui = GuidedSetup(Path(__file__).resolve().parent.parent, config_path)
        if args.upgrade_storage_identity:
            return 0 if ui.upgrade_storage_identity() else 2
        if args.choose_storage:
            return 0 if ui.choose_storage() else 2
        if args.hardlinks:
            return 0 if ui.hardlinks() else 2
        if args.quality_profiles:
            return 0 if ui.media_profiles() else 2
        if args.capacity:
            return 0 if ui.capacity_policy() else 2
        if args.restore_accounts:
            return 0 if ui.restored_accounts() else 2
        if args.step:
            try:
                return 0 if ui.step(args.step) else 2
            except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                ui._failure(args.step, exc)
                return 1
        return ui.menu()
    except (EOFError, KeyboardInterrupt):
        print('\nPaused. Existing data and progress were kept. Run ./setup again to continue.')
        return 130
    except (SafetyError, OSError, ValueError, RuntimeError) as exc:
        print('Paused: ' + (str(exc) if isinstance(exc, SafetyError) else
            'A local prerequisite or saved state could not be read or updated.'), file=sys.stderr)
        print('Run ./setup --recover for a read-only inventory. Keep configuration and appdata; do not delete directories or rename the project to bypass a collision.', file=sys.stderr)
        print('Host preparation can be run independently with ./setup 1.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
