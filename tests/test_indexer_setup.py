"""Stateful source intake contracts, private fixtures, no live services."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from samurai_stack import engine, indexer_setup as indexers, onboarding
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json

SOURCE_KEY = "fixture-indexer-api-key"  # release-audit: synthetic - indexer provider credential
PROWLARR_KEY = "fixture-prowlarr-api-key"  # release-audit: synthetic - local Prowlarr API credential


def template(implementation):
    return {"id": 0, "name": "Generic " + implementation, "implementation": implementation,
            "configContract": implementation + "Settings", "protocol": indexers.IMPLEMENTATIONS[implementation],
            "enable": True, "redirect": implementation == "Newznab", "supportsRedirect": True,
            "supportsRss": True, "supportsSearch": True, "appProfileId": 0, "priority": 25,
            "downloadClientId": 0, "tags": [], "presets": [], "capabilities": {"categories": []},
            "fields": [{"name": name, "value": value} for name, value in
                       (("baseUrl", ""), ("apiPath", "/api"), ("apiKey", ""), ("baseSettings.queryLimit", None))]}


def source_row(name="Existing source", row_id=1, implementation="Newznab"):
    row = template(implementation)
    row.update(id=row_id, name=name, appProfileId=1,
               capabilities={"categories": [{"id": 2000, "subCategories": []},
                                           {"id": 5000, "subCategories": [{"id": 5070, "subCategories": []}]}]})
    for field in row["fields"]:
        if field["name"] == "baseUrl":field["value"] = "https://existing.example.invalid"
        if field["name"] == "apiKey":field["value"] = SOURCE_KEY
    return row


class Vendor:
    def __init__(self):
        self.rows, self.calls = [], []
        self.schemas = [template(name) for name in indexers.IMPLEMENTATIONS]
        self.profiles = [{"id": 1, "name": "Standard", "enableRss": True,
                          "enableAutomaticSearch": True, "enableInteractiveSearch": True}]
        self.fail_tests = False
        self.unknown_test_response = False
        self.lose_create_response = False
        self.ignore_create = False
        self.poison_saved_key = False
        self.mask_keys = False
        self.edit_other_after_create = False

    def __call__(self, method, url, *, headers, timeout, json=None):
        self.calls.append((method, urlsplit(url).path, copy.deepcopy(json)))
        if headers != {"X-Api-Key": PROWLARR_KEY}:
            raise AssertionError("Only the local API credential goes in the API header")
        path = urlsplit(url).path.removeprefix("/api/v1")
        if method == "GET":
            if path == "/indexer":
                rows = copy.deepcopy(self.rows)
                if self.mask_keys:
                    for row in rows:
                        for field in row["fields"]:
                            if field["name"] == "apiKey":field["value"] = "********"
                return rows
            if path == "/indexer/schema":return copy.deepcopy(self.schemas)
            if path == "/appprofile":return copy.deepcopy(self.profiles)
        if method == "POST" and path == "/indexer/test":
            if self.fail_tests:raise RuntimeError("Vendor echo secret=" + SOURCE_KEY)
            return {"vendor_error": SOURCE_KEY} if self.unknown_test_response else {}
        if method == "POST" and path == "/indexer":
            if not self.ignore_create:
                row = copy.deepcopy(json)
                row.update(id=max((item["id"] for item in self.rows), default=0) + 1,
                           capabilities=copy.deepcopy(source_row()["capabilities"]))
                if self.poison_saved_key:
                    for field in row["fields"]:
                        if field["name"] == "apiKey":field["value"] += "/"
                self.rows.append(row)
                if self.edit_other_after_create:self.rows[0]["priority"] += 1
            if self.lose_create_response:
                self.lose_create_response = False
                raise RuntimeError("Timeout echo " + SOURCE_KEY)
            return copy.deepcopy(self.rows[-1]) if self.rows else {}
        raise AssertionError("Unexpected API operation")


class IndexerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.21")
        self.c["install_root"] = str(self.root)
        self.c["features"].update(usenet=True, torrents=False)
        for name in ("state", "secrets", "appdata"):
            (self.root / name).mkdir(mode=0o700)
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        self.enterContext(patch.object(onboarding, "_guard"))
        self.enterContext(patch.object(onboarding, "_root_owned", return_value=True))
        self.bind = self.enterContext(patch.object(engine, "check_container_ownership"))
        self.targets = self.enterContext(patch.object(onboarding, "check_targets"))
        self.enterContext(patch.object(engine, "host_identity", return_value="fixture-host"))
        self.vendor = Vendor()

    def options(self):
        return {"request": self.vendor, "secret_resolver": lambda name: PROWLARR_KEY}

    def save(self, **changes):
        return indexers.save_input(self.c, {"preset": "nzbgeek", "api_key": SOURCE_KEY, **changes})

    def plan(self, input_id=None):return indexers.plan(self.c, input_id, **self.options())
    def apply(self, preview):return indexers.apply(self.c, preview, **self.options())
    def verify(self):return indexers.verify(self.c, **self.options())
    def creates(self):return [row for row in self.vendor.calls if row[:2] == ("POST", "/api/v1/indexer")]
    def journal(self):return json.loads((self.root / "state/indexer-setup.json").read_text())

    def test_preset_private_input_and_safe_retry_inventory(self):
        input_id = self.save()
        path = self.root / "secrets" / ("indexer-input-" + input_id + ".json")
        self.assertEqual(json.loads(path.read_text())["source"]["base_url"], "https://api.nzbgeek.info")
        self.assertIn(SOURCE_KEY, path.read_text())
        if os.name == "posix":self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        choices = indexers.saved_inputs(self.c)
        self.assertEqual(choices[0]["input_id"], input_id)
        self.assertNotIn(SOURCE_KEY, json.dumps(choices))
        self.assertFalse(self.vendor.calls)

    def test_bad_or_credential_bearing_input_is_refused_before_private_publication(self):
        bad = [{"base_url": "http://example.invalid"}, {"base_url": "https://" + "fixture-user:" + "fixture-password@example.invalid"},
               {"base_url": "https://example.invalid?apikey=secret"}, {"base_url": "https://example.invalid/#secret"},
               {"base_url": "https://example.invalid:99999"}, {"api_path": "/../api"}, {"api_path": "/"},
               {"implementation": "Cardigann"}, {"api_key": "*" * 8}, {"api_key": SOURCE_KEY.replace("-", " ")},
               {"api_key": ""}, {"app_profile_id": True}, {"priority": 51}, {"label": "bad\nlabel"}]
        for change in bad:
            with self.subTest(change=list(change)), self.assertRaises(SafetyError):self.save(**change)
        self.assertFalse(list((self.root / "secrets").iterdir()))
        self.assertFalse(self.vendor.calls)

    def test_zero_or_disabled_sources_never_verify_and_do_not_write(self):
        self.assertFalse(self.verify()["ok"])
        self.assertEqual(self.verify()["missing_protocols"], ["usenet"])
        self.vendor.rows = [source_row()]
        self.vendor.rows[0]["enable"] = False
        self.vendor.calls.clear()
        report = self.verify()
        self.assertFalse(report["ok"])
        self.assertFalse(any(method == "POST" for method, _, _ in self.vendor.calls))
        self.assertFalse((self.root / "state/indexer-setup.json").exists())

    def test_existing_sources_are_tested_preserved_and_capabilities_reported(self):
        self.vendor.rows = [source_row()]
        before = copy.deepcopy(self.vendor.rows)
        report = self.verify()
        self.assertTrue(report["ok"], report)
        self.assertTrue(all(report["protocols"]["usenet"][key] for key in ("movies", "tv", "anime")))
        self.assertFalse(report["actual_download_tested"])
        self.assertEqual(self.vendor.rows, before)
        self.assertTrue(all(method == "GET" or path.endswith("/test") for method, path, _ in self.vendor.calls))
        self.assertNotIn(SOURCE_KEY, json.dumps(report))

    def test_parent_tv_category_does_not_claim_anime_and_music_is_not_media_ready(self):
        self.vendor.rows = [source_row()]
        self.vendor.rows[0]["capabilities"]["categories"][1]["subCategories"] = []
        self.assertTrue(self.verify()["ok"])
        self.assertFalse(self.verify()["protocols"]["usenet"]["anime"])
        self.vendor.rows[0]["capabilities"]["categories"] = [{"id": 3000}]
        report = self.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_categories"], ["movies", "tv"])

    def test_missing_protocol_and_disabled_automatic_search_are_pending(self):
        self.vendor.rows = [source_row()]
        self.c["features"]["torrents"] = True
        self.assertEqual(self.verify()["missing_protocols"], ["torrent"])
        self.c["features"]["torrents"] = False
        self.vendor.profiles[0]["enableAutomaticSearch"] = False
        self.assertEqual(self.verify()["sources"][0]["status"], "search-disabled")

    def test_failed_test_and_vendor_body_are_sanitized(self):
        self.vendor.rows = [source_row()]
        for attribute in ("fail_tests", "unknown_test_response"):
            with self.subTest(attribute=attribute):
                setattr(self.vendor, attribute, True)
                report = self.verify()
                self.assertFalse(report["ok"])
                self.assertEqual(report["sources"][0]["status"], "failed")
                self.assertNotIn(SOURCE_KEY, json.dumps(report))
                setattr(self.vendor, attribute, False)
        self.vendor.fail_tests = True
        with self.assertRaises(SafetyError) as error:self.plan(self.save())
        self.assertNotIn(SOURCE_KEY, str(error.exception))
        self.assertFalse(self.creates())

    def test_plan_tests_proposed_source_but_never_creates_or_exposes_credentials(self):
        preview = self.plan(self.save())
        self.assertEqual(preview["action"]["mode"], "create")
        self.assertFalse(self.creates())
        self.assertFalse((self.root / "state/indexer-setup.json").exists())
        self.assertNotIn(SOURCE_KEY, json.dumps(preview))
        self.assertNotIn(PROWLARR_KEY, json.dumps(preview))

    def test_additive_apply_and_retry_preserve_other_sources_without_duplicate_writes(self):
        self.vendor.rows = [source_row()]
        before = copy.deepcopy(self.vendor.rows)
        input_id = self.save()
        self.assertTrue(self.apply(self.plan(input_id))["ok"])
        self.assertEqual(self.vendor.rows[0], before[0])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.journal()["inputs"][input_id]["state"], "verified")
        retry = self.plan(input_id)
        self.assertEqual(retry["action"]["mode"], "verify-existing")
        self.assertTrue(self.apply(retry)["ok"])
        self.assertEqual(len(self.creates()), 1)
        self.assertFalse(any(method in {"PUT", "DELETE", "PATCH"} for method, _, _ in self.vendor.calls))

    def test_lost_create_response_resumes_from_intent_and_saved_identity(self):
        input_id = self.save()
        self.vendor.lose_create_response = True
        with self.assertRaises(SafetyError):self.apply(self.plan(input_id))
        self.assertEqual(self.journal()["inputs"][input_id]["state"], "intent")
        self.assertEqual(len(self.vendor.rows), 1)
        self.assertTrue(self.apply(self.plan(input_id))["ok"])
        self.assertEqual(len(self.creates()), 1)

    def test_untracked_name_and_endpoint_collisions_are_not_adopted_or_duplicated(self):
        input_id = self.save()
        self.vendor.rows = [source_row(name="Samurai homelab NZBGeek")]
        with self.assertRaisesRegex(SafetyError, "matching saved intent"):self.plan(input_id)
        self.vendor.rows[0]["name"] = "Unmanaged existing NZBGeek"
        for field in self.vendor.rows[0]["fields"]:
            if field["name"] == "baseUrl":field["value"] = "https://api.nzbgeek.info"
        with self.assertRaisesRegex(SafetyError, "endpoint already exists"):self.plan(input_id)
        self.assertFalse(self.creates())

    def test_deleted_verified_source_is_not_recreated(self):
        input_id = self.save()
        self.apply(self.plan(input_id))
        self.vendor.rows.clear()
        with self.assertRaisesRegex(SafetyError, "will not be recreated"):self.plan(input_id)
        self.assertEqual(len(self.creates()), 1)

    def test_ignored_create_and_changed_saved_key_fail_exact_readback(self):
        for attribute in ("ignore_create", "poison_saved_key"):
            with self.subTest(attribute=attribute):
                self.vendor.rows.clear()
                input_id = self.save(label=attribute)
                setattr(self.vendor, attribute, True)
                with self.assertRaisesRegex(SafetyError, "Saved indexer fields"):self.apply(self.plan(input_id))
                self.assertEqual(self.journal()["inputs"][input_id]["state"], "intent")
                setattr(self.vendor, attribute, False)

    def test_masked_stored_credentials_are_verified_by_existing_resource_id(self):
        input_id = self.save()
        self.vendor.mask_keys = True
        self.assertTrue(self.apply(self.plan(input_id))["ok"])
        self.assertTrue(self.apply(self.plan(input_id))["ok"])
        self.assertEqual(len(self.creates()), 1)

    def test_stale_host_config_sources_profiles_and_receipt_refuse_before_create(self):
        input_id = self.save()
        preview = self.plan(input_id)
        with patch.object(engine, "host_identity", return_value="different-host"):
            with self.assertRaisesRegex(SafetyError, "another host"):self.apply(preview)
        self.c["timezone"] = "Pacific/Honolulu"
        with self.assertRaisesRegex(SafetyError, "changed after review"):self.apply(preview)
        preview = self.plan(input_id)
        self.vendor.rows = [source_row()]
        with self.assertRaisesRegex(SafetyError, "changed after review"):self.apply(preview)
        preview = self.plan(input_id)
        self.vendor.profiles[0]["name"] = "Changed by operator"
        with self.assertRaisesRegex(SafetyError, "changed after review"):self.apply(preview)
        preview = self.plan(input_id)
        write_json(self.root / "state/indexer-setup.json", {"schema_version": 1, **indexers._binding(self.c),
                   "inputs": {}, "future_metadata": "changed"})
        with self.assertRaisesRegex(SafetyError, "changed after review"):self.apply(preview)
        self.assertFalse(self.creates())

    def test_other_source_changed_during_create_is_detected_without_rollback_deletion(self):
        self.vendor.rows = [source_row()]
        self.vendor.edit_other_after_create = True
        input_id = self.save()
        with self.assertRaisesRegex(SafetyError, "Other indexer settings changed"):self.apply(self.plan(input_id))
        self.assertEqual(len(self.vendor.rows), 2)
        self.assertEqual(self.journal()["inputs"][input_id]["state"], "intent")
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.vendor.calls))

    def test_generic_schema_bounds_allow_real_large_template_inventory_and_reject_ambiguity(self):
        self.vendor.schemas.extend({"name": "Other", "implementation": "Cardigann"} for _ in range(400))
        input_id = self.save()
        self.assertEqual(self.plan(input_id)["action"]["mode"], "create")
        self.vendor.schemas.append(template("Newznab"))
        with self.assertRaisesRegex(SafetyError, "unambiguous generic"):self.plan(input_id)

    def test_missing_template_field_and_unknown_resource_collection_fail_closed(self):
        input_id = self.save()
        self.vendor.schemas[0]["fields"].pop(1)
        with self.assertRaisesRegex(SafetyError, "required Newznab"):self.plan(input_id)
        self.vendor.rows = {"unexpected": []}
        with self.assertRaisesRegex(SafetyError, "unsupported source/profile"):self.verify()
        self.assertFalse(self.creates())

    def test_ambiguous_profiles_require_explicit_selection_without_profile_updates(self):
        second = {**self.vendor.profiles[0], "id": 2, "name": "Custom full search"}
        self.vendor.profiles.append(second)
        with self.assertRaisesRegex(SafetyError, "Choose one existing"):self.plan(self.save())
        preview = self.plan(self.save(app_profile_id=2))
        self.assertEqual(preview["action"]["app_profile_id"], 2)
        self.assertFalse(any(method in {"POST", "PUT"} and path.endswith("/appprofile")
                             for method, path, _ in self.vendor.calls))

    def test_torznab_has_explicit_protocol_and_does_not_change_vpn_or_site_config(self):
        self.c["features"].update(usenet=False, torrents=True)
        before = copy.deepcopy(self.c)
        input_id = indexers.save_input(self.c, {"implementation": "Torznab", "label": "Public feed",
            "base_url": "https://feed.example.invalid", "api_path": "/api", "api_key": ""})
        self.assertTrue(self.apply(self.plan(input_id))["ok"])
        self.assertEqual(self.vendor.rows[0]["protocol"], "torrent")
        self.assertEqual(self.c, before)

    def test_disabled_protocol_and_failed_owned_target_proof_make_no_vendor_write(self):
        self.c["features"]["usenet"] = False
        with self.assertRaisesRegex(SafetyError, "Enable this download protocol"):self.plan(self.save())
        self.vendor.calls.clear()
        self.bind.side_effect = SafetyError("Owned bind proof failed")
        with self.assertRaisesRegex(SafetyError, "Owned bind"):self.verify()
        self.assertFalse(self.vendor.calls)

    def test_private_retry_input_cannot_change_after_creation_intent(self):
        input_id = self.save()
        self.vendor.lose_create_response = True
        with self.assertRaises(SafetyError):self.apply(self.plan(input_id))
        path = self.root / "secrets" / ("indexer-input-" + input_id + ".json")
        value = json.loads(path.read_text()); value["source"]["priority"] = 10
        write_json(path, value, replace=True)
        with self.assertRaisesRegex(SafetyError, "changed after creation intent"):self.plan(input_id)


if __name__ == "__main__":
    unittest.main()
