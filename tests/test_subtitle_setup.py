"""Bazarr future-only policy, destructive collection semantics and private input."""
import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from samurai_stack import engine, onboarding, subtitle_setup as subtitles
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest, write_json

KEY = "fixture-subtitle-api-key"  # release-audit: synthetic - isolated Bazarr credential


def pinned_indexer_languages(profile, audio_languages=()):
    """Independent stored-item contract from pinned Bazarr 1.6.2 indexer.

    bazarr/subtitles/indexer/movies.py:261-270 and series.py:270-279.
    The frontend uses camelCase in memory but snakeCaseKeys on submitted items.
    This consumer is intentionally independent of the installer's profile builder.
    """
    result = []
    for language in profile['items']:
        if language['audio_exclude'] == 'True':
            if language['language'] in audio_languages:
                continue
        if language['audio_only_include'] == 'True':
            if language['language'] not in audio_languages:
                continue
        result.append({'language': language['language'], 'forced': str(language['forced']), 'hi': str(language['hi'])})
    return result


class Vendor:
    def __init__(self):
        self.general = {**copy.deepcopy(subtitles.GENERAL), "enabled_providers": [],
                        "serie_default_profile": "", "movie_default_profile": "",
                        "serie_default_enabled": False, "movie_default_enabled": False,
                        "concurrent_jobs": 3, "multithreading": True}
        self.profiles = []
        self.languages = [{"name": "English", "code2": "en", "code3": "eng", "enabled": False},
                          {"name": "French", "code2": "fr", "code3": "fra", "enabled": True}]
        self.other_settings = {"auth": {"username": "existing-admin"}, "sonarr": {"ip": "sonarr"}}
        self.calls, self.version, self.provider_state = [], "1.6.2", "Good"
        self.ignore = None
        self.lost = False
        self.race = False
        self.snapshots = 0

    def __call__(self, method, url, **options):
        path = urlsplit(url).path
        self.calls.append((method, path, options))
        assert options["headers"] == {"X-API-KEY": KEY}
        if method == "GET":
            if path == "/api/system/status":return {"data": {"bazarr_version": self.version}}
            if path == "/api/system/settings":
                self.snapshots += 1
                if self.race and self.snapshots == 3:self.general["concurrent_jobs"] = 9
                return copy.deepcopy({"general": self.general, **self.other_settings})
            if path == "/api/system/languages/profiles":return copy.deepcopy(self.profiles)
            if path == "/api/system/languages":return copy.deepcopy(self.languages)
            if path == "/api/providers":
                return {"data": [{"name": name, "status": self.provider_state} for name in self.general["enabled_providers"]]}
        assert (method, path) == ("POST", "/api/system/settings"), (method, path)
        form = options["form"]
        profiles = [v for k, v in form if k == "languages-profiles"]
        if profiles and self.ignore != "profiles":
            self.profiles = json.loads(profiles[0])
            # Profile JSON is opaque to the save route. Assignment/indexing then
            # consumes its snake_case fields; readback alone cannot validate it.
            for profile in self.profiles:pinned_indexer_languages(profile)
        enabled = [v for k, v in form if k == "languages-enabled"]
        if enabled and self.ignore != "languages":
            for row in self.languages:row["enabled"] = row["code2"] in enabled
        for key, value in form:
            if key.startswith('subzero-'):
                mod = key[len('subzero-'):]
                if mod == 'color':self.general['subzero_mods'] = [m for m in self.general['subzero_mods'] if not m.startswith('color')]
                else:self.general['subzero_mods'] = [m for m in self.general['subzero_mods'] if m != mod]
                continue
            if not key.startswith("settings-"):continue
            _, section, name = key.split("-", 2)
            if name == self.ignore:continue
            if value in {"true", "false"}:value = value == "true"
            elif name in {"concurrent_jobs", "serie_default_profile", "movie_default_profile",
                          "wanted_search_frequency", "wanted_search_frequency_movie"}:value = int(value)
            if name in {"enabled_providers", "subzero_mods"}:
                values = [v for k, v in form if k == key]
                value = [v for v in values if v]
            target = self.general if section == "general" else self.other_settings.setdefault(section, {})
            target[name] = value
        if self.ignore == "other-profile" and self.profiles:self.profiles[0]["name"] = "Changed by operator"
        if self.lost:
            self.lost = False
            raise RuntimeError(KEY)


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c = default_config()
        self.c["install_root"] = str(self.root)
        self.c["storage"]["data_root"] = str(self.root / "media")
        for name in ("state", "secrets", "appdata", "appdata/bazarr", "media"):
            (self.root / name).mkdir(mode=0o700)
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        self.enterContext(patch.object(onboarding, "_guard"))
        self.enterContext(patch.object(onboarding, "_root_owned", return_value=True))
        self.enterContext(patch.object(engine, "host_identity", return_value="fixture-host"))
        self.target = {"container": "a" * 64, "image": "sha256:" + "b" * 64,
                       "providers": {name: "c" * 64 for name in subtitles.DEFAULT_PROVIDERS},
                       "directory": {"device": 1, "inode": 2}}
        self.target_mock = self.enterContext(patch.object(subtitles, "_target", return_value=self.target))
        self.enterContext(patch.object(subtitles.storage, "verify"))
        self.space = self.enterContext(patch.object(subtitles.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(200*subtitles.GIB, 50*subtitles.GIB, 150*subtitles.GIB)))
        self.vendor = Vendor()

    def options(self):return {"runner": lambda *a, **k:None, "request": self.vendor, "secret_resolver": lambda name:KEY}
    def plan(self, providers=subtitles.DEFAULT_PROVIDERS):return subtitles.plan(self.c, providers, **self.options())
    def apply(self, document=None):return subtitles.apply(self.c, document or self.plan(), **self.options())
    def writes(self):return [v for v in self.vendor.calls if v[0] != "GET"]
    def receipt(self):return json.loads((self.root / "state/subtitle-setup.json").read_text())

    def test_plan_is_readonly_hash_bound_and_secret_free(self):
        preview = self.plan()
        self.assertEqual(preview["scope"], "future_additions_only")
        self.assertEqual(preview["existing_media_assigned"], 0)
        self.assertNotIn(KEY, json.dumps(preview))
        self.assertFalse(self.writes())
        self.assertFalse((self.root / "state/subtitle-setup.json").exists())

    def test_fresh_default_is_tv_only_and_discloses_missing_movie_provider(self):
        self.assertEqual(subtitles.DEFAULT_PROVIDERS, ('gestdown',))
        self.assertFalse(self.plan()['movie_provider_configured'])
        result = self.apply()
        self.assertEqual(self.vendor.general['enabled_providers'], ['gestdown'])
        self.assertFalse(result['movie_provider_configured'])
        self.assertTrue(any('No movie subtitle provider' in n for n in result['notifications']))

    def test_omitted_choices_keep_saved_opensubtitles_instead_of_reverting_to_default(self):
        supplied = {'username': 'fixture-provider-user', 'password': 'fixture-provider-private'}  # release-audit: synthetic - subtitle account fixture
        subtitles.save_provider_credentials(self.c, 'opensubtitlescom', supplied)
        self.apply(self.plan(['gestdown', 'opensubtitlescom']))
        self.vendor.calls.clear()
        preview = subtitles.plan(self.c, **self.options())
        self.assertEqual(preview['providers'], ['gestdown', 'opensubtitlescom'])
        self.assertNotIn('enabled_providers', preview['settings'])
        self.assertFalse(self.writes())
        self.apply(preview)
        self.assertFalse(self.writes(), 'An unchanged provider login must not reset native auth state')

    def test_legacy_yify_is_preserved_only_with_explicit_choice_and_warned(self):
        result = self.apply(self.plan(['gestdown', 'yifysubtitles']))
        self.assertTrue(result['movie_provider_configured'])
        self.assertTrue(any('legacy provider' in n for n in result['notifications']))
        self.assertEqual(subtitles.plan(self.c, **self.options())['providers'], ['gestdown', 'yifysubtitles'])

    def migration_fixture(self, *, selected=True):
        previous = copy.deepcopy(self.c)
        supplied = {'username': 'fixture-provider-user', 'password': 'fixture-provider-private'}  # release-audit: synthetic - subtitle binding migration fixture
        subtitles.save_provider_credentials(self.c, 'opensubtitlescom', supplied)
        self.apply(self.plan(['gestdown', 'opensubtitlescom'] if selected else ['gestdown']))
        paths = [self.root / 'state/subtitle-setup.json', self.root / 'secrets/subtitle-provider-opensubtitlescom.json']
        for path in paths:
            value = json.loads(path.read_text())
            value.pop('binding_scope')
            value['config_digest'] = digest(previous)
            write_json(path, value, replace=True)
        originals = {str(path): path.read_bytes() for path in paths}
        (self.root / 'backups').mkdir(mode=0o700)
        self.c['features'].update(photos=True, music=True)
        self.c['extras'] = {'photos': {'machine_learning': False}, 'music': {'soulseek_mode': 'offline'}}
        self.enterContext(patch.object(subtitles, 'validate_config'))  # Synthetic Windows/temp paths are not deployable sites.
        self.migration_lock = self.enterContext(patch.object(engine, 'deployment_lock', side_effect=lambda _: nullcontext()))
        self.vendor.calls.clear()
        return previous, supplied, paths, originals

    def migration_plan(self, previous):
        return subtitles.binding_migration_plan(self.c, previous, **self.options())

    def migrate(self, previous, document):
        return subtitles.apply_binding_migration(self.c, previous, document, **self.options())

    def test_scoped_binding_survives_photo_and_music_choices_without_migration(self):
        supplied = {'username': 'fixture-provider-user', 'password': 'fixture-provider-private'}  # release-audit: synthetic - stage-specific credential fixture
        subtitles.save_provider_credentials(self.c, 'opensubtitlescom', supplied)
        self.apply(self.plan(['gestdown', 'opensubtitlescom']))
        self.c['features'].update(photos=True, music=True)
        self.c['extras'] = {'photos': {'machine_learning': False}}
        self.vendor.calls.clear()
        self.assertTrue(subtitles.verify(self.c, **self.options())['ok'])
        self.assertFalse(self.writes())

    def test_scoped_binding_rejects_endpoint_storage_uid_and_host_changes(self):
        binding = subtitles._binding(self.c)
        for section, key, value in ((None, 'bind_address', '192.168.50.99'), (None, 'install_root', '/different-apps'),
                                    ('hosts', 'media', '192.168.50.99'), ('storage', 'data_root', '/different-media'),
                                    (None, 'puid', self.c['puid'] + 1), (None, 'pgid', self.c['pgid'] + 1)):
            changed = copy.deepcopy(self.c)
            (changed if section is None else changed[section])[key] = value
            self.assertFalse(subtitles._matches_binding(changed, binding), (section, key))
        with patch.object(engine, 'host_identity', return_value='other-host'):
            self.assertFalse(subtitles._matches_binding(self.c, binding))
        self.assertFalse(subtitles._matches_binding(self.c, {**binding, 'binding_scope': 'unknown-future-scope'}))

    def test_reviewed_binding_migration_preserves_native_state_and_private_bytes_backup(self):
        previous, supplied, paths, originals = self.migration_fixture()
        document = self.migration_plan(previous)
        self.assertEqual(len(document['files']), 2)
        self.assertNotIn(supplied['password'], json.dumps(document))
        result = self.migrate(previous, document)
        self.assertTrue(result['ok']); self.assertEqual(result['files_updated'], 2)
        self.assertEqual(result['native_settings_written'], 0)
        self.assertEqual(result['subtitle_jobs_started'], 0)
        self.assertFalse(self.writes())
        self.migration_lock.assert_called_once_with(self.c)
        for index, path in enumerate(paths):
            self.assertEqual((Path(result['backup']) / (str(index) + '-' + path.name)).read_bytes(), originals[str(path)])
            self.assertEqual(json.loads(path.read_text())['binding_scope'], subtitles.BINDING_SCOPE)
        self.assertEqual(json.loads(paths[1].read_text())['credentials'], supplied)
        self.assertTrue(subtitles.verify(self.c, **self.options())['ok'])

    def test_unselected_supported_private_input_is_migrated_without_being_enabled(self):
        previous, supplied, paths, _ = self.migration_fixture(selected=False)
        document = self.migration_plan(previous)
        self.assertEqual(document['inactive_private_inputs'], ['opensubtitlescom'])
        self.migrate(previous, document)
        self.assertEqual(self.vendor.general['enabled_providers'], ['gestdown'])
        self.assertNotIn('opensubtitlescom', self.vendor.other_settings)
        self.assertEqual(subtitles._credentials(self.c, ['opensubtitlescom'])['opensubtitlescom'], supplied)
        self.assertFalse(self.writes())

    def test_binding_migration_requires_exact_predecessor_and_same_media_authority(self):
        previous, _, _, _ = self.migration_fixture()
        unrelated = copy.deepcopy(previous); unrelated['features']['dashboard'] = not unrelated['features'].get('dashboard', False)
        with self.assertRaisesRegex(SafetyError, 'another host or configuration'):
            self.migration_plan(unrelated)
        changed = copy.deepcopy(previous); changed['puid'] += 1
        with self.assertRaisesRegex(SafetyError, 'identity changed'):
            self.migration_plan(changed)
        self.assertFalse(self.writes())

    def test_binding_migration_rejects_provider_native_or_persistent_target_drift(self):
        previous, _, _, _ = self.migration_fixture()
        original = copy.deepcopy(self.target)
        for key in ('image', 'providers', 'directory'):
            with self.subTest(key=key):
                self.target.clear(); self.target.update(copy.deepcopy(original))
                if key == 'image': self.target['image'] = 'sha256:' + 'e' * 64
                elif key == 'providers': self.target['providers']['gestdown'] = 'f' * 64
                else: self.target['directory']['inode'] += 1
                with self.assertRaisesRegex(SafetyError, 'no binding was migrated'):
                    self.migration_plan(previous)
        self.target.clear(); self.target.update(original)
        self.vendor.other_settings['opensubtitlescom']['password'] = 'fixture-owner-edited-private'  # release-audit: synthetic - native credential drift fixture
        with self.assertRaisesRegex(SafetyError, 'did not retain the private'):
            self.migration_plan(previous)
        self.assertFalse(self.writes())

    def test_raw_input_change_after_review_refuses_binding_apply(self):
        previous, _, paths, _ = self.migration_fixture()
        document = self.migration_plan(previous)
        paths[1].write_bytes(paths[1].read_bytes() + b' ')
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.migrate(previous, document)
        self.assertNotIn('binding_scope', json.loads(paths[0].read_text()))
        self.assertFalse(self.writes())

    @unittest.skipUnless(os.name == 'posix', 'POSIX private directory mode')
    def test_binding_migration_requires_private_backup_parent(self):
        previous, _, _, _ = self.migration_fixture()
        (self.root / 'backups').chmod(0o755)
        with self.assertRaisesRegex(SafetyError, 'root-private managed directory'):
            self.migration_plan(previous)
        self.assertFalse(self.writes())

    def test_partial_metadata_migration_resumes_only_after_a_fresh_review(self):
        previous, supplied, paths, _ = self.migration_fixture()
        original_plan = self.migration_plan(previous)
        actual_write = subtitles.write_json
        def interrupted(path, value, **kwargs):
            if Path(path) == paths[1] and value.get('binding_scope') == subtitles.BINDING_SCOPE:
                raise OSError('synthetic interruption before second metadata replacement')
            return actual_write(path, value, **kwargs)
        with patch.object(subtitles, 'write_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'synthetic interruption'):
                self.migrate(previous, original_plan)
        self.assertEqual(json.loads(paths[0].read_text())['binding_scope'], subtitles.BINDING_SCOPE)
        self.assertNotIn('binding_scope', json.loads(paths[1].read_text()))
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.migrate(previous, original_plan)
        fresh = self.migration_plan(previous)
        self.assertEqual(len(fresh['files']), 1)
        self.migrate(previous, fresh)
        self.assertEqual(subtitles._credentials(self.c, ['opensubtitlescom'])['opensubtitlescom'], supplied)
        self.assertFalse(self.writes())

    def test_new_policy_preserves_profiles_languages_connections_and_does_not_search(self):
        custom = {**subtitles._profile(4), "name": "Existing French", "tag": "custom"}
        self.vendor.profiles = [custom]
        before = copy.deepcopy(self.vendor.other_settings)
        result = self.apply()
        self.assertTrue(result["ok"])
        self.assertEqual(result["subtitle_delivery"], "not_tested")
        self.assertEqual(self.vendor.profiles[0], custom)
        self.assertEqual(self.vendor.profiles[1], subtitles._profile(5, subtitles.profile_name(self.c)))
        self.assertEqual(self.vendor.other_settings, before)
        self.assertTrue(all(row["enabled"] for row in self.vendor.languages))
        self.assertEqual(self.vendor.general["concurrent_jobs"], 1)
        self.assertEqual(self.vendor.general["wanted_search_frequency"], 876000)
        self.assertFalse(self.vendor.general["upgrade_subs"])
        self.assertEqual([row[1] for row in self.writes()], ["/api/system/settings"])
        self.assertEqual(self.receipt()["state"], "verified")

    def test_repeat_does_not_rewrite_profiles_or_settings(self):
        self.apply(); self.vendor.calls.clear(); self.apply()
        self.assertFalse(self.writes())

    def test_verify_is_readonly_and_reports_throttle_honestly(self):
        self.apply(); self.vendor.calls.clear(); self.vendor.provider_state = "TooManyRequests"
        result = subtitles.verify(self.c, **self.options())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["notifications"]), 2)
        self.assertFalse(self.writes())

    def test_verify_survives_owned_container_recreation_and_block_device_renumbering(self):
        self.target["directory"]["filesystem"] = {"target": "/", "source": "UUID=fixture-app-filesystem", "fstype": "ext4"}
        self.apply(); before = (self.root / "state/subtitle-setup.json").read_bytes()
        self.vendor.calls.clear()
        self.target["container"] = "d" * 64
        self.target["directory"]["device"] = 65
        self.assertTrue(subtitles.verify(self.c, **self.options())["ok"])
        self.assertFalse(self.writes())
        self.assertEqual((self.root / "state/subtitle-setup.json").read_bytes(), before)

    def test_recreated_container_still_rejects_foreign_image_provider_or_datastore(self):
        self.target["directory"]["filesystem"] = {"target": "/", "source": "UUID=fixture-app-filesystem", "fstype": "ext4"}
        self.apply(); original = copy.deepcopy(self.target)
        for key in ("image", "providers", "inode", "filesystem"):
            with self.subTest(key=key):
                self.target.clear(); self.target.update(copy.deepcopy(original)); self.target["container"] = "d" * 64
                if key == "image":self.target["image"] = "sha256:" + "e" * 64
                elif key == "providers":self.target["providers"][subtitles.DEFAULT_PROVIDERS[0]] = "e" * 64
                elif key == "inode":self.target["directory"]["inode"] += 1
                else:self.target["directory"]["filesystem"]["source"] = "UUID=foreign-filesystem"
                with self.assertRaisesRegex(SafetyError, "target changed"):
                    subtitles.verify(self.c, **self.options())

    def test_unmanaged_matching_name_cannot_be_adopted(self):
        self.vendor.profiles = [subtitles._profile(1)]
        with self.assertRaisesRegex(SafetyError, "existing subtitle profile"):self.plan()
        self.assertFalse(self.writes())

    def test_verified_profile_is_not_recreated_after_deletion(self):
        self.apply(); self.vendor.profiles.clear(); self.vendor.calls.clear()
        with self.assertRaisesRegex(SafetyError, "will not be recreated"):self.plan()
        self.assertFalse(self.writes())

    def test_lost_response_preserves_intent_and_resumes_without_duplicate_profile(self):
        self.vendor.lost = True
        with self.assertRaises(SafetyError) as caught:self.apply()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(self.receipt()["state"], "intent")
        self.vendor.calls.clear(); self.apply()
        self.assertEqual(len(self.vendor.profiles), 1)
        self.assertFalse(self.writes())

    def test_profile_or_settings_change_invalidates_review(self):
        for resource in ("settings", "profiles", "languages"):
            with self.subTest(resource=resource):
                preview = self.plan()
                if resource == "settings":self.vendor.general["concurrent_jobs"] += 1
                elif resource == "profiles":self.vendor.profiles.append({**subtitles._profile(10), "name": "Other"})
                else:self.vendor.languages[1]["enabled"] = not self.vendor.languages[1]["enabled"]
                with self.assertRaisesRegex(SafetyError, "changed after review"):self.apply(preview)
        self.assertFalse(self.writes())

    def test_late_race_prevents_replace_all_profile_write(self):
        preview = self.plan(); self.vendor.race = True
        with self.assertRaisesRegex(SafetyError, "changed before writing"):self.apply(preview)
        self.assertFalse(self.writes())

    def test_ignored_writes_fail_exact_readback(self):
        for ignored in ("profiles", "languages", "concurrent_jobs"):
            with self.subTest(ignored=ignored):
                self.vendor = Vendor(); self.vendor.ignore = ignored
                path = self.root / "state/subtitle-setup.json"
                if path.exists():path.unlink()
                with self.assertRaises(SafetyError):self.apply()
                self.assertEqual(self.receipt()["state"], "intent")

    def test_other_profile_changed_during_write_is_detected_without_rollback(self):
        self.vendor.profiles = [{**subtitles._profile(3), "name": "Custom"}]
        self.vendor.ignore = "other-profile"
        with self.assertRaisesRegex(SafetyError, "Other subtitle profiles changed"):self.apply()
        self.assertEqual(len(self.vendor.profiles), 2)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.vendor.calls))

    def test_changed_container_after_write_cannot_gain_verified_receipt(self):
        preview = self.plan()
        self.target_mock.side_effect = [self.target, {**self.target, "container": "d" * 64}]
        with self.assertRaisesRegex(SafetyError, "target changed"):self.apply(preview)
        self.assertEqual(self.receipt()["state"], "intent")

    def test_disk_guard_checks_both_filesystems_before_api_write(self):
        for low_index in (0, 1):
            self.space.side_effect = [shutil._ntuple_diskusage(200, 100, 49*subtitles.GIB if low_index == 0 else 100*subtitles.GIB),
                                      shutil._ntuple_diskusage(200, 100, 19*subtitles.GIB if low_index == 1 else 100*subtitles.GIB)]
            with self.assertRaisesRegex(SafetyError, "50 GiB"):self.plan()
        self.assertFalse(self.vendor.calls)

    def test_wrong_version_and_html_route_response_are_refused(self):
        self.vendor.version = "1.7.0"
        with self.assertRaisesRegex(SafetyError, "1.6.2"):self.plan()
        self.vendor.version = "1.6.2"
        original = self.vendor
        self.vendor = lambda method, url, **options: "<html>SPA</html>" if url.endswith("/system/languages/profiles") else original(method,url,**options)
        with self.assertRaisesRegex(SafetyError, "unsupported subtitle"):self.plan()

    def test_ai_unknown_duplicate_and_empty_provider_choices_refused(self):
        for choices in (["whisperai"], ["embeddedsubtitles"], [], ["gestdown", "gestdown"], ["unknown"]):
            with self.assertRaises(SafetyError):self.plan(choices)
        self.assertFalse(self.vendor.calls)

    def test_provider_credentials_are_private_and_absent_from_review(self):
        supplied = {"username": "fixture-provider-user", "password": "fixture-provider-private"}  # release-audit: synthetic - subtitle account fixture
        subtitles.save_provider_credentials(self.c, "opensubtitlescom", supplied)
        preview = self.plan(["opensubtitlescom"])
        self.assertNotIn(supplied["password"], json.dumps(preview))
        result = self.apply(preview)
        self.assertTrue(result["ok"])
        self.assertEqual(self.vendor.other_settings["opensubtitlescom"]["password"], supplied["password"])
        self.assertFalse(self.vendor.other_settings["opensubtitlescom"]["include_ai_translated"])
        with self.assertRaisesRegex(SafetyError, "differ"):
            subtitles.save_provider_credentials(self.c, "opensubtitlescom", {**supplied, "username": "different"})

    def test_private_credentials_cannot_cross_host(self):
        supplied = {"username": "fixture-provider-user", "password": "fixture-provider-private"}  # release-audit: synthetic - subtitle account fixture
        subtitles.save_provider_credentials(self.c, "opensubtitlescom", supplied)
        with patch.object(engine, "host_identity", return_value="different-host"):
            with self.assertRaisesRegex(SafetyError, "another installation"):self.plan(["opensubtitlescom"])

    def test_opensubtitles_email_login_refused_before_private_input_or_vendor_write(self):
        supplied = {"username": "fixture-provider@example.invalid", "password": "fixture-provider-private"}  # release-audit: synthetic - subtitle account fixture
        with self.assertRaisesRegex(SafetyError, "account username, not an email"):
            subtitles.save_provider_credentials(self.c, "opensubtitlescom", supplied)
        root, _ = subtitles._paths(self.c)
        self.assertFalse((root / "secrets/subtitle-provider-opensubtitlescom.json").exists())
        self.assertFalse(self.vendor.calls)

    def test_older_saved_email_login_refused_before_vendor_requests(self):
        supplied = {"username": "fixture-provider-user", "password": "fixture-provider-private"}  # release-audit: synthetic - subtitle account fixture
        subtitles.save_provider_credentials(self.c, "opensubtitlescom", supplied)
        root, _ = subtitles._paths(self.c)
        path = root / "secrets/subtitle-provider-opensubtitlescom.json"
        value = json.loads(path.read_text())
        value['credentials']['username'] = 'fixture-provider@example.invalid'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(SafetyError, "account username, not an email"):
            self.plan(['opensubtitlescom'])
        self.assertFalse(self.vendor.calls)

    def test_verify_requires_verified_receipt(self):
        with self.assertRaisesRegex(SafetyError, "verified subtitle"):subtitles.verify(self.c, **self.options())

    def test_existing_modifiers_use_real_toggle_contract_instead_of_invalid_list_setting(self):
        self.vendor.general['subzero_mods'] = ['remove_HI', 'OCR_fixes', 'color(name=yellow)']
        self.assertTrue(self.apply()['ok'])
        form = self.writes()[0][2]['form']
        self.assertNotIn('settings-general-subzero_mods', [key for key, _ in form])
        self.assertIn(('subzero-remove_HI', 'false'), form)
        self.assertIn(('subzero-OCR_fixes', 'false'), form)
        self.assertIn(('subzero-color', ''), form)
        self.assertEqual(self.vendor.general['subzero_mods'], [])

    def test_profile_items_satisfy_the_real_indexer_consumer_not_frontend_camel_case(self):
        expected = [{'language': 'en', 'forced': 'False', 'hi': 'False'}]
        self.assertEqual(pinned_indexer_languages(subtitles._profile(1)), expected)
        self.assertEqual(pinned_indexer_languages(subtitles._profile(1), ['en']), expected)
        with self.assertRaises(KeyError):pinned_indexer_languages(subtitles._legacy_profile(1))

    def legacy(self):
        self.apply()
        legacy = subtitles._legacy_profile(1)
        self.vendor.profiles = [legacy]
        path = self.root / 'state/subtitle-setup.json'
        receipt = self.receipt();receipt['profile'] = legacy
        write_json(path, receipt, replace=True)
        self.vendor.calls.clear()
        return legacy

    def test_reviewed_repair_only_replaces_exact_receipt_owned_legacy_profile_and_backs_up_all(self):
        legacy = self.legacy()
        other = {**subtitles._profile(5), 'name': 'Existing custom profile'}
        self.vendor.profiles.append(other)
        preview = self.plan()
        self.assertTrue(preview['repair_profile'])
        self.assertEqual(preview['profile_action'], 'repair_owned_legacy_profile')
        self.assertFalse(preview['create_profile'])
        self.assertTrue(self.apply(preview)['ok'])
        self.assertEqual(self.vendor.profiles, [subtitles._profile(1, subtitles.profile_name(self.c)), other])
        backup = json.loads(next((self.root/'state').glob('subtitle-profile-repair-*.json')).read_text())
        self.assertEqual(backup['before']['profiles'], [legacy, other])
        self.assertEqual(self.receipt()['previous_profile'], legacy)
        self.assertEqual(self.receipt()['profile'], subtitles._profile(1, subtitles.profile_name(self.c)))

    def test_repair_refuses_foreign_or_edited_legacy_profile(self):
        self.legacy()
        self.vendor.profiles[0]['items'][0]['audioExclude'] = 'True'
        with self.assertRaisesRegex(SafetyError, 'existing subtitle profile'):self.plan()
        self.assertFalse(self.writes())

    def test_lost_repair_response_resumes_from_exact_saved_intent_without_second_profile_write(self):
        self.legacy();self.vendor.lost = True
        with self.assertRaises(SafetyError):self.apply()
        self.assertEqual(self.receipt()['state'], 'intent')
        self.assertEqual(self.vendor.profiles, [subtitles._profile(1, subtitles.profile_name(self.c))])
        self.vendor.calls.clear();self.apply()
        self.assertFalse(self.writes())
        self.assertEqual(self.receipt()['state'], 'verified')

    def test_ignored_repair_preserves_legacy_intent_for_reviewed_retry(self):
        legacy = self.legacy();self.vendor.ignore = 'profiles'
        with self.assertRaisesRegex(SafetyError, 'exact English'):self.apply()
        self.assertEqual(self.vendor.profiles, [legacy])
        self.assertEqual(self.receipt()['previous_profile'], legacy)
        self.vendor.ignore = None
        self.assertTrue(self.apply()['ok'])
        self.assertEqual(self.vendor.profiles, [subtitles._profile(1, subtitles.profile_name(self.c))])

    def test_owned_old_display_name_is_renamed_without_changing_policy_id_or_other_profiles(self):
        self.apply()
        old=subtitles._profile(1)
        self.vendor.profiles=[old, {**subtitles._profile(8), 'name':'Owner custom', 'tag':'owner'}]
        path=self.root/'state/subtitle-setup.json'
        saved=self.receipt();saved['profile']=old
        write_json(path,saved,replace=True)
        before=copy.deepcopy(self.vendor.profiles)
        reviewed=self.plan()
        self.assertEqual('rename_owned_profile',reviewed['profile_action'])
        self.assertTrue(self.apply(reviewed)['ok'])
        expected=copy.deepcopy(before);expected[0]['name']=subtitles.profile_name(self.c)
        self.assertEqual(expected,self.vendor.profiles)
        self.assertEqual(old,self.receipt()['previous_profile'])
        self.vendor.calls.clear()
        self.assertTrue(self.apply()['ok']);self.assertFalse(self.writes())

    def test_duplicate_legacy_and_friendly_subtitle_names_refuse_replace_all_write(self):
        self.apply()
        self.vendor.profiles.append(subtitles._profile(2))
        self.vendor.calls.clear()
        with self.assertRaises(SafetyError):self.plan()
        self.assertFalse(self.writes())

    def test_legacy_repair_snapshot_change_invalidates_review_without_profile_write(self):
        self.legacy();preview = self.plan()
        self.vendor.profiles.append({**subtitles._profile(3), 'name': 'Operator-added'})
        with self.assertRaisesRegex(SafetyError, 'changed after review'):self.apply(preview)
        self.assertFalse(self.writes())


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config()
        self.calls, self.bad = [], None
        self.enterContext(patch.object(engine, "check_container_ownership"))
        self.enterContext(patch.object(onboarding, "check_targets"))
        self.enterContext(patch.object(onboarding, "_identity", return_value={"device": 1, "inode": 2}))
        self.enterContext(patch.object(engine, "recipe", return_value={"services": {"bazarr": {"image": "fixture@sha256:" + "b"*64}}}))

    def runner(self, argv, **options):
        self.calls.append((argv, options))
        args = argv[3:]
        image = "sha256:" + "b"*64
        defaults = {"Cmd": None, "Entrypoint": ["/init"], "WorkingDir": "/"}
        if args[0] == "ps":value = "a"*64
        elif args[:2] == ["image", "inspect"]:
            value = image if args[-1] == "{{.Id}}" else json.dumps(defaults)
        elif args[0] == "inspect":
            if args[-1] == "{{json .Image}}":value = json.dumps("sha256:" + "f"*64 if self.bad == "image" else image)
            elif args[-1] == "{{json .Config}}":value = json.dumps({**defaults, "Cmd": ["alternate"]} if self.bad == "command" else defaults)
            elif args[-1] == "{{json .Mounts}}":
                mounts = [{"Destination": "/config", "Type": "bind", "Source": "/opt/samurai/appdata/bazarr", "RW": True},
                          {"Destination": "/data", "Type": "bind", "Source": self.c["storage"]["data_root"], "RW": True}]
                if self.bad == "source":mounts[1]["Source"] = "/wrong"
                if self.bad == "readonly":mounts[0]["RW"] = False
                if self.bad == "nested":mounts.append({"Destination": "/data/library", "Source": "/wrong"})
                if self.bad == "source-overlay":mounts.append({"Destination": "/app/bazarr", "Source": "/wrong"})
                value = json.dumps(mounts)
            else:raise AssertionError(argv)
        elif args[0] == "exec":
            self.assertIn("/lsiopy/bin/python3", args)
            self.assertNotIn(KEY, json.dumps(argv))
            names = json.loads(options["input"])
            value = "{}" if self.bad == "provider" else json.dumps({name: "c"*64 for name in names})
        else:raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, value, "")

    def test_owned_pinned_target_and_installed_provider_hashes(self):
        result = subtitles._target(self.c, list(subtitles.DEFAULT_PROVIDERS), self.runner)
        self.assertEqual(set(result["providers"]), set(subtitles.DEFAULT_PROVIDERS))
        self.assertEqual(result["container"], "a"*64)

    def test_wrong_image_command_nested_media_mount_or_source_overlay_refuses(self):
        for bad in ("image", "command", "source", "readonly", "nested", "source-overlay"):
            with self.subTest(bad=bad):
                self.bad = bad
                with self.assertRaisesRegex(SafetyError, "owned pinned"):subtitles._target(self.c, list(subtitles.DEFAULT_PROVIDERS), self.runner)

    def test_missing_provider_cannot_be_recommended(self):
        self.bad = "provider"
        with self.assertRaisesRegex(SafetyError, "not available"):subtitles._target(self.c, list(subtitles.DEFAULT_PROVIDERS), self.runner)


if __name__ == "__main__":unittest.main()
