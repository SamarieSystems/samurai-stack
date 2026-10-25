"""Legacy Jellyfin accounts are authenticated, never reset or adopted by flags."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from samurai_stack import enroll_jellyfin_existing as legacy, onboarding
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError
import test_onboarding as fixtures
from test_onboarding import ADMIN_ID, API_TOKEN, CREDS, VIEWER_ID, Vendor


class LegacyVendor(Vendor):
    def write(self, service, path):
        receipt = json.loads((Path(self.c["install_root"]) / "state" / "onboarding.json").read_text())
        entry = receipt["apps"][service]
        if path == "/Auth/Keys":
            assert entry["api_key_intent"]
        elif path == "/Startup/Complete":
            assert entry["legacy_complete_intent"]
        elif path == "/Users/New":
            assert entry["legacy_viewer_create_intent"] and entry["viewer_intent"]
        elif path.endswith("/Policy"):
            assert entry["viewer_policy_intent"]
        else:
            raise AssertionError("Existing-owner account mutation was not authorized")
        if self.fail_after == (service, path):
            self.fail_after = None
            raise RuntimeError("Lost response: " + CREDS["admin_password"])


class LegacyConnectTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.OnboardingTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.c, self.root = fixture.c, fixture.root
        receipt = fixture.reserve()
        receipt["apps"]["jellyfin"].update(eligible=False, state="manual")
        onboarding._credentials(self.c, CREDS)
        self.vendor = LegacyVendor(self.c)
        self.vendor.complete = True
        self.vendor.users[0].update(Name=CREDS["admin_username"], HasPassword=True, HasConfiguredPassword=True)
        self.vendor.passwords[CREDS["admin_username"]] = CREDS["admin_password"]
        self.operation = onboarding._Enrollment(self.c, CREDS, receipt, self.vendor, self.vendor.session, lambda name: "fixture-key")

    def connect(self, need_viewer=False):
        return legacy.connect(self.operation, need_viewer=need_viewer)

    def owner_writes(self):
        return [row for row in self.vendor.calls if row[0] in {"PUT", "POST"}
                and row[1].split(":8096")[-1] in {"/Startup/User", "/Users/" + ADMIN_ID + "/Policy", "/Users/" + ADMIN_ID + "/Password"}]

    def test_existing_admin_connects_and_key_saved_without_owner_changes(self):
        original = copy.deepcopy(self.vendor.users[0])
        detail = self.connect()
        self.assertIn("credentials and policies preserved", detail)
        self.assertEqual(self.vendor.users[0], original)
        self.assertFalse(self.owner_writes())
        self.assertEqual((self.root / "secrets" / "jellyfin_api_key").read_text().strip(), API_TOKEN)
        self.assertFalse(self.operation.receipt["apps"]["jellyfin"]["eligible"])

    def test_existing_password_with_incomplete_wizard_only_finishes_completion_flag(self):
        self.vendor.complete = False
        original = copy.deepcopy(self.vendor.users[0])
        self.connect()
        self.assertTrue(self.vendor.complete)
        self.assertEqual(original, self.vendor.users[0])
        self.assertFalse(self.owner_writes())
        self.assertTrue(self.operation.receipt["apps"]["jellyfin"]["legacy_complete_intent"])

    def test_lost_key_response_reuses_key_without_duplicate_creation(self):
        self.vendor.fail_after = ("jellyfin", "/Auth/Keys")
        with self.assertRaises(SafetyError):
            self.connect()
        self.connect()
        self.assertEqual(len(self.vendor.keys), 1)
        self.assertFalse(self.owner_writes())

    def test_lost_wizard_completion_response_resumes_without_repeating_it(self):
        self.vendor.complete = False
        self.vendor.fail_after = ("jellyfin", "/Startup/Complete")
        with self.assertRaises(SafetyError):
            self.connect()
        self.connect()
        self.assertEqual(sum(method == "POST" and url.endswith("/Startup/Complete")
                             for method, url, _ in self.vendor.calls), 1)
        self.assertFalse(self.owner_writes())

    def test_existing_matching_deployment_key_is_reused_after_admin_login(self):
        self.vendor.keys.append({"AppName": "Samurai Stack " + self.c["deployment_id"], "AccessToken": API_TOKEN})
        self.connect()
        self.assertEqual(len(self.vendor.keys), 1)
        self.assertFalse(any(method == "POST" and "/Auth/Keys?" in url for method, url, _ in self.vendor.calls))

    def test_existing_private_key_is_preserved_and_not_replaced_with_another_named_key(self):
        private = self.root / "secrets" / "jellyfin_api_key"
        private.write_text(API_TOKEN)
        private.chmod(0o600)
        self.connect()
        self.assertEqual(private.read_text(), API_TOKEN)
        self.assertFalse(self.vendor.keys)

    def test_existing_viewer_is_authenticated_without_any_policy_write(self):
        user = {"Id": VIEWER_ID, "Name": CREDS["viewer_username"],
                "Policy": {"IsAdministrator": False, "EnableContentDownloading": True}}
        self.vendor.users.append(copy.deepcopy(user))
        self.vendor.passwords[CREDS["viewer_username"]] = CREDS["viewer_password"]
        self.connect(True)
        self.assertEqual(self.vendor.users[-1], user)
        self.assertFalse(any(url.endswith("/Policy") for _, url, _ in self.vendor.calls))

    def test_new_viewer_gets_explicit_restrictions_and_durable_intent(self):
        self.connect(True)
        self.assertEqual(len(self.vendor.users), 2)
        for key, expected in onboarding.VIEWER_POLICY.items():
            self.assertEqual(self.vendor.users[-1]["Policy"][key], expected)
        self.assertTrue(self.operation.receipt["apps"]["jellyfin"]["legacy_viewer_create_intent"])
        self.assertFalse(self.owner_writes())

    def test_interrupted_new_viewer_resumes_without_recreating_it(self):
        self.vendor.fail_after = ("jellyfin", "/Users/New")
        with self.assertRaises(SafetyError):
            self.connect(True)
        self.connect(True)
        self.assertEqual(len(self.vendor.users), 2)
        self.assertFalse(self.owner_writes())

    def test_wrong_existing_admin_credentials_never_trigger_reset(self):
        self.operation.credentials = {**CREDS, "admin_password": "fixture-wrong-password"}  # release-audit: synthetic - invalid existing administrator login
        with self.assertRaises(SafetyError):
            self.connect()
        self.assertFalse(self.owner_writes())
        self.assertFalse(self.vendor.keys)


class PasswordlessSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"] = str(self.root)
        if os.name == "posix" and os.geteuid() != 0:
            self.c["puid"] = os.geteuid()
        self.db = self.root / "appdata" / "jellyfin" / "data" / "jellyfin.db"
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute('CREATE TABLE "Users" ("Id" TEXT PRIMARY KEY,"Username" TEXT,"Password" TEXT,"AuthenticationProviderId" TEXT)')
            database.execute('INSERT INTO "Users" VALUES (?,?,NULL,?)', (ADMIN_ID, "seed", legacy._DEFAULT_PROVIDER))

    def test_exact_single_null_or_empty_password_seed_returns_id_without_writes(self):
        before = self.db.read_bytes()
        self.assertEqual(legacy.passwordless_seed(self.c, "seed"), ADMIN_ID)
        self.assertEqual(self.db.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute('UPDATE "Users" SET "Password"=\'\'')
        self.assertEqual(legacy.passwordless_seed(self.c, "seed"), ADMIN_ID)

    def test_stored_hash_or_wrong_username_is_not_a_passwordless_seed(self):
        self.assertIsNone(legacy.passwordless_seed(self.c, "other"))
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute('UPDATE "Users" SET "Password"=?', ("fixture-opaque-hash",))
        self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))

    def test_active_wal_password_or_second_user_is_not_ignored(self):
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA wal_autocheckpoint=0")
            database.execute('UPDATE "Users" SET "Password"=?', ("fixture-wal-hash",))
            database.commit()
            self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))
            database.execute('UPDATE "Users" SET "Password"=NULL')
            database.execute('INSERT INTO "Users" VALUES (?,?,NULL,?)', (VIEWER_ID, "other", legacy._DEFAULT_PROVIDER))
            database.commit()
            self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))

    def test_external_provider_and_custom_database_path_are_not_claimed(self):
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute('UPDATE "Users" SET "AuthenticationProviderId"=?', ("fixture-external-provider",))
        self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))
        config = self.root / "appdata" / "jellyfin" / "config" / "database.xml"
        config.parent.mkdir()
        config.write_text("<DatabaseConfigurationOptions><DatabaseType>Jellyfin-SQLite</DatabaseType><CustomProviderOptions><Options><CustomDatabaseOption><Key>path</Key><Value>/other/database.db</Value></CustomDatabaseOption></Options></CustomProviderOptions></DatabaseConfigurationOptions>")
        self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))

    def test_missing_or_unknown_database_is_not_created_or_guessed(self):
        self.db.unlink()
        self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))
        self.assertFalse(self.db.exists())
        with closing(sqlite3.connect(self.db)) as database, database:
            database.execute('CREATE TABLE "Users" ("Id" TEXT PRIMARY KEY)')
        self.assertIsNone(legacy.passwordless_seed(self.c, "seed"))


if __name__ == "__main__":
    unittest.main()
