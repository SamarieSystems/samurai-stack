"""Recovery state survives source replacement without adopting storage or apps."""
import copy
import errno
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import installation_registry as registry
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest, write_json


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'private' / 'installations'
        self.units = self.root / 'units'
        self.c = default_config('192.168.50.21')
        self.guard = patch.object(registry, '_guard')
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.host = patch.object(registry, 'host_identity', return_value='a' * 64)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.verify = patch.object(registry.storage, 'verify', side_effect=lambda c, runner: {
            'storage': 'verified', 'mount': {**c['storage']['expected_mount'], 'options': 'rw,hard'}})
        self.verifier = self.verify.start()
        self.addCleanup(self.verify.stop)
        # Ownership checks remain real on root Linux; normal Windows tests have
        # no POSIX owner identity. Never patch file type/link/permission checks.
        if os.name == 'posix' and os.geteuid() != 0:
            owner = patch.object(registry, '_owner')
            owner.start()
            self.addCleanup(owner.stop)

    def remember(self, config=None):
        return registry.remember_verified(config or self.c, registry_root=self.registry)

    def discover(self, **kwargs):
        return registry.discover(registry_root=self.registry, unit_root=self.units, **kwargs)

    def private_config(self, name, config):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / 'site.json'
        write_json(path, config)
        return path

    def test_failed_or_incomplete_storage_proof_writes_nothing(self):
        for value in (SafetyError('fixture verification refusal'), {}, {'storage': 'prepared'},
                      {'storage': 'verified', 'mount': {'source': 'different'}}):
            self.verifier.side_effect = value if isinstance(value, Exception) else None
            self.verifier.return_value = value
            with self.subTest(proof=type(value).__name__), self.assertRaises(SafetyError):
                self.remember()
            self.assertFalse(self.registry.parent.exists())

    def test_first_save_load_and_stable_source_independent_state(self):
        path = self.remember()
        self.assertEqual(path, self.registry / self.c['deployment_id'] / 'site.json')
        self.assertEqual(registry.load(self.c['deployment_id'], registry_root=self.registry), self.c)
        state = registry.state_directory(self.c['deployment_id'], registry_root=self.registry)
        write_json(state / 'progress.json', {'2': {'status': 'verified'}})
        # Deleting/replacing an unrelated checkout cannot change its location.
        self.assertEqual(state, registry.state_directory(self.c['deployment_id'], registry_root=self.registry))
        self.assertEqual(json.loads((state / 'progress.json').read_text())['2']['status'], 'verified')
        identity = json.loads((path.parent / 'identity.json').read_text())
        self.assertEqual(identity['host_id'], 'a' * 64)
        self.assertEqual(identity['identity']['data_root'], '/mnt/media/samurai')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)

    def test_idempotent_save_reverifies_without_replacing_config(self):
        path = self.remember()
        before = path.stat().st_mtime_ns, path.read_bytes()
        self.assertEqual(path, self.remember())
        self.assertEqual(before, (path.stat().st_mtime_ns, path.read_bytes()))
        self.assertEqual(self.verifier.call_count, 2)
        self.assertFalse((path.parent / 'history').exists())

    def test_valid_legacy_config_without_optional_role_uses_all_identity(self):
        del self.c['role']
        path = self.remember()
        self.assertEqual(json.loads((path.parent / 'identity.json').read_text())['identity']['role'], 'all')
        self.assertEqual(registry.load(self.c['deployment_id'], registry_root=self.registry), self.c)

    def test_mutable_feature_update_archives_previous_exact_configuration(self):
        path = self.remember()
        changed = copy.deepcopy(self.c)
        changed['features']['dashboard'] = True
        changed['vpn']['port_forwarding'] = True
        self.remember(changed)
        archive = path.parent / 'history' / ('site-' + digest(self.c) + '.json')
        self.assertEqual(json.loads(archive.read_text()), self.c)
        self.assertEqual(json.loads(path.read_text()), changed)
        self.assertFalse((path.parent / 'registry.lock').exists())

    def test_immutable_identity_changes_refuse_without_touching_cache(self):
        path = self.remember()
        before = path.read_bytes()
        changes = [lambda c: c.update(install_root='/opt/another-stack'),
                   lambda c: c.update(deployment_name='other-name'),
                   lambda c: c.update(puid=1001),
                   lambda c: c['storage'].update(data_root='/mnt/media/other'),
                   lambda c: c['storage']['expected_mount'].update(source='192.168.50.30:/media')]
        for change in changes:
            c = copy.deepcopy(self.c)
            change(c)
            with self.subTest(change=changes.index(change)), self.assertRaises(SafetyError):
                self.remember(c)
            self.assertEqual(path.read_bytes(), before)

    def test_host_mismatch_refuses_load_and_update(self):
        path = self.remember()
        before = path.read_bytes()
        with patch.object(registry, 'host_identity', return_value='b' * 64):
            with self.assertRaises(SafetyError):
                registry.load(self.c['deployment_id'], registry_root=self.registry)
            with self.assertRaises(SafetyError):
                self.remember()
        self.assertEqual(path.read_bytes(), before)

    def test_identity_only_interrupted_first_save_resumes_same_identity(self):
        real_write = registry.write_json
        def failed_site(path, value, **kwargs):
            if path.name == 'site.json':
                raise OSError(errno.ENOSPC, 'fixture full disk')
            return real_write(path, value, **kwargs)
        with patch.object(registry, 'write_json', side_effect=failed_site), self.assertRaises(OSError):
            self.remember()
        self.assertEqual(self.discover(), [])
        self.assertEqual(json.loads(self.remember().read_text()), self.c)

    def test_failed_update_keeps_old_config_and_private_archived_copy(self):
        path = self.remember()
        before = path.read_bytes()
        changed = copy.deepcopy(self.c)
        changed['features']['dashboard'] = True
        real_write = registry.write_json
        def failed_site(target, value, **kwargs):
            if target == path:
                raise OSError(errno.ENOSPC, 'fixture full disk')
            return real_write(target, value, **kwargs)
        with patch.object(registry, 'write_json', side_effect=failed_site), self.assertRaises(OSError):
            self.remember(changed)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list((path.parent / 'history').glob('*.json'))), 1)
        self.assertEqual(self.remember(changed), path)

    def test_existing_unbound_cache_and_active_lock_are_never_overwritten(self):
        self.registry.mkdir(parents=True, mode=0o700)
        directory = self.registry / self.c['deployment_id']
        directory.mkdir(mode=0o700)
        write_json(directory / 'site.json', self.c)
        with self.assertRaisesRegex(SafetyError, 'unbound'):
            self.remember()
        self.assertEqual(json.loads((directory / 'site.json').read_text()), self.c)
        # A refused cache is not silently repaired; an exclusive existing lock
        # also remains available for operator diagnosis.
        write_json(directory / 'registry.lock', {'pid': 12345})
        with self.assertRaisesRegex(SafetyError, 'busy'):
            self.remember()
        self.assertTrue((directory / 'registry.lock').exists())

    def test_different_uuid_cannot_reserve_same_application_directory(self):
        path = self.remember()
        other = default_config('192.168.50.21')
        other['storage']['data_root'] = '/mnt/media/other-stack'
        with self.assertRaisesRegex(SafetyError, 'already reserves'):
            self.remember(other)
        self.assertFalse((self.registry / other['deployment_id']).exists())
        self.assertEqual(json.loads(path.read_text()), self.c)

    def test_global_lock_prevents_cross_uuid_concurrent_publication(self):
        self.registry.mkdir(parents=True, mode=0o700)
        write_json(self.registry / '.registry.lock', {'pid': 12345})
        with self.assertRaisesRegex(SafetyError, 'busy'):
            self.remember()
        self.assertFalse((self.registry / self.c['deployment_id']).exists())
        self.assertTrue((self.registry / '.registry.lock').exists())

    def test_noncanonical_uuid_secret_fields_and_oversized_files_refuse(self):
        c = copy.deepcopy(self.c)
        c['deployment_id'] = c['deployment_id'].upper()
        with self.assertRaises(SafetyError):
            self.remember(c)
        c = copy.deepcopy(self.c)
        c['admin_password'] = 'fixture-do-not-save'  # release-audit: synthetic - rejected password fixture
        with self.assertRaises(SafetyError):
            self.remember(c)
        self.assertFalse(self.registry.exists())
        path = self.remember()
        path.write_bytes(b' ' * (registry.MAX_BYTES + 1))
        with self.assertRaises(SafetyError):
            registry.load(self.c['deployment_id'], registry_root=self.registry)

    def test_hardlinked_config_refuses_even_when_content_is_valid(self):
        path = self.remember()
        os.link(path, path.parent / 'untrusted-hardlink')
        with self.assertRaises(SafetyError):
            registry.load(self.c['deployment_id'], registry_root=self.registry)
        with self.assertRaises(SafetyError):
            self.remember()

    @unittest.skipUnless(os.name == 'posix', 'Private directory mode checks are POSIX-only')
    def test_nonprivate_registry_and_symlink_configs_refuse_without_chmod(self):
        self.registry.mkdir(parents=True, mode=0o755)
        self.registry.chmod(0o755)
        with self.assertRaises(SafetyError):
            self.remember()
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o755)
        self.registry.chmod(0o700)
        path = self.remember()
        original = path.parent / 'original.json'
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(SafetyError):
            registry.load(self.c['deployment_id'], registry_root=self.registry)
        self.assertTrue(path.is_symlink())

    def test_discovery_is_readonly_and_empty_host_creates_nothing(self):
        self.assertEqual(self.discover(), [])
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.units.exists())
        self.verifier.assert_not_called()

    def test_canonical_config_wins_over_valid_older_mutable_snapshots(self):
        path = self.remember()
        old = copy.deepcopy(self.c)
        old['features']['torrents'] = False
        saved = self.private_config('old-source', old)
        before = path.read_bytes(), saved.read_bytes()
        result = self.discover(config_paths=[saved])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['path'], path)
        self.assertEqual(result[0]['config'], self.c)
        self.assertEqual(result[0]['aliases'], [saved])
        self.assertEqual(before, (path.read_bytes(), saved.read_bytes()))

    def test_legacy_discovery_retains_distinct_mutable_snapshots_for_review(self):
        a = self.private_config('old-a', self.c)
        changed = copy.deepcopy(self.c)
        changed['features']['dashboard'] = True
        b = self.private_config('old-b', changed)
        duplicate = self.private_config('old-duplicate', self.c)
        result = self.discover(config_paths=[a, b, duplicate])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['aliases'], [duplicate])
        self.assertEqual({row['path'] for row in result}, {a, b})
        self.assertFalse(self.registry.exists())

    def test_discovery_refuses_conflicting_uuid_or_same_install_root(self):
        a = self.private_config('old-a', self.c)
        changed = copy.deepcopy(self.c)
        changed['storage']['data_root'] = '/mnt/media/other'
        b = self.private_config('old-b', changed)
        with self.assertRaisesRegex(SafetyError, 'Conflicting'):
            self.discover(config_paths=[a, b])
        changed = default_config('192.168.50.21')
        write_json(b, changed, replace=True)
        with self.assertRaisesRegex(SafetyError, 'Conflicting'):
            self.discover(config_paths=[a, b])

    def test_draft_state_is_stable_host_bound_and_private(self):
        setup = self.root / 'setup'
        first = registry.draft_directory(setup_root=setup)
        self.assertEqual(first, registry.draft_directory(setup_root=setup))
        self.assertEqual(first, setup / ('a' * 64))
        self.assertFalse(self.registry.exists())

    @unittest.skipUnless(os.name == 'posix', 'Existing setup ancestor modes require POSIX')
    def test_existing_setup_parent_755_accepts_private_draft_without_chmod_but_775_refuses(self):
        setup = self.root / 'setup'
        setup.mkdir(mode=0o755)
        setup.chmod(0o755)
        draft = registry.draft_directory(setup_root=setup)
        self.assertEqual(draft, setup / ('a' * 64))
        self.assertEqual(stat.S_IMODE(setup.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o700)
        write_json(draft / 'progress.json', {'fixture': 'retained'})
        before = (draft / 'progress.json').read_bytes()
        setup.chmod(0o775)
        with self.assertRaisesRegex(SafetyError, 'unsafe permissions'):
            registry.draft_directory(setup_root=setup)
        self.assertEqual(stat.S_IMODE(setup.stat().st_mode), 0o775)
        self.assertEqual((draft / 'progress.json').read_bytes(), before)
        self.assertFalse(self.registry.exists())

    def test_missing_config_in_managed_service_blocks_new_uuid_discovery(self):
        self.units.mkdir(mode=0o700)
        unit = self.units / ('samurai-' + self.c['deployment_name'] + '-2.service')
        # A real generated unit references this missing path after /opt deletion.
        missing = self.root / 'deleted-appstate' / 'site.json'
        text = ('# Managed by Samurai Stack: ' + self.c['deployment_id'] + '\n'
                'ExecStartPre=/usr/bin/python3 /opt/samurai/runtime_guard.py ' + shlex.quote(str(missing)) + ' 2\n')
        unit.write_text(text)
        unit.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, 'missing saved configuration'):
            self.discover()
        self.assertFalse(self.registry.exists())

    def test_missing_runtime_uses_only_matching_host_bound_canonical_with_warning(self):
        if os.name == 'posix':
            self.c['install_root'] = str(self.root / 'apps')
        canonical = self.remember()
        self.units.mkdir(mode=0o700)
        unit = self.units / ('samurai-' + self.c['deployment_name'] + '-2.service')
        guard = Path(self.c['install_root']) / 'runtime_guard.py'
        runtime = Path(self.c['install_root']) / 'stages' / (self.c['deployment_name'] + '-2') / 'site.json'
        text = ('# Managed by Samurai Stack: ' + self.c['deployment_id'] + '\n'
                'ExecStartPre=/usr/bin/python3 ' + shlex.quote(str(guard)) + ' ' + shlex.quote(str(runtime)) + ' 2\n')
        unit.write_text(text)
        unit.chmod(0o644)
        rows = self.discover()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], canonical)
        self.assertEqual(rows[0]['aliases'], [runtime])
        self.assertEqual(len(rows[0]['warnings']), 1)
        self.assertIn('inspect application data', rows[0]['warnings'][0])
        self.assertFalse(runtime.exists())
        # A valid registry cannot bless arbitrary missing paths from an old unit.
        unit.write_text(text.replace(shlex.quote(str(runtime)), shlex.quote(str(self.root / 'other' / 'site.json'))))
        with self.assertRaisesRegex(SafetyError, 'conflicts'):
            self.discover()

    def test_canonical_cannot_bless_foreign_missing_unit_identity(self):
        if os.name == 'posix':
            self.c['install_root'] = str(self.root / 'apps')
        self.remember()
        self.units.mkdir(mode=0o700)
        unit = self.units / ('samurai-' + self.c['deployment_name'] + '-2.service')
        other = default_config()['deployment_id']
        guard = Path(self.c['install_root']) / 'runtime_guard.py'
        runtime = Path(self.c['install_root']) / 'stages' / (self.c['deployment_name'] + '-2') / 'site.json'
        unit.write_text('# Managed by Samurai Stack: ' + other + '\n'
                        'ExecStartPre=/usr/bin/python3 ' + shlex.quote(str(guard)) + ' ' + shlex.quote(str(runtime)) + ' 2\n')
        unit.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, 'missing saved configuration'):
            self.discover()

    @unittest.skipUnless(os.name == 'posix', 'Generated Linux unit paths require POSIX paths')
    def test_owned_runtime_unit_migrates_config_without_storage_or_service_operations(self):
        c = copy.deepcopy(self.c)
        c['install_root'] = str(self.root / 'application')
        stage = Path(c['install_root']) / 'stages' / (c['deployment_name'] + '-2')
        stage.mkdir(parents=True, mode=0o700)
        write_json(stage / 'site.json', c)
        self.units.mkdir(mode=0o700)
        unit = self.units / ('samurai-' + c['deployment_name'] + '-2.service')
        unit.write_text('# Managed by Samurai Stack: ' + c['deployment_id'] + '\n'
                        'ExecStartPre=/usr/bin/python3 ' + str(Path(c['install_root']) / 'runtime_guard.py') +
                        ' ' + str(stage / 'site.json') + ' 2\n')
        unit.chmod(0o644)
        result = self.discover()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['config'], c)
        self.assertEqual(result[0]['source'], 'service')
        self.verifier.assert_not_called()
        self.assertFalse(self.registry.exists())

    @unittest.skipUnless(os.name == 'posix', 'Historical runtime parent modes require POSIX')
    def test_runtime_parent_755_is_accepted_but_group_writable_775_is_refused(self):
        c = copy.deepcopy(self.c)
        c['install_root'] = str(self.root / 'application')
        stage = Path(c['install_root']) / 'stages' / (c['deployment_name'] + '-2')
        stage.mkdir(parents=True, mode=0o755)
        stage.chmod(0o755)
        config = stage / 'site.json'
        write_json(config, c)
        self.units.mkdir(mode=0o700)
        unit = self.units / ('samurai-' + c['deployment_name'] + '-2.service')
        unit.write_text('# Managed by Samurai Stack: ' + c['deployment_id'] + '\n'
                        'ExecStartPre=/usr/bin/python3 ' + str(Path(c['install_root']) / 'runtime_guard.py') +
                        ' ' + str(config) + ' 2\n')
        unit.chmod(0o644)
        before = config.read_bytes()
        self.assertEqual(self.discover()[0]['config'], c)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        stage.chmod(0o775)
        with self.assertRaisesRegex(SafetyError, 'unsafe permissions'):
            self.discover()
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o775)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(self.registry.exists())


if __name__ == '__main__':
    unittest.main()
