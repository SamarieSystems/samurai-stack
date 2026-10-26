"""Receipt-only restored-account authorization; ordinary enrollment stays strict."""
import copy
import hashlib
import json
from unittest import TestCase, mock

from samurai_stack import engine, onboarding, restore_accounts
from samurai_stack.safety import SafetyError, private_read, write_json
import test_onboarding as fixtures
from test_onboarding import CREDS, CORE_APPS


class RestoreTests(TestCase):
    # Reuse only the existing stateful vendor fixture, not inherited test cases.
    setUp = fixtures.OnboardingTests.setUp
    reserve = fixtures.OnboardingTests.reserve
    onboard = fixtures.OnboardingTests.onboard
    passwordless_seed = fixtures.OnboardingTests.passwordless_seed

    def prepare(self):
        self.reserve()
        self.assertTrue(self.onboard()["ok"])
        for app in CORE_APPS:
            directory = self.root / "appdata" / app
            (directory / "existing-state.bin").write_bytes(b"owned configured fixture")
            directory.rename(directory.with_name(app + "-original"))
            directory.mkdir()
            (directory / "existing-state.bin").write_bytes(b"owned configured fixture")
        self.vendor.calls.clear()
        self.guard = self.enterContext(mock.patch.object(onboarding, "check_targets"))
        self.bind_guard = self.enterContext(mock.patch.object(engine, "check_container_ownership"))

    def options(self):
        return {"request": self.vendor, "session_factory": self.vendor.session,
                "secret_resolver": lambda name: "fixture-api-key"}

    def plan(self):
        return restore_accounts.plan(self.c, **self.options())

    def apply(self, plan):
        return restore_accounts.apply(self.c, plan, **self.options())

    def receipt(self):
        return onboarding._private_json(self.root / "state/onboarding.json")

    def save(self, value):
        write_json(self.root / "state/onboarding.json", value, replace=True)

    def test_normal_load_still_refuses_changed_directory_before_explicit_restore(self):
        self.prepare()
        with self.assertRaisesRegex(SafetyError, "directory was replaced"):
            onboarding._load(self.c)
        self.assertEqual(len(self.plan()["changes"]), 4)
        with self.assertRaisesRegex(SafetyError, "directory was replaced"):
            onboarding._load(self.c)

    def test_explicit_restore_accepts_original_stable_filesystem_identity(self):
        identity = onboarding._identity
        filesystem = {"target": "/", "source": "UUID=fixture-app-filesystem", "fstype": "ext4"}
        with mock.patch.object(onboarding, "_identity", side_effect=lambda path, **kw: {
                **identity(path, **kw), "filesystem": filesystem}):
            self.prepare()
            preview = self.plan()
            self.assertEqual(len(preview["changes"]), len(CORE_APPS))
            self.assertTrue(all(row["old_directory"]["filesystem"] == filesystem for row in preview["changes"]))
            self.assertTrue(self.apply(preview)["ok"])
            self.assertTrue(restore_accounts.verify(self.c, **self.options())["ok"])

    def test_plan_is_readonly_apply_changes_only_three_receipt_fields_and_keeps_raw_backup(self):
        self.prepare()
        path = self.root / "state/onboarding.json"
        original = private_read(path)
        before = self.receipt()
        preview = self.plan()
        self.assertEqual(private_read(path), original)
        with mock.patch.object(onboarding._Enrollment, "mark", side_effect=AssertionError("Account writes forbidden")):
            result = self.apply(preview)
        self.assertTrue(result["ok"])
        expected = copy.deepcopy(before)
        for row in preview["changes"]:
            expected["apps"][row["app"]].update(directory=row["new_directory"], eligible=False, restore_verified=True)
        self.assertEqual(onboarding._load(self.c), expected)
        backups = list((self.root / "backups").glob("onboarding-before-restore-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(private_read(backups[0]), original)
        self.assertFalse(any(method in {"PUT", "DELETE"} or path.endswith("/Startup/User")
                             for method, path, _ in self.vendor.calls))
        self.assertEqual(self.plan()["changes"], [])
        self.assertTrue(restore_accounts.verify(self.c, **self.options())["ok"])

    def test_future_replacement_stays_strict_after_eligibility_is_removed(self):
        self.prepare(); self.apply(self.plan())
        directory = self.root / "appdata/sonarr"
        directory.rename(directory.with_name("sonarr-rebound")); directory.mkdir()
        with self.assertRaisesRegex(SafetyError, "directory was replaced"):
            onboarding._load(self.c)

    def test_crlf_receipt_hash_and_original_backup_preserve_exact_bytes(self):
        self.prepare()
        path = self.root / "state/onboarding.json"
        original = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        path.write_bytes(original)
        preview = self.plan()
        self.assertEqual(preview["receipt_sha256"], hashlib.sha256(original).hexdigest())
        self.apply(preview)
        backup = next((self.root / "backups").glob("onboarding-before-restore-*.json"))
        self.assertEqual(backup.read_bytes(), original)

    def test_verified_ineligible_accounts_never_regain_first_owner_authority(self):
        self.prepare(); self.apply(self.plan())
        with mock.patch("samurai_stack.enrollment_preflight.claimable", return_value=True) as claim:
            result = onboarding.claim_unconfigured(self.c, request=self.vendor,
                    session_factory=self.vendor.session, secret_resolver=lambda name: "fixture-api-key")
        self.assertEqual(result["claimed"], [])
        self.assertEqual(set(result["preserved"]), set(CORE_APPS))
        claim.assert_not_called()
        self.assertTrue(all(not row["eligible"] for row in self.receipt()["apps"].values()))

    def test_empty_directory_or_unverified_state_cannot_be_adopted(self):
        self.prepare()
        receipt = self.receipt();receipt["apps"]["sonarr"]["state"] = "intent";self.save(receipt)
        with self.assertRaisesRegex(SafetyError, "already verified"):
            self.plan()
        receipt["apps"]["sonarr"]["state"] = "verified";self.save(receipt)
        directory = self.root / "appdata/sonarr"
        directory.rename(directory.with_name("sonarr-restored"));directory.mkdir()
        with self.assertRaisesRegex(SafetyError, "directory is empty"):
            self.plan()

    def test_server_identity_or_saved_password_mismatch_refuses_before_backup(self):
        self.prepare()
        self.vendor.server_id = "another-server"
        with self.assertRaisesRegex(SafetyError, "server, account IDs"):
            self.plan()
        self.assertFalse((self.root / "backups").exists())
        self.vendor.server_id = "server-one"
        self.vendor.passwords[CREDS["viewer_username"]] = "different-existing-password"
        with self.assertRaises(SafetyError):
            self.plan()
        self.assertFalse((self.root / "backups").exists())

    def test_stale_receipt_and_tampered_plan_refused_before_backup(self):
        self.prepare();preview = self.plan()
        receipt = self.receipt();receipt["unrelated_future_field"] = "preserved";self.save(receipt)
        with self.assertRaisesRegex(SafetyError, "changed after review"):
            self.apply(preview)
        self.assertFalse((self.root / "backups").exists())
        preview = self.plan();preview["changes"] = []
        with self.assertRaisesRegex(SafetyError, "changed after review"):
            self.apply(preview)

    def test_changed_host_or_config_cannot_replay_identical_api_proof(self):
        self.prepare();preview = self.plan()
        with mock.patch.object(engine, "host_identity", return_value="other-host"):
            with self.assertRaisesRegex(SafetyError, "another host"):
                self.apply(preview)
        self.c["timezone"] = "Pacific/Honolulu"
        with self.assertRaisesRegex(SafetyError, "changed after review"):
            self.apply(preview)

    def test_directory_replaced_during_login_is_refused(self):
        self.prepare()
        original = restore_accounts._prove_accounts
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            directory = self.root / "appdata/sonarr"
            directory.rename(directory.with_name("sonarr-raced"));directory.mkdir()
            (directory / "state.bin").write_bytes(b"race")
            return result
        with mock.patch.object(restore_accounts, "_prove_accounts", side_effect=changing):
            with self.assertRaisesRegex(SafetyError, "changed during account verification"):
                self.plan()
        self.assertFalse((self.root / "backups").exists())

    def test_stale_live_bind_is_refused_before_any_account_request(self):
        self.prepare()
        self.bind_guard.side_effect = SafetyError("Live bind does not match host appdata")
        with self.assertRaisesRegex(SafetyError, "Live bind"):
            self.plan()
        self.assertFalse(self.vendor.calls)
        self.assertFalse((self.root / "backups").exists())

    def test_unsafe_viewer_and_unexpected_session_errors_are_sanitized(self):
        self.prepare()
        self.vendor.users[1]["Policy"]["EnableContentDeletion"] = True
        with self.assertRaisesRegex(SafetyError, "viewer policy differ"):
            self.plan()
        self.vendor.users[1]["Policy"]["EnableContentDeletion"] = False
        options = self.options();options["session_factory"] = mock.Mock(side_effect=RuntimeError("secret=never-print-this"))
        with self.assertRaises(SafetyError) as caught:
            restore_accounts.plan(self.c, **options)
        self.assertNotIn("never-print-this", str(caught.exception))

    def test_symlink_replacement_is_refused(self):
        self.prepare()
        directory = self.root / "appdata/sonarr"
        directory.rename(directory.with_name("sonarr-symlink-target"))
        try:directory.symlink_to(directory.with_name("sonarr-symlink-target"), target_is_directory=True)
        except OSError:self.skipTest("Directory symlink creation unavailable")
        with self.assertRaisesRegex(SafetyError, "symbolic link"):
            self.plan()
