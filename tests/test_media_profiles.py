import copy
from contextlib import nullcontext
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from samurai_stack import media_profiles
from samurai_stack.safety import SafetyError, digest
from test_catalog import config


class StarrProfiles:
    """Stateful v3 API model. Unknown write endpoints fail the test."""
    def __init__(self):
        self.calls = []
        self.ignore_writes = False
        self.fail_after = None
        self.counter = 0
        self.profiles = {}
        self.formats = {}
        self.definitions = {}
        self.schemas = {}
        self.format_schemas = {}
        self.titles = {service: [{"id": 88, "qualityProfileId": 1, "title": "Keep existing title"}]
                       for service in media_profiles.SERVICES}
        names = ["Unknown", "SDTV", "DVD", "HDTV-720p", "HDTV-1080p", "WEBRip-720p", "WEBDL-720p",
                 "WEBRip-1080p", "WEBDL-1080p", "Bluray-720p", "Bluray-1080p", "Remux-1080p", "Bluray-2160p"]
        self.quality_ids = {name: number for number, name in enumerate(names, 1)}
        for service in media_profiles.SERVICES:
            self.formats[service] = [{"id": 1, "name": "Someone else's preferred release", "includeCustomFormatWhenRenaming": True,
                                      "specifications": [{"name": "Existing", "implementation": "ReleaseTitleSpecification",
                                                          "negate": False, "required": False,
                                                          "fields": [{"name": "value", "value": "EXISTING"}]}]}]
            self.schemas[service] = {
                "id": 0, "name": "", "upgradeAllowed": True, "cutoff": 1,
                "items": [{"name": name, "quality": {"id": number, "name": name},
                           "items": [], "allowed": True} for name, number in self.quality_ids.items()],
                "formatItems": [{"format": 1, "name": self.formats[service][0]["name"], "score": 0}],
                "minFormatScore": -10000, "cutoffFormatScore": 10000, "minUpgradeFormatScore": 50,
                "futureVendorField": {"retained": True},
            }
            if service == "radarr":
                self.schemas[service]["language"] = {"id": -1, "name": "Any"}
            self.profiles[service] = [{**copy.deepcopy(self.schemas[service]), "id": 1, "name": "My existing profile"}]
            self.definitions[service] = [{"id": number, "quality": {"id": number, "name": name},
                                          "title": name, "weight": number, "minSize": 0, "preferredSize": 1999,
                                          "maxSize": 2000, "futureVendorField": "keep"}
                                         for name, number in self.quality_ids.items()]
            self.format_schemas[service] = [
                {"implementation": "ReleaseTitleSpecification", "implementationName": "Release Title",
                 "presets": [], "fields": [{"name": "value", "value": "", "type": "textbox"}]},
                {"implementation": "LanguageSpecification", "implementationName": "Language",
                 "presets": [], "fields": [{"name": "value", "value": 0, "type": "select"},
                                           {"name": "exceptLanguage", "value": False, "type": "checkbox"}]},
            ]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        uri = urlsplit(url)
        service = {8989: "sonarr", 7878: "radarr"}[uri.port]
        path = uri.path.removeprefix("/api/v3/")
        self.calls.append((method, service, path, copy.deepcopy(json)))
        self.counter += 1
        if self.fail_after is not None and self.counter >= self.fail_after:
            raise RuntimeError("password=secret-vendor-echo")
        if method == "GET":
            if path == "qualityprofile/schema":
                result = copy.deepcopy(self.schemas[service])
                result["formatItems"] = [{"format": row["id"], "name": row["name"], "score": 0}
                                         for row in self.formats[service]]
                return result
            if path == "customformat/schema":
                return copy.deepcopy(self.format_schemas[service])
            if path == "language":
                return [{"id": 0, "name": "Unknown"}, {"id": 1, "name": "English"}, {"id": -2, "name": "Original"}]
            return copy.deepcopy({"qualityprofile": self.profiles, "customformat": self.formats,
                                  "qualitydefinition": self.definitions}[path][service])
        if method == "POST" and path in {"customformat", "qualityprofile"}:
            if path == "qualityprofile" and json.get("minUpgradeFormatScore", 1) < 1:
                raise ValueError("MinUpgradeFormatScore must be at least 1 in live Starr")
            rows = self.formats[service] if path == "customformat" else self.profiles[service]
            item = {**copy.deepcopy(json), "id": max(row["id"] for row in rows) + 1}
            if not self.ignore_writes:
                rows.append(item)
                if path == "customformat":
                    # Starr appends new formats at score zero to other profiles.
                    for profile in self.profiles[service]:
                        profile["formatItems"].append({"format": item["id"], "name": item["name"], "score": 0})
            return copy.deepcopy(item)
        if method == "PUT" and path.startswith("qualitydefinition/"):
            identifier = int(path.rsplit("/", 1)[-1])
            if not self.ignore_writes:
                index = next(index for index, row in enumerate(self.definitions[service]) if row["id"] == identifier)
                self.definitions[service][index] = copy.deepcopy(json)
            return copy.deepcopy(json)
        raise AssertionError("Unexpected mutation endpoint: " + method + " " + path)


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.c = config()
        self.c["deployment_id"] = "f26af450-4590-4d51-aa7a-f3284b90f174"
        self.vendor = StarrProfiles()
        self.target_guard = patch.object(media_profiles.hardlinks, "check_targets").start()
        self.host_identity = patch.object(media_profiles.engine, "host_identity", return_value="synthetic-profile-host").start()
        patch.object(media_profiles.engine, "deployment_lock", return_value=nullcontext()).start()
        self.backup = patch.object(media_profiles, "_backup").start()
        self.addCleanup(patch.stopall)

    def plan(self, **kwargs):
        return media_profiles.plan(self.c, request=self.vendor, secret_resolver=lambda name: "private-key", **kwargs)

    def apply(self, reviewed):
        return media_profiles.apply(self.c, reviewed, request=self.vendor, secret_resolver=lambda name: "private-key")

    def verify(self, **kwargs):
        return media_profiles.verify(self.c, request=self.vendor, secret_resolver=lambda name: "private-key", **kwargs)

    def test_preview_is_read_only_and_reports_global_sizes_unchanged(self):
        reviewed = self.plan()
        self.assertFalse(self.vendor.writes)
        self.assertEqual(reviewed["policy"]["global_sizes"], "unchanged")
        self.assertEqual(reviewed["services"]["sonarr"]["action"], "create")
        self.assertFalse(any(call[2] == "qualitydefinition" for call in self.vendor.calls))
        self.assertIn("not English-only", reviewed["limitations"][0])

    def test_review_binds_the_current_host_and_complete_site_configuration(self):
        reviewed = self.plan()
        self.assertEqual("synthetic-profile-host", reviewed["host_id"])
        self.assertEqual(digest(self.c), reviewed["config_digest"])

    def test_another_hosts_identical_api_state_refuses_every_write(self):
        reviewed = self.plan()
        self.host_identity.return_value = "synthetic-another-host"
        with self.assertRaisesRegex(SafetyError, "changed after the preview"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)
        self.backup.assert_not_called()

    def test_changed_local_paths_refuse_the_old_review_before_any_write(self):
        reviewed = self.plan()
        original = copy.deepcopy(self.c)
        for key in ("install_root", "storage.data_root"):
            with self.subTest(path=key):
                self.c = copy.deepcopy(original)
                if key == "install_root":
                    self.c[key] += "-another-installation"
                else:
                    self.c["storage"]["data_root"] += "/another-directory"
                with self.assertRaisesRegex(SafetyError, "changed after the preview"):
                    self.apply(reviewed)
                self.assertFalse(self.vendor.writes)
                self.backup.assert_not_called()

    def test_creates_bounded_original_sdr_profiles_without_changing_existing_policy_or_titles(self):
        before_profiles = copy.deepcopy(self.vendor.profiles)
        before_formats = copy.deepcopy(self.vendor.formats)
        before_definitions = copy.deepcopy(self.vendor.definitions)
        before_titles = copy.deepcopy(self.vendor.titles)
        result = self.apply(self.plan())
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile_ids"], {"sonarr": 2, "radarr": 2})
        self.backup.assert_called_once()
        self.target_guard.assert_called()
        self.assertEqual(self.vendor.definitions, before_definitions)
        self.assertEqual(self.vendor.titles, before_titles)
        for service in media_profiles.SERVICES:
            existing = self.vendor.profiles[service][0]
            # The app adds score-zero CF references itself; every old preference
            # remains byte-for-byte identical. We never PUT an existing profile.
            old_format_count = len(before_profiles[service][0]["formatItems"])
            self.assertEqual(existing["formatItems"][:old_format_count], before_profiles[service][0]["formatItems"])
            old = copy.deepcopy(existing)
            old["formatItems"] = old["formatItems"][:old_format_count]
            self.assertEqual(old, before_profiles[service][0])
            self.assertEqual(self.vendor.formats[service][0], before_formats[service][0])
            profile = self.vendor.profiles[service][1]
            allowed = {row["quality"]["name"] for row in profile["items"] if row["allowed"]}
            self.assertEqual(allowed, media_profiles.QUALITIES)
            self.assertFalse(profile["upgradeAllowed"])
            self.assertEqual(profile["minFormatScore"], 0)
            self.assertEqual(profile["minUpgradeFormatScore"], 1)
            self.assertTrue(all(row["score"] <= 0 for row in profile["formatItems"]))
            self.assertEqual(profile["futureVendorField"], {"retained": True})
            self.assertEqual(profile["formatItems"][0]["score"], 0)
        language_cf = next(row for row in self.vendor.formats["sonarr"] if "Missing original" in row["name"])
        self.assertTrue(language_cf["specifications"][0]["negate"])
        self.assertEqual(language_cf["specifications"][0]["fields"][0]["value"], -2)
        self.assertEqual(self.vendor.profiles["radarr"][1]["language"], {"id": -2, "name": "Original"})
        self.assertFalse(any(call[0] == "PUT" for call in self.vendor.writes))

    def test_retry_is_idempotent_and_verification_read_only(self):
        self.apply(self.plan())
        self.vendor.calls.clear()
        self.assertTrue(self.apply(self.plan())["ok"])
        self.assertFalse(self.vendor.writes)
        self.assertTrue(self.verify()["ok"])
        self.assertFalse(self.vendor.writes)

    def test_global_sizes_need_explicit_review_and_preserve_unrelated_settings(self):
        before = copy.deepcopy(self.vendor.definitions)
        reviewed = self.plan(include_global_sizes=True)
        self.assertFalse(self.vendor.writes)
        self.assertEqual(len(reviewed["services"]["sonarr"]["size_changes"]), 8)
        self.assertTrue(self.apply(reviewed)["ok"])
        writes = [call for call in self.vendor.writes if call[0] == "PUT"]
        self.assertEqual(len(writes), 16)
        for service in media_profiles.SERVICES:
            for original, actual in zip(before[service], self.vendor.definitions[service]):
                quality = original["quality"]["name"]
                if quality in media_profiles.QUALITIES:
                    values = dict(zip(media_profiles.LIMIT_KEYS, media_profiles.LIMITS[service][1080 if quality.endswith("1080p") else 720]))
                    expected = {**original, **values}
                    self.assertEqual(actual, expected)
                else:
                    self.assertEqual(actual, original)
        self.vendor.calls.clear()
        self.assertTrue(self.apply(self.plan(include_global_sizes=True))["ok"])
        self.assertFalse(self.vendor.writes)

    def test_stale_review_refuses_every_write(self):
        reviewed = self.plan(include_global_sizes=True)
        self.vendor.definitions["radarr"][3]["minSize"] = 9
        with self.assertRaisesRegex(SafetyError, "changed after the preview"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)

    def test_unlimited_definition_nullable_fields_can_be_omitted_by_live_radarr(self):
        row = next(row for row in self.vendor.definitions["radarr"] if row["quality"]["name"] == "Bluray-1080p")
        del row["preferredSize"]
        del row["maxSize"]
        reviewed = self.plan(include_global_sizes=True)
        change = next(row for row in reviewed["services"]["radarr"]["size_changes"] if row["quality"] == "Bluray-1080p")
        self.assertIsNone(change["before"]["maxSize"])
        self.assertTrue(self.apply(reviewed)["ok"])

    def test_tampering_with_preview_refuses_every_write(self):
        reviewed = self.plan()
        reviewed["policy"]["upgrades"] = True
        with self.assertRaisesRegex(SafetyError, "fresh profile plan"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)

    def test_matching_name_with_different_policy_is_not_adopted_or_overwritten(self):
        self.apply(self.plan())
        self.vendor.profiles["sonarr"][1]["upgradeAllowed"] = True
        self.vendor.calls.clear()
        with self.assertRaisesRegex(SafetyError, "existing Samurai quality profile differs"):
            self.plan()
        self.assertFalse(self.vendor.writes)
        self.assertTrue(self.vendor.profiles["sonarr"][1]["upgradeAllowed"])

    def test_differing_custom_format_cannot_bypass_penalty(self):
        self.apply(self.plan())
        self.vendor.formats["sonarr"][1]["specifications"][0]["negate"] = True
        self.vendor.calls.clear()
        with self.assertRaisesRegex(SafetyError, "existing Samurai custom format differs"):
            self.plan()
        self.assertFalse(self.vendor.writes)

    def test_ignored_writes_are_not_reported_as_success(self):
        self.vendor.ignore_writes = True
        with self.assertRaisesRegex(SafetyError, "did not retain"):
            self.apply(self.plan())
        self.assertEqual(len(self.vendor.profiles["sonarr"]), 1)

    def test_transport_errors_are_sanitized(self):
        self.vendor.fail_after = 1
        with self.assertRaisesRegex(RuntimeError, "API request failed") as caught:
            self.plan()
        self.assertNotIn("secret-vendor", str(caught.exception))

    def test_missing_schema_support_refuses_before_writes(self):
        self.vendor.format_schemas["sonarr"] = self.vendor.format_schemas["sonarr"][:1]
        with self.assertRaisesRegex(SafetyError, "lacks a required"):
            self.plan()
        self.assertFalse(self.vendor.writes)

    def test_grouped_qualities_keep_parent_allowed_and_use_group_cutoff(self):
        for service in media_profiles.SERVICES:
            items = self.vendor.schemas[service]["items"]
            bluray_id = self.vendor.quality_ids["Bluray-1080p"]
            bluray = next(row for row in items if row["quality"]["id"] == bluray_id)
            remux = next(row for row in items if row["quality"]["name"] == "Remux-1080p")
            items[:] = [row for row in items if row not in (bluray, remux)]
            items.append({"id": 1000, "name": "Custom 1080 group", "quality": None, "allowed": True,
                          "items": [bluray, remux]})
        self.apply(self.plan())
        for service in media_profiles.SERVICES:
            profile = self.vendor.profiles[service][1]
            group = profile["items"][-1]
            self.assertEqual(profile["cutoff"], 1000)
            self.assertTrue(group["allowed"])
            self.assertEqual([row["allowed"] for row in group["items"]], [True, False])

    def test_hdr_pattern_rejects_common_markers_without_matching_dvd_or_david(self):
        for name in ["Movie.1080p.HDR10+.WEB", "Movie.Dolby.Vision.1080p", "Movie.DoVi", "Movie.DV", "Show.HLG"]:
            self.assertIsNotNone(re.search(media_profiles.HDR_PATTERN, name), name)
        for name in ["Movie.David.1080p", "Movie.DVD", "Movie.1080p.SDR", "Movie.Adventure"]:
            self.assertIsNone(re.search(media_profiles.HDR_PATTERN, name), name)


if __name__ == "__main__":
    unittest.main()
