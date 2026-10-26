"""Friendly labels preserve the actual managed resource identities."""
import copy
import unittest
from urllib.parse import urlsplit

from samurai_stack import integrations, naming
from test_catalog import config
from test_integrations import VendorModel as BaseVendorModel, secrets


class VendorModel(BaseVendorModel):
    """Seerr enforces its OpenAPI request schema before the route handler."""
    def __call__(self, method, url, headers=None, json=None, timeout=None, form=None):
        uri = urlsplit(url)
        base, _, identifier = uri.path.rpartition('/')
        if (method == 'PUT' and uri.port == 5055 and identifier.isdigit()
                and base in {'/api/v1/settings/radarr', '/api/v1/settings/sonarr'}):
            self.calls.append((method, url, copy.deepcopy(json), copy.deepcopy(form)))
            if not isinstance(json, dict) or 'id' in json:
                raise AssertionError('request/body/id is read-only')
            rows = self.collections[('seerr', base)]
            index = next(i for i, row in enumerate(rows) if row['id'] == int(identifier))
            saved = dict(copy.deepcopy(json), id=int(identifier))
            if not self.ignore_writes:
                if saved.get('isDefault'):
                    for row in rows:
                        if row.get('is4k') == saved.get('is4k'):
                            row['isDefault'] = False
                rows[index] = saved
            return copy.deepcopy(saved)
        return super().__call__(method, url, headers=headers, json=json, timeout=timeout, form=form)


