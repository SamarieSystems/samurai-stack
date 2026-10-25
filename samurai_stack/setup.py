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

STEPS = {1: 'Prepare Debian', 2: 'Connect storage', 3: 'Media applications',
         4: 'VPN and torrents', 5: 'Management', 6: 'Optional extras'}
APP_NAMES = {'jellyfin': 'Jellyfin', 'seerr': 'Seerr', 'sonarr': 'Sonarr',
             'radarr': 'Radarr', 'prowlarr': 'Prowlarr', 'bazarr': 'Bazarr',
             'sabnzbd': 'SABnzbd', 'qbittorrent': 'qBittorrent',
             'dockge': 'Dockge', 'uptime-kuma': 'Uptime Kuma', 'homarr': 'Homarr',
             'immich-server': 'Immich', 'navidrome': 'Navidrome', 'slskd': 'slskd',
             'music-assistant': 'Music Assistant'}


class GuidedSetup:
    def __init__(self, repo, config_path, *, input_fn=input, output_fn=print, secret_fn=None):
        self.repo = Path(repo)
        self.config_path = no_symlinks(Path(config_path).absolute())
        self.read, self.say = input_fn, output_fn
        self.secret_fn = secret_fn
        identity = hashlib.sha256(str(self.config_path).encode()).hexdigest()[:16]
        self.state_root = no_symlinks(self.config_path.parent / '.setup-state' / identity)

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
        no_symlinks(self.state_root)
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == 'posix' and self.state_root.stat().st_mode & 0o077:
            raise SafetyError('The setup state directory must be private (mode 700). Existing files were retained.')
        return self.state_root

    def state(self):
        path = self.state_root / 'progress.json'
        if not path.exists():
            return {}
        result = json.loads(private_read(path))
        if not isinstance(result, dict):
            raise SafetyError('Saved setup progress is invalid. Keep it for diagnosis; application data was not changed.')
        return result

    def mark(self, number, status):
        state = self.state()
        state[str(number)] = {'status': status, 'checked': engine.now()}
        path = self._state_dir() / 'progress.json'
        write_json(path, state, replace=path.exists())

    def config(self):
        if not self.config_path.exists():
            raise SafetyError('First choose step 2, Connect storage. It saves your answers for later steps.')
        return load_config(self.config_path)

    def invoke(self, arguments, activity):
        """Keep advanced JSON/vendor errors private; never put secret values in argv."""
        attempt = self._state_dir() / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        command = [sys.executable, str(self.repo / 'stack'), '--config', str(self.config_path), *arguments]
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
            self.say('Paused: ' + (line or 'This operation did not complete.'))
            self.say('Private diagnostic: ' + str(stderr))
            self.say('Your existing files were kept. Fix the cause, then select this step again.')
        return code, result

    def endpoints(self, c, stage):
        for service, url in catalog.stage_endpoints(c, stage).items():
            self.say('  ' + APP_NAMES.get(service, service) + ': ' + url)

    def install(self, c, stage):
        if stage:
            try:
                engine.preflight(c, stage)
                engine.check_container_ownership(c, stage)
                engine.service_health(c, stage)
                if (json.loads(engine.compose_path(c, stage).read_text()) == engine.recipe(c, stage)
                        and self.guarded_runtime(c, stage)):
                    self.say('The existing applications are already running. Continuing with their setup.')
                    return True
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
        code, _ = self.invoke(['apply', str(stage), '--plan', str(path),
                               '--accept-plan', document['review_hash']],
                              'Applying this step. Image downloads can take several minutes...')
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
        while True:
            code, result = self.invoke(['configure', str(stage)], 'Connecting applications and checking saved settings...')
            manual_pending = code == 2 and stage in {4, 6} and result.get('status') == 'pending'
            if code == 0 or manual_pending:
                code, proof = self.invoke(['verify', str(stage)], 'Verifying this step...')
                if code == 0 and proof.get('status') in {'verified', 'verified_with_manual_checks'}:
                    self.say('Verified. This step is complete.')
                    return True
                result = proof
            self.report(result)
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

    def storage(self):
        from .setup_storage import ensure_site_config
        c = ensure_site_config(self.config_path, input_fn=self.read, output_fn=self.say)
        if not self.install(c, 0):
            return False
        code, proof = self.invoke(['verify', '0'], 'Checking the mounted storage and media identity...')
        if code == 0 and proof.get('status') == 'verified':
            self.say('Storage verified. Downloads and libraries use the same data tree.')
            return True
        return False

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
        eligible = onboarding.eligible_apps(c)
        if not eligible:
            self.say('Existing application accounts are preserved. Finish any remaining first-run screens below; setup will not reset them.')
            return set()
        saved = engine.secret_path(c, 'onboarding_credentials.json')
        credentials = None
        if saved.exists():
            self.say('Checking the saved application administrator and viewer accounts. Passwords will not be replaced.')
        else:
            self.say('Set one application administrator login for Jellyfin, Sonarr, Radarr and Prowlarr, plus a separate everyday Jellyfin viewer.')
            self.say('These are application accounts. Use new passwords, separate from the Linux root or SSH login.')
            self.say('Leave their browser setup wizards alone until this automatic enrollment finishes; do not create the same accounts in parallel.')
            if not self.yes('Create and verify these fresh application accounts now?', True):
                return set()
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
        unresolved = {'pending': [item for item in result['pending'] if item['name'].split(':')[0] in eligible],
                      'errors': result['errors']}
        if unresolved['pending'] or unresolved['errors']:
            self.report(unresolved)
            self.say('Account setup paused. The private credentials and application state were retained for retry.')
            return None
        return verified

    def media(self):
        c = self.config()
        if not self.install(c, 2):
            return False
        enrolled = self.enroll_accounts(c)
        if enrolled is None:
            return False
        self.say('\nOpen these addresses from a browser on your LAN:')
        self.endpoints(c, 2)
        remaining = [APP_NAMES[name] for name in ('sonarr', 'radarr', 'prowlarr', 'bazarr') if name not in enrolled]
        if remaining:
            self.say('1. Create/verify administrator logins in ' + ', '.join(remaining) + '.')
        if 'jellyfin' not in enrolled:
            self.say('2. Finish the Jellyfin administrator wizard; create an API key in Dashboard > API Keys.')
        else:
            self.say('2. Jellyfin administrator, everyday viewer and API key are ready. Use the viewer for playback.')
        self.say('3. Finish Seerr onboarding against that Jellyfin address; its API key is in Settings > General.')
        self.say('4. Choose your quality profiles, indexers and subtitle languages/providers.')
        if c['features'].get('usenet'):
            self.say('5. In SABnzbd, enter the NNTP account and test its provider connection.')
        if not self.yes('Ready to connect the applications? You can come back later.', True):
            return False
        for name, instruction in [
            ('jellyfin_api_key', 'Paste the Jellyfin API key from Dashboard > API Keys.'),
            ('seerr_api_key', 'Paste the Seerr API key from Settings > General.'),
            ('bazarr_api_key', 'The Bazarr key was not readable locally. Copy its API key from Settings > General.'),
        ]:
            if not self.secret(c, name, instruction):
                return False
        return self.wire(2)

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
        write_json(self.config_path, updated, replace=True)
        write_json(path, {'deployment_id': c['deployment_id'], 'vpn': vpn}, replace=path.exists())
        self.say('VPN selection saved. The private credential is entered next.')
        return updated

    def management(self):
        c = self.config()
        if not self.install(c, 4):
            return False
        self.endpoints(c, 4)
        self.say('Create the management administrator accounts, add monitors, and send/receive a test notification.')
        self.say('These interfaces stay on your trusted LAN or private VPN. Dockge controls this guest only.')
        # The engine still records the explicit manual checklist separately
        # from live frontend/API verification. No hidden automatic attestation.
        for check in engine.MANUAL_CHECKS[4]:
            self.say('  - ' + check)
        if not self.yes('Have ALL applicable checklist items actually been tested?', False):
            return False
        engine.attest(c, 4, 'ENROLLMENT COMPLETE')
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
            write_json(self.config_path, c, replace=True)
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
            if not value:
                value = str(default)
            if value not in {'1', '2', '3', '4', '5', '6'}:
                self.say('Choose a number from 1 to 6, h for hardlinks, or q.')
                continue
            try:
                self.step(int(value))
            except (SafetyError, OSError, ValueError, RuntimeError) as exc:
                self._failure(int(value), exc)

    def _failure(self, number, exc):
        self.say('Paused: ' + (str(exc) if isinstance(exc, SafetyError) else 'A prerequisite or local operation failed.'))
        self.say('Existing application data was retained. Select the same step to retry.')
        self.mark(number, 'paused')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Guided setup. Plans, credentials and progress are handled for you.')
    parser.add_argument('step', nargs='?', type=int, choices=range(1, 7))
    parser.add_argument('--config', default='site.json', help='Advanced: existing private site file')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--hardlinks', action='store_true', help='Check/enable hardlinks only; no deployment or media import')
    args = parser.parse_args(argv)
    if args.hardlinks and args.step:
        parser.error('Choose a numbered setup step or --hardlinks, not both')
    if not sys.platform.startswith('linux') or os.geteuid() != 0 or Path('/etc/pve').exists():
        print('Run ./setup as administrator inside the intended Linux VM, not on the Proxmox host.', file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Use an interactive terminal for guided setup. No changes were made.', file=sys.stderr)
        return 1
    ui = GuidedSetup(Path(__file__).resolve().parent.parent, args.config)
    try:
        if args.hardlinks:
            return 0 if ui.hardlinks() else 2
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


if __name__ == '__main__':
    raise SystemExit(main())
