"""Native first-owner boundaries and idempotent photo resource policy."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import engine, extras, onboarding, photos_setup as photos, setup
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json

CREDS = {"email": "fixture-owner@example.invalid", "password": "fixture-photo-password", "name": "Fixture Owner"}  # release-audit: synthetic - isolated photo owner input
OWNER = "9562898f-03b4-4593-9f68-679cf4238656"
DIRECTORY = {"device": 1, "inode": 42}


class Native:
    def __init__(self, c):
        self.initialized = False
        self.onboarded = False
        self.calls = []
        self.owner = {'id': OWNER, 'email': CREDS['email'], 'name': CREDS['name'], 'isAdmin': True,
                      'quotaSizeInBytes': None, 'shouldChangePassword': False}
        self.config = extras.photo_policy(c)
        for row in self.config['job'].values():
            row['concurrency'] = 4
        self.config['ffmpeg'].update(threads=0, preset='medium')
        self.config['unrelated'] = {'librarySchedule': 'original'}

    def request(self, method, url, **kw):
        path = url.split('/api', 1)[1]
        self.calls.append((method, path, deepcopy(kw)))
        if (method, path) == ('GET', '/server/config'):
            return {'isInitialized': self.initialized, 'isOnboarded': self.onboarded, 'maintenanceMode': False}
        if (method, path) == ('POST', '/auth/admin-sign-up'):
            if self.initialized:
                raise SafetyError('Server already initialized')
            self.initialized = True
            return {'id': OWNER}
        if (method, path) == ('POST', '/auth/login'):
            if not self.initialized or kw['json'] != {k: CREDS[k] for k in ('email', 'password')}:
                raise SafetyError('fixture-private-server-message')
            return {'accessToken': 'fixture-session-token', 'userEmail': CREDS['email'], 'userId': OWNER, 'isAdmin': True}  # release-audit: synthetic - ephemeral native session fixture
        if (method, path) == ('POST', '/auth/logout'):
            return {'successful': True}
        if (method, path) == ('GET', '/admin/users/' + OWNER):
            return deepcopy(self.owner)
        if (method, path) == ('PUT', '/admin/users/' + OWNER):
            self.owner.update(kw['json'])
            return deepcopy(self.owner)
        if (method, path) == ('GET', '/admin/config'):
            return deepcopy(self.config)
        if (method, path) == ('PUT', '/admin/config'):
            self.config = deepcopy(kw['json'])
            return deepcopy(self.config)
        if (method, path) == ('POST', '/system-metadata/admin-onboarding'):
            self.onboarded = kw['json']['isOnboarded']
            return None
        raise AssertionError((method, path))


class PhotoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for part in ('state', 'secrets', 'backups', 'appdata/immich-postgres'):
            (self.root / part).mkdir(parents=True)
        self.c = default_config()
        self.c['install_root'] = str(self.root)
        self.c['features']['photos'] = True
        self.c['extras'] = {'photos': {'machine_learning': False}}
        self.enterContext(patch.object(photos, '_paths', return_value=(self.root, self.root / 'state/photos-onboarding.json')))
        self.enterContext(patch.object(engine, 'host_identity', return_value='fixture-host'))
        self.enterContext(patch.object(engine, 'deployment_lock', side_effect=lambda _: nullcontext()))
        self.enterContext(patch.object(onboarding, '_identity', return_value=DIRECTORY))
        self.enterContext(patch.object(onboarding, '_root_owned', return_value=True))
        self.target = self.enterContext(patch.object(photos, 'check_target', return_value={'container': 'a'*12, 'image': 'fixture-image'}))
        self.native = Native(self.c)
        photos.save_credentials(self.c, CREDS)

    def reservation(self, **changes):
        value = {'schema_version': 1, **photos._binding(self.c), 'directory': DIRECTORY,
                 'eligible': True, 'state': 'reserved', **changes}
        path = self.root / 'state/photos-onboarding.json'
        write_json(path, value, replace=path.exists())

    def plan(self):
        return photos.plan(self.c, request=self.native.request)

    def apply(self, document):
        return photos.apply(self.c, document, document['review_hash'], request=self.native.request)

    def test_unknown_empty_server_is_never_a_first_owner_claim(self):
        with self.assertRaisesRegex(SafetyError, 'not reserved fresh'):
            self.plan()
        self.assertEqual([row[0] for row in self.native.calls], ['GET'])

    def test_first_claim_resource_merge_real_login_and_retry_are_bounded(self):
        self.reservation()
        document = self.plan()
        self.assertNotIn(CREDS['password'], json.dumps(document))
        report = self.apply(document)
        self.assertTrue(report['ok'])
        self.assertFalse(report['backup_restore_tested'])
        self.assertEqual(self.native.config['unrelated'], {'librarySchedule': 'original'})
        self.assertEqual(self.native.config['ffmpeg']['preset'], 'medium')
        self.assertTrue(all(row['concurrency'] == 1 for row in self.native.config['job'].values()))
        self.assertFalse(self.native.config['machineLearning']['enabled'])
        self.assertTrue(self.native.onboarded)
        self.assertTrue(photos.verify(self.c, request=self.native.request)['ok'])
        second = self.plan()
        self.assertTrue(self.apply(second)['ok'])
        writes = [(m, p) for m, p, _ in self.native.calls if m in {'PUT', 'PATCH', 'DELETE'} or p == '/auth/admin-sign-up']
        self.assertEqual(writes, [('POST', '/auth/admin-sign-up'), ('PUT', '/admin/config'), ('PUT', '/admin/users/' + OWNER)])
        self.assertEqual(self.native.owner['quotaSizeInBytes'], 2 * 1024 ** 3)
        self.assertTrue(all(set(kw['json']) == {'quotaSizeInBytes'} for m, p, kw in self.native.calls
                            if m == 'PUT' and p.startswith('/admin/users/')))

    def test_existing_server_needs_real_owner_login_and_never_creates_users(self):
        self.native.initialized = True
        document = self.plan()
        self.assertEqual(document['action'], 'verify_existing_owner')
        self.apply(document)
        self.assertFalse(any(p == '/auth/admin-sign-up' for _, p, _ in self.native.calls))

    def test_tampered_and_stale_plans_cannot_mutate_native_settings(self):
        self.native.initialized = True
        document = self.plan()
        document['policy']['ffmpeg']['threads'] = 8
        with self.assertRaisesRegex(SafetyError, 'reviewed plan hash'):
            self.apply(document)
        document = self.plan()
        self.native.config['unrelated']['librarySchedule'] = 'changed-after-review'
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.apply(document)
        self.assertFalse(any(m in {'PUT', 'PATCH', 'DELETE'} for m, _, _ in self.native.calls))

    def test_interrupted_claim_is_not_replayed_against_empty_or_existing_accounts(self):
        self.reservation(state='claiming')
        with self.assertRaisesRegex(SafetyError, 'unconfirmed photo owner'):
            self.plan()
        self.native.initialized = True
        self.assertEqual(self.plan()['action'], 'verify_existing_owner')
        self.assertFalse(any(p == '/auth/admin-sign-up' for _, p, _ in self.native.calls))

    def test_foreign_host_or_replaced_directory_is_never_adopted(self):
        self.reservation(host_id='another-host')
        with self.assertRaisesRegex(SafetyError, 'another host or datastore'):
            self.plan()
        self.reservation(directory={'device': 1, 'inode': 99})
        with self.assertRaisesRegex(SafetyError, 'another host or datastore'):
            self.plan()

    def test_schema_drift_and_type_coercion_are_refused(self):
        self.native.initialized = True
        self.native.config['job']['thumbnailGeneration']['concurrency'] = '4'
        with self.assertRaisesRegex(SafetyError, 'field type changed'):
            self.plan()
        with self.assertRaisesRegex(SafetyError, 'fields differ'):
            photos.merge_policy({}, {'ffmpeg': {'threads': 1}})
        self.assertFalse(any(m == 'PUT' for m, _, _ in self.native.calls))

    def test_unknown_native_state_and_private_errors_fail_closed(self):
        with self.assertRaisesRegex(SafetyError, 'unknown'):
            photos.plan(self.c, request=lambda *a, **kw: {'isInitialized': 'false'})
        def private_failure(*a, **kw):
            raise RuntimeError('fixture-private-server-message')
        with self.assertRaises(SafetyError) as caught:
            photos.plan(self.c, request=private_failure)
        self.assertNotIn('fixture-private-server-message', str(caught.exception))

    def test_explicit_saved_input_correction_backs_up_input_without_password_reset(self):
        changed = {**CREDS, 'password': 'fixture-corrected-photo-password'}  # release-audit: synthetic - corrected saved input fixture
        with self.assertRaisesRegex(SafetyError, 'private-input correction'):
            photos.save_credentials(self.c, changed)
        photos.save_credentials(self.c, changed, replace=True)
        self.assertEqual(len(list((self.root/'backups').glob('photos-owner-input-*'))), 1)
        self.assertFalse(self.native.calls)

    def test_reservation_never_relabels_an_existing_project_as_fresh(self):
        with patch.object(engine, 'docker', return_value=SimpleNamespace(stdout='a'*12)), \
             patch.object(engine, 'compose_path', return_value=self.root/'compose.yaml'):
            value = photos.reserve_fresh(self.c)
        self.assertFalse(value['eligible'])
        self.assertEqual(value['state'], 'existing')

    def test_unconfirmed_claim_requires_a_distinct_new_review_and_same_private_input(self):
        self.reservation()
        original = self.plan()
        self.reservation(state='claiming', private_input_hash=original['private_input_hash'])
        retried = self.plan()
        self.assertEqual(retried['action'], 'retry_unconfirmed_first_owner')
        self.assertNotEqual(retried['review_hash'], original['review_hash'])
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.apply(original)
        self.assertTrue(self.apply(retried)['ok'])

    def test_changed_private_input_invalidates_review_before_any_signup(self):
        self.reservation()
        document = self.plan()
        photos.save_credentials(self.c, {**CREDS, 'name': 'Different owner'}, replace=True)
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.apply(document)
        self.assertFalse(any(p == '/auth/admin-sign-up' for _, p, _ in self.native.calls))


class GuidedPhotosTests(unittest.TestCase):
    def test_photos_selects_lean_mode_and_native_setup_without_browser_wizard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ui = setup.GuidedSetup(root, root/'site.json', input_fn=lambda _: '1', output_fn=lambda _: None,
                                   state_base=root/'progress', registry_root=root/'registry')
            c = default_config()
            c['features']['photos'] = True
            with patch.object(ui, 'config', return_value=c), patch.object(ui, '_save_configuration') as saved, \
                 patch.object(ui, 'install', return_value=True) as install, patch.object(ui, 'endpoints'), \
                 patch.object(ui, 'photo_owner', return_value=True) as owner, patch.object(ui, 'wire', return_value=True) as wire:
                self.assertTrue(ui.extras())
            self.assertFalse(c['extras']['photos']['machine_learning'])
            install.assert_called_once_with(c, 5)
            owner.assert_called_once_with(c)
            wire.assert_called_once_with(5)
            self.assertTrue(saved.called)


if __name__ == '__main__':
    unittest.main()
