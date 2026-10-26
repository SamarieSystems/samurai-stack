"""Photo enrollment target attestation uses synthetic Docker and bind identities."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import catalog, engine, extras, naming, onboarding, photos_setup as photos
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest


class PhotoTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config()
        self.c['install_root'] = str(self.root / 'apps')
        self.c['storage']['data_root'] = str(self.root / 'media')
        self.c['features']['photos'] = True
        self.c['extras'] = {'photos': {'machine_learning': False}}
        self.upload = Path(self.c['storage']['data_root']) / 'photos/upload'
        self.database = Path(self.c['install_root']) / 'appdata/immich-postgres'
        for path in (self.upload, self.database):
            path.mkdir(parents=True)
        self.server_id, self.db_id = 'a' * 64, 'b' * 64
        self.model = catalog.render_stage(self.c, 5)
        self.project = naming.project_name(self.c, 5)
        self.images = {}
        self.rows = {}
        for service, cid, image_id, pid, directory, destination in (
                ('immich-server', self.server_id, 'sha256:' + 'c' * 64, 1701, self.upload, '/data'),
                ('database', self.db_id, 'sha256:' + 'd' * 64, 1702, self.database, '/var/lib/postgresql/data')):
            expected = self.model['services'][service]
            image_env = ['PATH=/usr/bin']
            if service == 'database':
                image_env.append('PGDATA=/var/lib/postgresql/data')
            image_config = {'Cmd': ['postgres'] if service == 'database' else ['start.sh'],
                            'Entrypoint': ['fixture-entrypoint'], 'WorkingDir': '', 'Env': image_env}
            self.images[expected['image']] = {'Id': image_id, 'Config': deepcopy(image_config)}
            self.rows[cid] = {
                'Id': cid, 'Image': image_id, 'State': {'Pid': pid},
                'Config': {**deepcopy(image_config), 'Image': expected['image'],
                           'Env': image_env + [key + '=' + value for key, value in expected['environment'].items()],
                           'Labels': {'systems.samarie.deployment': self.c['deployment_id'],
                                      'systems.samarie.stage': '5', 'com.docker.compose.project': self.project,
                                      'com.docker.compose.service': service}},
                'Mounts': [{'Destination': destination, 'Source': str(directory), 'Type': 'bind', 'RW': True}],
                'NetworkSettings': {'Ports': {'2283/tcp': [{'HostIp': self.c['bind_address'], 'HostPort': '2283'}]} if service == 'immich-server' else {},
                                    'Networks': {self.project + '_default': {'Aliases': [service]}}}}
        self.native_stat = os.stat
        self.live = {'/proc/1701/root/data': self.upload.stat(),
                     '/proc/1702/root/var/lib/postgresql/data': self.database.stat()}
        self.docker_calls = []
        self.enterContext(patch.object(photos, '_paths', return_value=(Path(self.c['install_root']), self.root / 'receipt.json')))
        self.preflight = self.enterContext(patch.object(engine, 'preflight'))
        self.space = self.enterContext(patch.object(extras, 'free_space'))
        self.enterContext(patch.object(engine, 'recipe', side_effect=lambda *_: deepcopy(self.model)))
        self.enterContext(patch.object(engine, 'docker', side_effect=self.docker))
        self.enterContext(patch.object(onboarding, '_identity', side_effect=self.identity))
        self.enterContext(patch.object(photos.os, 'stat', side_effect=self.stat))
        self.http = self.enterContext(patch.object(engine, 'request', side_effect=AssertionError('Target inspection must not send credentials')))

    def identity(self, path, **_):
        info = self.stat(path)
        return {'device': info.st_dev, 'inode': info.st_ino}

    def stat(self, path, *args, **kwargs):
        return self.live[str(path)] if str(path) in self.live else self.native_stat(path, *args, **kwargs)

    def docker(self, runner, *args):
        self.docker_calls.append(args)
        if args[0] == 'ps':
            service = next(value.split('=', 2)[-1] for value in args if value.startswith('label=com.docker.compose.service='))
            return SimpleNamespace(stdout=(self.server_id if service == 'immich-server' else self.db_id) + '\n')
        if args[0] == 'inspect':
            return SimpleNamespace(stdout=json.dumps([self.rows[args[1]]]))
        if args[:2] == ('image', 'inspect'):
            return SimpleNamespace(stdout=json.dumps([self.images[args[2]]]))
        raise AssertionError(args)

    def check(self):
        return photos.check_target(self.c, runner=lambda *_: None)

    def test_exact_owned_server_and_reserved_database_pass_without_http(self):
        result = self.check()
        self.assertEqual(result['container'], self.server_id)
        self.assertEqual(result['database'], self.db_id)
        self.assertEqual(result['directory'], self.identity(self.database))
        self.space.assert_called_once_with(self.c, 'photos')
        self.http.assert_not_called()

    def test_command_entrypoint_working_directory_and_actual_image_overrides_refused(self):
        for field in ('Cmd', 'Entrypoint', 'WorkingDir', 'Image'):
            with self.subTest(field=field):
                original = deepcopy(self.rows[self.server_id])
                if field == 'Image':
                    self.rows[self.server_id]['Image'] = 'sha256:' + 'f' * 64
                else:
                    self.rows[self.server_id]['Config'][field] = 'override' if field == 'WorkingDir' else ['override']
                with self.assertRaisesRegex(SafetyError, 'command overrides'):
                    self.check()
                self.rows[self.server_id] = original

    def test_postgres_unset_and_empty_working_directory_mean_root_but_tmp_is_refused(self):
        image = self.images[self.model['services']['database']['image']]
        image['Config']['WorkingDir'] = None
        self.rows[self.db_id]['Config']['WorkingDir'] = ''
        self.assertEqual(self.check()['database'], self.db_id)
        self.rows[self.db_id]['Config']['WorkingDir'] = '/tmp'
        with self.assertRaisesRegex(SafetyError, 'database image, identity or datastore'):
            self.check()

    def test_foreign_ownership_public_listener_and_wrong_originals_mount_refused(self):
        original = deepcopy(self.rows[self.server_id])
        changes = (
            lambda row: row['Config']['Labels'].update({'systems.samarie.deployment': 'foreign-deployment'}),
            lambda row: row['NetworkSettings']['Ports']['2283/tcp'][0].update(HostIp='0.0.0.0'),
            lambda row: row['Mounts'][0].update(Source=str(self.root / 'other-originals')),
            lambda row: row['Mounts'][0].update(RW=False))
        for change in changes:
            self.rows[self.server_id] = deepcopy(original)
            change(self.rows[self.server_id])
            with self.assertRaisesRegex(SafetyError, 'ownership, private port or originals mount'):
                self.check()

    def test_source_and_nested_originals_overlays_refused(self):
        for destination in ('/app', '/app/code.js', '/usr/src/app', '/data/upload'):
            with self.subTest(destination=destination):
                self.rows[self.server_id]['Mounts'].append({'Destination': destination, 'Source': '/fixture/override', 'Type': 'bind', 'RW': True})
                with self.assertRaisesRegex(SafetyError, 'originals mount'):
                    self.check()
                self.rows[self.server_id]['Mounts'].pop()

    def test_server_database_redirect_and_extra_db_url_refused(self):
        original = self.rows[self.server_id]['Config']['Env'][:]
        for modified in ([v for v in original if not v.startswith('DB_HOSTNAME=')] + ['DB_HOSTNAME=other-database'],
                         original + ['DB_URL=postgresql://fixture.invalid/unrelated']):
            self.rows[self.server_id]['Config']['Env'] = modified
            with self.assertRaisesRegex(SafetyError, 'originals mount'):
                self.check()

    def test_database_command_identity_and_source_overrides_refused(self):
        original = deepcopy(self.rows[self.db_id])
        changes = (
            lambda row: row['Config']['Labels'].update({'systems.samarie.deployment': 'foreign-deployment'}),
            lambda row: row['Config'].update(Cmd=['postgres', '-D', '/tmp/other-database']),
            lambda row: row['Mounts'][0].update(Source=str(self.root / 'unreserved-database')),
            lambda row: row['Mounts'].append({'Destination': '/var/lib/postgresql/data/pgdata', 'Type': 'volume', 'Source': 'other', 'RW': True}))
        for change in changes:
            self.rows[self.db_id] = deepcopy(original)
            change(self.rows[self.db_id])
            with self.assertRaisesRegex(SafetyError, 'database image, identity or datastore'):
                self.check()

    def test_database_pgdata_and_postgres_environment_override_refused(self):
        original = self.rows[self.db_id]['Config']['Env'][:]
        for key, value in (('PGDATA', '/tmp/unreserved-pgdata'), ('POSTGRES_DB', 'other_database'), ('POSTGRES_USER', 'other_user')):
            with self.subTest(key=key):
                self.rows[self.db_id]['Config']['Env'] = [v for v in original if not v.startswith(key + '=')] + [key + '=' + value]
                with self.assertRaisesRegex(SafetyError, 'database image, identity or datastore'):
                    self.check()

    def test_disconnected_database_and_missing_shared_network_alias_refused(self):
        original = deepcopy(self.rows[self.db_id]['NetworkSettings']['Networks'])
        for networks in ({'other-network': {'Aliases': ['database']}},
                         {self.project + '_default': {'Aliases': ['other-alias']}}):
            self.rows[self.db_id]['NetworkSettings']['Networks'] = networks
            with self.assertRaisesRegex(SafetyError, 'database image, identity or datastore'):
                self.check()
        self.rows[self.db_id]['NetworkSettings']['Networks'] = original

    @unittest.skipUnless(os.name == 'posix', 'Linux live-bind proof uses /proc')
    def test_deleted_and_recreated_live_bind_is_refused_for_originals_and_database(self):
        for path in list(self.live):
            with self.subTest(path=path):
                original = self.live[path]
                values = list(original); values[1] += 1
                self.live[path] = os.stat_result(values)
                with self.assertRaisesRegex(SafetyError, 'replaced directory'):
                    self.check()
                self.live[path] = original


class PhotoPrivatePlanBindingTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config()
        self.creds = {'email': 'fixture-owner@example.invalid', 'name': 'Fixture Owner', 'password': 'fixture-private-photo-password'}  # release-audit: synthetic - private plan binding fixture
        self.receipt = {'eligible': True, 'state': 'reserved'}
        self.enterContext(patch.object(photos, 'check_target', return_value={'container': 'a' * 64, 'database': 'b' * 64}))
        self.enterContext(patch.object(photos, '_binding', return_value={'deployment_id': self.c['deployment_id'], 'host_id': 'fixture-host'}))
        self.enterContext(patch.object(photos, '_receipt', side_effect=lambda _: deepcopy(self.receipt)))
        self.credentials = self.enterContext(patch.object(photos, '_credentials', side_effect=lambda _: deepcopy(self.creds)))
        self.call = self.enterContext(patch.object(photos, '_call', return_value={'isInitialized': False, 'isOnboarded': False, 'maintenanceMode': False}))
        self.enterContext(patch.object(engine, 'deployment_lock', side_effect=lambda _: nullcontext()))

    def test_review_hash_changes_when_any_private_owner_field_changes_without_disclosing_it(self):
        before = photos.plan(self.c)
        self.assertNotIn(self.creds['password'], json.dumps(before))
        for key in self.creds:
            with self.subTest(field=key):
                original = self.creds[key]
                self.creds[key] = 'changed-' + original
                after = photos.plan(self.c)
                self.assertNotEqual(before['private_input_hash'], after['private_input_hash'])
                self.assertNotEqual(before['review_hash'], after['review_hash'])
                self.creds[key] = original

    def test_changed_private_input_refuses_signup_before_any_native_write(self):
        document = photos.plan(self.c)
        self.creds['password'] = 'fixture-private-corrected-password'  # release-audit: synthetic - changed plan input fixture
        self.call.reset_mock()
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            photos.apply(self.c, document, document['review_hash'])
        self.assertTrue(all(call.args[2] == 'GET' for call in self.call.call_args_list))

    def test_unconfirmed_empty_claim_can_retry_only_with_the_same_private_input(self):
        self.receipt.update(state='claiming', private_input_hash=digest(self.creds))
        self.assertEqual(photos.plan(self.c)['action'], 'retry_unconfirmed_first_owner')
        self.creds['name'] = 'Different Fixture Owner'
        with self.assertRaisesRegex(SafetyError, 'different input'):
            photos.plan(self.c)

    def test_private_input_rotation_during_claim_cannot_change_signup_or_session(self):
        reviewed_credentials = deepcopy(self.creds)
        document = photos.plan(self.c)
        owner = '9562898f-03b4-4593-9f68-679cf4238656'
        policy = deepcopy(document['policy'])
        signups = []
        state = {'initialized': False, 'onboarded': False}

        def save(path, value, **_):
            if value.get('state') == 'claiming':
                self.receipt = deepcopy(value)
                self.creds.update(email='changed-owner@example.invalid', name='Different Fixture Owner',
                                  password='fixture-rotated-private-password')  # release-audit: synthetic - concurrent saved-input rotation

        def request(c, call, method, path, **kwargs):
            if (method, path) == ('GET', '/server/config'):
                return {'isInitialized': state['initialized'], 'isOnboarded': state['onboarded'], 'maintenanceMode': False}
            if (method, path) == ('POST', '/auth/admin-sign-up'):
                signups.append(deepcopy(kwargs['json']))
                state['initialized'] = True
                return {'id': owner}
            if (method, path) == ('GET', '/admin/config'):
                return deepcopy(policy)
            if (method, path) == ('POST', '/system-metadata/admin-onboarding'):
                state['onboarded'] = True
                return None
            raise AssertionError((method, path))

        @contextmanager
        def session(c, request, credentials=None):
            self.assertEqual(credentials, reviewed_credentials)
            yield {}, owner

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(photos, '_paths', return_value=(root, root / 'receipt.json')), \
                 patch.object(photos, 'write_json', side_effect=save), \
                 patch.object(photos, '_call', side_effect=request), \
                 patch.object(photos, '_session', side_effect=session), \
                 patch.object(photos, '_owner', return_value={'id': owner, 'quotaSizeInBytes': document['owner_quota_bytes']}):
                self.assertTrue(photos.apply(self.c, document, document['review_hash'])['ok'])
        self.assertEqual(signups, [reviewed_credentials])
        self.assertNotEqual(self.creds, reviewed_credentials)
        self.assertEqual(digest(signups[0]), document['private_input_hash'])


if __name__ == '__main__':
    unittest.main()
