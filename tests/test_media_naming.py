"""Reviewed future-import naming never reaches media mutation endpoints."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from samurai_stack import cli, media_naming
from samurai_stack.safety import SafetyError


class StarrNaming:
    def __init__(self):
        self.settings = {
            "sonarr": {"id": 1, "renameEpisodes": False, "replaceIllegalCharacters": True,
                "colonReplacementFormat": 4, "customColonReplacementFormat": "", "multiEpisodeStyle": 5,
                "standardEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title}",
                "dailyEpisodeFormat": "{Series Title} - {Air-Date} - {Episode Title}",
                "animeEpisodeFormat": "{Series Title} - S{season:00}E{episode:00}",
                "seriesFolderFormat": "{Series Title}", "seasonFolderFormat": "Season {season}",
                "specialsFolderFormat": "My Specials", "futureVendorField": {"keep": [1, 2]}},
            "radarr": {"id": 1, "renameMovies": False, "replaceIllegalCharacters": True,
                "colonReplacementFormat": "smart", "standardMovieFormat": "{Movie Title} ({Release Year})",
                "movieFolderFormat": "{Movie Title} ({Release Year})", "futureVendorField": "keep"}}
        self.calls = []
        self.bad_examples = None
        self.fail_service = None
        self.ignore_write = False
        self.after_write = None

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        uri = urlsplit(url)
        service = {8989: "sonarr", 7878: "radarr"}[uri.port]
        path = uri.path.removeprefix("/api/v3/config/naming")
        self.calls.append((method, service, path, copy.deepcopy(json)))
        if method == "GET" and not path:
            return copy.deepcopy(self.settings[service])
        if method == "GET" and path == "/examples":
            query = parse_qs(uri.query, keep_blank_values=True)
            # Without id=1 the upstream controller substitutes saved formats.
            if query.get("id") != ["1"]:
                raise AssertionError("Preview must validate proposed, not saved formats")
            if query.get("colonReplacementFormat") != [str(self.settings[service]["colonReplacementFormat"])]:
                raise AssertionError("Preview must preserve the vendor's exact colon-replacement value")
            for key, value in media_naming.POLICY[service].items():
                self_value = str(value).lower() if type(value) is bool else value
                if query.get(key) != [self_value]:
                    raise AssertionError("Every proposed format must reach the vendor preview")
            result = {key: "Validated " + key for key in media_naming.EXAMPLES[service]}
            if self.bad_examples:
                result[media_naming.EXAMPLES[service][0]] = self.bad_examples
            return result
        if method == "PUT" and path == "/1":
            if self.fail_service == service:
                raise RuntimeError("secret-vendor-body=fixture-sensitive-value")
            if not self.ignore_write:
                self.settings[service] = copy.deepcopy(json)
            if self.after_write:
                self.after_write(service)
            return {"id": 1}
        raise AssertionError("Unexpected naming endpoint: " + method + " " + path)


class NamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = cli.default_config()
        self.c["deployment_id"] = "fdb2fa60-7a69-4519-84f5-b7e228fb3964"
        self.c["install_root"] = str(self.root / "install")
        Path(self.c["install_root"]).mkdir()
        self.vendor = StarrNaming()
        self.guard = self.enterContext(mock.patch.object(media_naming, "_guard", return_value=Path(self.c["install_root"])))
        self.enterContext(mock.patch.object(media_naming.onboarding, "_root_owned", return_value=True))
        self.host = self.enterContext(mock.patch.object(media_naming.engine, "host_identity", return_value="fixture-naming-host"))
        self.enterContext(mock.patch.object(media_naming.engine, "deployment_lock", return_value=nullcontext()))

    def plan(self):
        return media_naming.plan(self.c, request=self.vendor, secret_resolver=lambda name: "fixture-key")

    def apply(self, reviewed):
        return media_naming.apply(self.c, reviewed, request=self.vendor, secret_resolver=lambda name: "fixture-key")

    def verify(self):
        return media_naming.verify(self.c, request=self.vendor, secret_resolver=lambda name: "fixture-key")

    def test_plan_is_readonly_and_all_formats_reach_examples_with_real_id(self):
        reviewed = self.plan()
        self.assertFalse(self.vendor.writes)
        self.assertEqual(reviewed["kind"], "future-import-naming")
        self.assertEqual(set(reviewed["services"]), {"sonarr", "radarr"})
        self.assertFalse((Path(self.c["install_root"]) / "backups").exists())
        self.assertEqual(reviewed["services"]["sonarr"]["changes"]["seriesFolderFormat"]["after"],
            "{Series CleanTitleWithoutYear} {(Series Year)} [tvdbid-{TvdbId}]")
        self.assertEqual(reviewed["services"]["radarr"]["changes"]["movieFolderFormat"]["after"],
            "{Movie CleanTitle} ({Release Year}) [tmdbid-{TmdbId}]")
        for service in media_naming.SERVICES:
            file_keys = [key for key in media_naming.POLICY[service] if key.endswith("EpisodeFormat") or key == "standardMovieFormat"]
            for key in file_keys:
                for token in ("Quality Full", "Release Group", "AudioCodec", "VideoCodec"):
                    self.assertIn(token, media_naming.POLICY[service][key])

    def test_apply_changes_only_reviewed_naming_fields_and_full_private_backup(self):
        original = copy.deepcopy(self.vendor.settings)
        reviewed = self.plan()
        self.assertTrue(self.apply(reviewed)["ok"])
        self.assertEqual(len(self.vendor.writes), 2)
        for service in media_naming.SERVICES:
            self.assertEqual(self.vendor.settings[service], {**original[service], **media_naming.POLICY[service]})
        backup = next((Path(self.c["install_root"]) / "backups").glob("media-naming-*.json"))
        self.assertEqual(json.loads(backup.read_text())["services"], original)
        if __import__("os").name == "posix":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(call[0] == "PUT" and call[2] == "/1" for call in self.vendor.writes))

    def test_already_matching_settings_do_not_write_or_create_backup(self):
        for service in media_naming.SERVICES:
            self.vendor.settings[service].update(media_naming.POLICY[service])
        self.assertTrue(self.verify()["ok"])
        self.assertTrue(self.apply(self.plan())["ok"])
        self.assertFalse(self.vendor.writes)
        self.assertFalse((Path(self.c["install_root"]) / "backups").exists())

    def test_verify_pending_never_saves_settings(self):
        result = self.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pending")
        self.assertFalse(self.vendor.writes)

    def test_radarr_string_and_numeric_enums_are_preserved_in_examples_put_and_backup(self):
        # Official Radarr enum names: delete/dash/spaceDash/spaceDashSpace/smart.
        # The serializer also allows integer values and future vendor enum names.
        for item in ("smart", "delete", "dash", "spaceDash", "spaceDashSpace", 4, "futureVendorEnum"):
            with self.subTest(item=item):
                vendor = StarrNaming(); vendor.settings["radarr"]["colonReplacementFormat"] = item
                plan = media_naming.plan(self.c, request=vendor, secret_resolver=lambda name: "fixture-key")
                self.assertTrue(media_naming.apply(self.c, plan, request=vendor, secret_resolver=lambda name: "fixture-key")["ok"])
                self.assertEqual(vendor.settings["radarr"]["colonReplacementFormat"], item)
                backup = Path(self.c["install_root"]) / "backups" / ("media-naming-" + plan["review_hash"] + ".json")
                self.assertEqual(json.loads(backup.read_text())["services"]["radarr"]["colonReplacementFormat"], item)

    def test_malformed_radarr_enum_and_sonarr_string_enum_are_refused_without_writes(self):
        for service, item in (("radarr", True), ("radarr", None), ("radarr", []),
                              ("radarr", "bad enum"), ("radarr", ""), ("sonarr", "smart")):
            with self.subTest(service=service, item=item):
                vendor = StarrNaming(); vendor.settings[service]["colonReplacementFormat"] = item
                with self.assertRaises(SafetyError):
                    media_naming.plan(self.c, request=vendor, secret_resolver=lambda name: "fixture-key")
                self.assertFalse(vendor.writes)

    def test_host_config_or_any_naming_field_change_invalidates_plan_before_writes(self):
        reviewed = self.plan()
        original_config, original_settings = copy.deepcopy(self.c), copy.deepcopy(self.vendor.settings)
        for kind in ("host", "path", "storage", "unknown-setting", "managed-setting"):
            with self.subTest(kind=kind):
                self.c = copy.deepcopy(original_config)
                self.vendor.settings = copy.deepcopy(original_settings)
                self.host.return_value = "fixture-naming-host"
                if kind == "host": self.host.return_value = "fixture-other-host"
                if kind == "path": self.c["install_root"] += "-other"
                if kind == "storage": self.c["storage"]["data_root"] += "/other"
                if kind == "unknown-setting": self.vendor.settings["radarr"]["futureVendorField"] = "operator edit"
                if kind == "managed-setting": self.vendor.settings["sonarr"]["renameEpisodes"] = True
                with self.assertRaisesRegex(SafetyError, "changed after the preview"):
                    self.apply(reviewed)
                self.assertFalse(self.vendor.writes)
        self.assertFalse(list(self.root.rglob("media-naming-*.json")))

    def test_tampered_hash_and_changes_are_not_authority(self):
        reviewed = self.plan()
        reviewed["services"]["radarr"]["changes"]["renameMovies"]["after"] = False
        with self.assertRaisesRegex(SafetyError, "changed after the preview"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)

    def test_owned_target_failure_prevents_vendor_or_backup_writes(self):
        reviewed = self.plan()
        self.guard.side_effect = SafetyError("Foreign application bind")
        with self.assertRaisesRegex(SafetyError, "Foreign application"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)
        self.assertFalse(list(self.root.rglob("media-naming-*.json")))

    def test_malformed_resource_or_example_cannot_be_accepted(self):
        original = copy.deepcopy(self.vendor.settings)
        for mutation in ("identity", "boolean", "missing", "examples"):
            with self.subTest(mutation=mutation):
                self.vendor.settings = copy.deepcopy(original)
                self.vendor.bad_examples = None
                if mutation == "identity": self.vendor.settings["radarr"]["id"] = 0
                if mutation == "boolean": self.vendor.settings["sonarr"]["renameEpisodes"] = "false"
                if mutation == "missing": self.vendor.settings["sonarr"].pop("seasonFolderFormat")
                if mutation == "examples": self.vendor.bad_examples = "   "
                with self.assertRaises(SafetyError): self.plan()
                self.assertFalse(self.vendor.writes)

    def test_ignored_put_and_unrelated_changes_fail_exact_readback(self):
        reviewed = self.plan()
        self.vendor.ignore_write = True
        with self.assertRaisesRegex(SafetyError, "exact reviewed naming"):
            self.apply(reviewed)
        self.assertEqual(len(self.vendor.writes), 1)

    def test_concurrent_other_app_change_is_not_overwritten(self):
        reviewed = self.plan()
        def change_other(service):
            if service == "sonarr": self.vendor.settings["radarr"]["futureVendorField"] = "operator edit"
        self.vendor.after_write = change_other
        with self.assertRaisesRegex(SafetyError, "exact reviewed naming"):
            self.apply(reviewed)
        self.assertEqual(len(self.vendor.writes), 1)
        self.assertEqual(self.vendor.settings["radarr"]["futureVendorField"], "operator edit")

    def test_partial_apply_requires_fresh_review_and_does_not_rewrite_completed_app(self):
        reviewed = self.plan()
        self.vendor.fail_service = "radarr"
        with self.assertRaisesRegex(SafetyError, "Starr naming request failed") as caught:
            self.apply(reviewed)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertTrue(self.vendor.settings["sonarr"]["renameEpisodes"])
        self.assertFalse(self.vendor.settings["radarr"]["renameMovies"])
        self.vendor.fail_service = None
        with self.assertRaisesRegex(SafetyError, "changed after the preview"):
            self.apply(reviewed)
        self.vendor.calls.clear()
        refreshed = self.plan()
        self.assertTrue(self.apply(refreshed)["ok"])
        self.assertEqual([call[1] for call in self.vendor.writes], ["radarr"])

    def test_conflicting_private_backup_refuses_before_vendor_write(self):
        reviewed = self.plan()
        directory = Path(self.c["install_root"]) / "backups"
        directory.mkdir()
        from samurai_stack.safety import write_json
        write_json(directory / ("media-naming-" + reviewed["review_hash"] + ".json"), {"foreign": True})
        with self.assertRaisesRegex(SafetyError, "backup conflicts"):
            self.apply(reviewed)
        self.assertFalse(self.vendor.writes)


if __name__ == "__main__":
    unittest.main()