class IntegrationDisplayNames(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.config['compose_projects'] = {'2': 'home-picturehouse', '3': 'home-courier', '4': 'home-control-room'}
        self.vendor = VendorModel()
        result = integrations.configure_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)
        self.assertTrue(result['ok'], result)

    def legacy(self):
        for (service, path), rows in self.vendor.collections.items():
            if path == '/api/v3/downloadclient':
                for row in rows:
                    kind = 'qBittorrent' if row['implementation'] == 'QBittorrent' else 'SABnzbd'
                    row['name'] = 'Samurai example ' + kind
            elif path == '/api/v1/applications':
                for row in rows:
                    row['name'] = 'Samurai example ' + row['implementation'].title()
            elif path == '/api/v3/notification':
                for row in rows:
                    row['name'] = 'Samurai example Jellyfin'
            elif service == 'seerr' and path in {'/api/v1/settings/sonarr', '/api/v1/settings/radarr'}:
                for row in rows:
                    row['name'] = 'Samurai example ' + path.rsplit('/', 1)[-1].title()

    def test_fresh_resources_put_the_app_before_the_actual_project(self):
        for service in ('sonarr', 'radarr'):
            self.assertEqual('SABnzbd | home-picturehouse', self.vendor.collections[(service, '/api/v3/downloadclient')][0]['name'])
            self.assertEqual('Jellyfin | home-picturehouse', self.vendor.collections[(service, '/api/v3/notification')][0]['name'])
            self.assertEqual(naming.service_label(self.config, service, 2),
                             self.vendor.collections[('seerr', '/api/v1/settings/' + service)][0]['name'])
        self.assertEqual({'Movies | home-picturehouse', 'TV | home-picturehouse'},
                         {row['Name'] for row in self.vendor.libraries})

    def test_legacy_verification_is_read_only_and_configure_renames_same_ids(self):
        self.legacy()
        # Unrelated manually configured resources and their settings survive.
        rows = self.vendor.collections[('radarr', '/api/v3/downloadclient')]
        custom = copy.deepcopy(rows[0])
        custom.update(id=800, name='My existing manual downloader', tags=[37], futureSetting={'retain': True})
        rows.append(custom)
        before = copy.deepcopy(self.vendor.collections)
        self.vendor.calls.clear()
        verified = integrations.verify_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)
        self.assertTrue(verified['ok'], verified)
        self.assertEqual(before, self.vendor.collections)
        self.assertFalse(any(method in {'PUT', 'DELETE'} for method, *_ in self.vendor.calls))
        result = integrations.configure_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)
        self.assertTrue(result['ok'], result)
        for key, previous in before.items():
            actual = self.vendor.collections[key]
            self.assertEqual(len(previous), len(actual))
            for old, new in zip(previous, actual):
                self.assertEqual({k:v for k,v in old.items() if k != 'name'},
                                 {k:v for k,v in new.items() if k != 'name'})
                if old.get('name', '').startswith('Samurai example '):
                    self.assertNotEqual(old['name'], new['name'])
        self.assertEqual(custom, self.vendor.collections[('radarr', '/api/v3/downloadclient')][-1])
        self.vendor.calls.clear()
        self.assertTrue(integrations.configure_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)['ok'])
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_torrent_client_label_uses_its_separate_project(self):
        first = integrations.configure_stage(self.config, 3, request=self.vendor, secret_resolver=secrets)
        self.assertTrue(first['ok'], first)
        self.legacy()
        original = copy.deepcopy(self.vendor.collections[('sonarr', '/api/v3/downloadclient')])
        result = integrations.configure_stage(self.config, 3, request=self.vendor, secret_resolver=secrets)
        self.assertTrue(result['ok'], result)
        rows = self.vendor.collections[('sonarr', '/api/v3/downloadclient')]
        torrent = next(row for row in rows if row['implementation'] == 'QBittorrent')
        old = next(row for row in original if row['implementation'] == 'QBittorrent')
        self.assertEqual(torrent['name'], 'qBittorrent | home-courier')
        self.assertEqual(torrent['id'], old['id'])
        self.assertEqual(torrent['fields'], old['fields'])

    def test_duplicate_old_and_new_labels_refuse_before_any_label_write(self):
        row = self.vendor.collections[('radarr', '/api/v3/downloadclient')][0]
        legacy = copy.deepcopy(row)
        legacy.update(id=12, name='Samurai example SABnzbd')
        self.vendor.collections[('radarr', '/api/v3/downloadclient')].append(legacy)
        before = copy.deepcopy(self.vendor.collections)
        self.vendor.calls.clear()
        result = integrations.configure_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.vendor.collections)
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_legacy_label_with_wrong_implementation_is_not_adopted(self):
        row = self.vendor.collections[('radarr', '/api/v3/downloadclient')][0]
        row.update(name='Samurai example SABnzbd', implementation='UnrelatedProvider')
        before = copy.deepcopy(row)
        self.vendor.calls.clear()
        result = integrations.configure_stage(self.config, 2, request=self.vendor, secret_resolver=secrets)
        self.assertFalse(result['ok'])
        self.assertEqual(row, before)
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_resource_change_during_review_refuses_put(self):
        self.legacy()
        count = 0
        target = '/api/v3/downloadclient'
        def transport(method, url, **kwargs):
            nonlocal count
            if method == 'GET' and urlsplit(url).port == 7878 and urlsplit(url).path == target:
                count += 1
                if count == 2:
                    self.vendor.collections[('radarr', target)][0]['priority'] = 99
            return self.vendor(method, url, **kwargs)
        run = integrations._Run(self.config, 2, transport, secrets, True)
        self.vendor.calls.clear()
        with self.assertRaisesRegex(integrations.Conflict, 'changed during label review'):
            integrations._download_client(run, 'radarr', 'movies')
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_seerr_zero_id_is_preserved(self):
        self.legacy()
        row = self.vendor.collections[('seerr', '/api/v1/settings/radarr')][0]
        row['id'] = 0
        run = integrations._Run(self.config, 2, self.vendor, secrets, True)
        integrations._seerr(run, 'radarr')
        saved = self.vendor.collections[('seerr', '/api/v1/settings/radarr')][0]
        self.assertEqual(saved['id'], 0)
        self.assertEqual(saved['name'], 'Radarr | home-picturehouse')

    def test_seerr_duplicate_same_type_default_blocks_rename(self):
        self.legacy()
        rows = self.vendor.collections[('seerr', '/api/v1/settings/radarr')]
        custom = copy.deepcopy(rows[0])
        custom.update(id=18, name='Custom same-type default')
        rows.append(custom)
        before = copy.deepcopy(rows)
        run = integrations._Run(self.config, 2, self.vendor, secrets, True)
        self.vendor.calls.clear()
        with self.assertRaisesRegex(integrations.Conflict, 'ambiguous same-type defaults'):
            integrations._seerr(run, 'radarr')
        self.assertEqual(before, rows)
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_new_seerr_connection_prefers_friendly_profile_without_reassigning_existing(self):
        vendor = VendorModel()
        def transport(method, url, **kwargs):
            response = vendor(method, url, **kwargs)
            if urlsplit(url).port == 5055 and urlsplit(url).path.endswith('/test'):
                response['profiles'].extend([
                    {'id': 41, 'name': 'Samurai example 1080p Original'},
                    {'id': 42, 'name': '1080p Original | home-picturehouse'},
                ])
            return response
        result = integrations.configure_stage(self.config, 2, request=transport, secret_resolver=secrets)
        self.assertTrue(result['ok'], result)
        for service in ('sonarr', 'radarr'):
            row = vendor.collections[('seerr', '/api/v1/settings/' + service)][0]
            self.assertEqual(42, row['activeProfileId'])
            row.update(activeProfileId=1, activeProfileName='HD-1080p')
        result = integrations.configure_stage(self.config, 2, request=transport, secret_resolver=secrets)
        self.assertTrue(result['ok'], result)
        for service in ('sonarr', 'radarr'):
            self.assertEqual(1, vendor.collections[('seerr', '/api/v1/settings/' + service)][0]['activeProfileId'])

    def profile_transport(self, service, profiles=None):
        def transport(method, url, **kwargs):
            response = self.vendor(method, url, **kwargs)
            if urlsplit(url).port == 5055 and urlsplit(url).path == '/api/v1/settings/' + service + '/test':
                response['profiles'] = profiles if profiles is not None else [
                    {'id': 7, 'name': '1080p Original | home-picturehouse'}]
            return response
        return transport

    def stale_profile_row(self, service, *, legacy=False):
        row = self.vendor.collections[('seerr', '/api/v1/settings/' + service)][0]
        row.update(id=0, activeProfileId=7, activeProfileName='Samurai example 1080p Original',
                   tags=[12], overrideRule=[{'key': 'preserved'}], futureSetting={'enabled': False})
        if legacy:
            row['name'] = 'Samurai example ' + service.title()
        return row

    def test_seerr_cache_refresh_changes_only_name_of_the_same_selected_profile(self):
        for service in ('radarr', 'sonarr'):
            with self.subTest(service=service):
                row = self.stale_profile_row(service)
                rows = self.vendor.collections[('seerr', '/api/v1/settings/' + service)]
                custom = copy.deepcopy(row)
                custom.update(id=18, name='My other server', isDefault=False)
                rows.append(custom)
                before = copy.deepcopy(rows)
                transport = self.profile_transport(service)
                self.vendor.calls.clear()
                verify = integrations._Run(self.config, 2, transport, secrets, False)
                self.assertIn('display cache is stale', integrations._seerr(verify, service))
                self.assertEqual(before, rows)
                self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))
                run = integrations._Run(self.config, 2, transport, secrets, True)
                integrations._seerr(run, service)
                expected = copy.deepcopy(before)
                expected[0]['activeProfileName'] = '1080p Original | home-picturehouse'
                self.assertEqual(expected, rows)
                puts = [(url, body) for method, url, body, _ in self.vendor.calls if method == 'PUT']
                self.assertEqual(1, len(puts))
                self.assertTrue(puts[0][0].endswith('/settings/' + service + '/0'))
                self.assertEqual(7, puts[0][1]['activeProfileId'])
                self.vendor.calls.clear()
                integrations._seerr(run, service)
                self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_seerr_combines_server_label_and_profile_cache_refresh_in_one_put(self):
        row = self.stale_profile_row('radarr', legacy=True)
        before = copy.deepcopy(row)
        run = integrations._Run(self.config, 2, self.profile_transport('radarr'), secrets, True)
        self.vendor.calls.clear()
        integrations._seerr(run, 'radarr')
        expected = dict(before, name='Radarr | home-picturehouse', activeProfileName='1080p Original | home-picturehouse')
        self.assertEqual(expected, self.vendor.collections[('seerr', '/api/v1/settings/radarr')][0])
        self.assertEqual(1, sum(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_seerr_put_omits_readonly_body_id_but_retains_url_and_saved_zero_id(self):
        for service in ('radarr', 'sonarr'):
            with self.subTest(service=service):
                before = copy.deepcopy(self.stale_profile_row(service, legacy=True))
                run = integrations._Run(self.config, 2, self.profile_transport(service), secrets, True)
                self.vendor.calls.clear()
                integrations._seerr(run, service)
                puts = [(url, body) for method, url, body, _ in self.vendor.calls if method == 'PUT']
                self.assertEqual(1, len(puts))
                self.assertTrue(puts[0][0].endswith('/settings/' + service + '/0'))
                self.assertNotIn('id', puts[0][1])
                after = self.vendor.collections[('seerr', '/api/v1/settings/' + service)][0]
                self.assertEqual(0, after['id'])
                self.assertEqual({key: value for key, value in before.items() if key not in {'name', 'activeProfileName'}},
                                 {key: value for key, value in after.items() if key not in {'name', 'activeProfileName'}})

    def test_seerr_cache_uses_the_selected_custom_profile_without_reassigning_it(self):
        self.stale_profile_row('radarr')
        transport = self.profile_transport('radarr', [{'id': 7, 'name': 'My renamed custom profile'},
                                                    {'id': 99, 'name': '1080p Original | home-picturehouse'}])
        run = integrations._Run(self.config, 2, transport, secrets, True)
        integrations._seerr(run, 'radarr')
        actual = self.vendor.collections[('seerr', '/api/v1/settings/radarr')][0]
        self.assertEqual(7, actual['activeProfileId'])
        self.assertEqual('My renamed custom profile', actual['activeProfileName'])

    def test_seerr_ambiguous_selected_profile_metadata_refuses_cache_write(self):
        for profiles in ([{'id': 7, 'name': 'First'}, {'id': 7, 'name': 'Second'}],
                         [{'id': 7, 'name': ''}], [{'id': 7, 'name': None}]):
            with self.subTest(profiles=profiles):
                self.stale_profile_row('radarr')
                before = copy.deepcopy(self.vendor.collections)
                run = integrations._Run(self.config, 2, self.profile_transport('radarr', profiles), secrets, True)
                self.vendor.calls.clear()
                with self.assertRaisesRegex(integrations.Conflict, 'ambiguous display metadata'):
                    integrations._seerr(run, 'radarr')
                self.assertEqual(before, self.vendor.collections)
                self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_seerr_cache_refresh_requires_saved_readback_and_preserves_default_policy(self):
        self.stale_profile_row('radarr')
        run = integrations._Run(self.config, 2, self.profile_transport('radarr'), secrets, True)
        self.vendor.ignore_writes = True
        with self.assertRaisesRegex(integrations.Conflict, 'readback changed identity or settings'):
            integrations._seerr(run, 'radarr')
        self.vendor.ignore_writes = False
        rows = self.vendor.collections[('seerr', '/api/v1/settings/radarr')]
        other = copy.deepcopy(rows[0])
        other.update(id=18, name='Custom same-type default')
        rows.append(other)
        before = copy.deepcopy(rows)
        self.vendor.calls.clear()
        with self.assertRaisesRegex(integrations.Conflict, 'ambiguous same-type defaults'):
            integrations._seerr(run, 'radarr')
        self.assertEqual(before, rows)
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))

    def test_seerr_cache_refresh_refuses_changed_server_before_put(self):
        self.stale_profile_row('radarr')
        transport = self.profile_transport('radarr')
        count = 0
        def changing_transport(method, url, **kwargs):
            nonlocal count
            if method == 'GET' and urlsplit(url).port == 5055 and urlsplit(url).path == '/api/v1/settings/radarr':
                count += 1
                if count == 2:
                    self.vendor.collections[('seerr', '/api/v1/settings/radarr')][0]['tags'] = [93]
            return transport(method, url, **kwargs)
        run = integrations._Run(self.config, 2, changing_transport, secrets, True)
        self.vendor.calls.clear()
        with self.assertRaisesRegex(integrations.Conflict, 'changed during label review'):
            integrations._seerr(run, 'radarr')
        self.assertFalse(any(method == 'PUT' for method, *_ in self.vendor.calls))


if __name__ == '__main__':
    unittest.main()
