"""Locked NNTP facade, exact private retries and provider-only authorization."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import engine, enroll_sabnzbd as sab, onboarding as accounts, usenet
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json
from test_enroll_sabnzbd import CREDS, KEY, PROVIDER, Vendor


REAL_GUARD = accounts._guard


class UsenetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"], self.c["features"]["usenet"] = str(self.root), True
        for name in ("state", "secrets", "appdata"):
            (self.root / name).mkdir(mode=0o700)
        app = self.root / "appdata/sabnzbd"
        app.mkdir()
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        self.receipt_path = self.root / "state/onboarding.json"
        receipt = {"schema_version": 1, "deployment_id": self.c["deployment_id"], "host_id": "fixture-machine",
                   "bind_address": self.c["bind_address"], "apps": {sab.APP: {
                       "eligible": True, "state": "verified", "admin_verified": True, "directory": accounts._identity(app)}}}
        write_json(self.receipt_path, receipt)
        write_json(self.root / "secrets/onboarding_credentials.json", CREDS)
        self.vendor_operation = accounts._Enrollment(self.c, CREDS, receipt, None, None, lambda name: KEY)
        self.vendor_operation.state_path = self.receipt_path
        self.vendor = Vendor(self.vendor_operation)
        self.vendor.username, self.vendor.password = CREDS["admin_username"], CREDS["admin_password"]
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(accounts, "_guard"))
        # Ordinary Linux CI users exercise real private modes/link checks while
        # production still enforces UID 0 through these unmodified predicates.
        stack.enter_context(mock.patch.object(accounts, "_root_owned", return_value=True))
        stack.enter_context(mock.patch.object(engine, "host_identity", return_value="fixture-machine"))
        self.targets = stack.enter_context(mock.patch.object(accounts, "check_targets", side_effect=self.check_target))

    def check_target(self, config, apps, runner):
        self.assertEqual([sab.APP], apps)
        self.assertEqual(self.c, config)
        self.assertTrue((self.root / "state/apply.lock").is_file())
        self.assertTrue((self.root / "state/onboarding.lock").is_file())

    def call(self, function, *args, **kwargs):
        return function(self.c, *args, session_factory=self.vendor.session,
                        secret_resolver=lambda name: KEY, **kwargs)

    def receipt(self):
        return json.loads(self.receipt_path.read_text())

    def legacy(self, *, remove_shared=False):
        receipt = self.receipt()
        receipt["apps"][sab.APP].update(eligible=False, state="manual")
        receipt["apps"][sab.APP].pop("admin_verified", None)
        write_json(self.receipt_path, receipt, replace=True)
        if remove_shared:
            (self.root / "secrets/onboarding_credentials.json").unlink()

    def writes(self):
        return [form for _, _, form in self.vendor.calls if form and form.get("mode") == "set_config"]

    def test_providers_are_sanitized_readonly_without_shared_credentials(self):
        self.legacy(remove_shared=True)
        self.vendor.servers = [self.vendor.server()]
        before = {path.name: path.read_bytes() for path in (self.root / "secrets").iterdir()}
        receipt = self.receipt_path.read_bytes()
        result = self.call(usenet.providers)
        self.assertEqual("existing-provider", result[0]["name"])
        self.assertNotIn("password", json.dumps(result))
        self.assertNotIn("username", json.dumps(result))
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.root / "secrets").iterdir()})
        self.assertEqual(receipt, self.receipt_path.read_bytes())
        self.assertFalse(self.writes())
        self.targets.assert_called_once()
        self.assertFalse((self.root / "state/apply.lock").exists())
        self.assertFalse((self.root / "state/onboarding.lock").exists())

    def test_verify_uses_actual_stored_credentials_without_admin_pair(self):
        self.legacy(remove_shared=True)
        self.vendor.servers = [self.vendor.server()]
        result = self.call(usenet.verify)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["verified"])
        self.assertFalse(self.writes())
        self.assertFalse(any(path == "/login/" for _, path, _ in self.vendor.calls))

    def test_fresh_configure_saves_private_input_before_http_and_preserves_account(self):
        original_session = self.vendor.session

        def session(base):
            self.assertTrue((self.root / "secrets/usenet_provider.json").is_file())
            return original_session(base)

        self.vendor.session = session
        existing = self.vendor.server()
        self.vendor.servers = [copy.deepcopy(existing)]
        password = self.vendor.password
        result = self.call(usenet.configure, PROVIDER)
        self.assertTrue(result["connection_verified"])
        self.assertEqual(existing, self.vendor.servers[0])
        self.assertEqual(password, self.vendor.password)
        self.assertTrue(all(row["section"] == "servers" for row in self.writes()))
        path = self.root / "secrets/usenet_provider.json"
        self.assertEqual(PROVIDER, json.loads(path.read_text()))
        if os.name == "posix":
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(1, path.stat().st_nlink)

    def test_none_retries_exact_saved_provider_without_rewriting_secret_or_server(self):
        self.call(usenet.configure, PROVIDER)
        path = self.root / "secrets/usenet_provider.json"
        before = path.read_bytes(), path.stat().st_mtime_ns, copy.deepcopy(self.writes())
        result = self.call(usenet.configure)
        self.assertTrue(result["wizard_complete"])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns, self.writes()))

    def test_changed_provider_or_password_is_refused_without_http_or_overwrite(self):
        self.call(usenet.configure, PROVIDER)
        path = self.root / "secrets/usenet_provider.json"
        before = path.read_bytes()
        for change in ({"host": "other.example.test"}, {"password": PROVIDER["password"] + "-changed"}):
            self.vendor.calls.clear()
            with self.subTest(change=list(change)), self.assertRaisesRegex(SafetyError, "Saved NNTP provider differs"):
                self.call(usenet.configure, {**PROVIDER, **change})
            self.assertFalse(self.vendor.calls)
            self.assertEqual(before, path.read_bytes())

    def test_invalid_provider_is_not_saved_and_makes_no_http_request(self):
        with self.assertRaises(SafetyError):
            self.call(usenet.configure, {**PROVIDER, "ssl": False})
        self.assertFalse((self.root / "secrets/usenet_provider.json").exists())
        self.assertFalse(self.vendor.calls)

    def test_failed_nntp_test_retains_private_retry_but_creates_no_server(self):
        self.vendor.test_success = False
        with self.assertRaisesRegex(SafetyError, "connection or authentication test failed") as error:
            self.call(usenet.configure, PROVIDER)
        self.assertNotIn(PROVIDER["password"], str(error.exception))
        self.assertEqual([], self.vendor.servers)
        self.assertTrue((self.root / "secrets/usenet_provider.json").is_file())
        self.vendor.test_success = True
        self.assertTrue(self.call(usenet.configure)["connection_verified"])

    def test_explicit_correction_before_provider_intent_retains_exact_private_backup(self):
        wrong = {**PROVIDER, "host": "typo.example.test"}
        self.vendor.test_success = False
        with self.assertRaises(SafetyError):
            self.call(usenet.configure, wrong)
        path = self.root / "secrets/usenet_provider.json"
        # Preserve exact private bytes, including formatting and CRLF endings.
        original = (json.dumps(wrong, indent=4) + "\r\n").encode()
        path.write_bytes(original)
        account = self.vendor.username, self.vendor.password
        self.assertTrue(usenet.saved_provider(self.c)["safely_editable"])
        self.vendor.test_success = True
        result = self.call(usenet.configure, PROVIDER, replace_unverified=True)
        self.assertEqual(PROVIDER["host"], result["host"])
        backups = list((self.root / "backups").glob("usenet-provider-before-edit-*.json"))
        self.assertEqual(1, len(backups))
        self.assertEqual(original, backups[0].read_bytes())
        self.assertEqual(PROVIDER, json.loads(path.read_text()))
        self.assertEqual(account, (self.vendor.username, self.vendor.password))
        self.assertFalse(usenet.saved_provider(self.c)["safely_editable"])
        if os.name == "posix":
            self.assertEqual(0o600, backups[0].stat().st_mode & 0o777)
            self.assertEqual(0o700, backups[0].parent.stat().st_mode & 0o777)

    def test_verified_or_attempted_provider_input_cannot_use_correction_route(self):
        self.call(usenet.configure, PROVIDER)
        path = self.root / "secrets/usenet_provider.json"
        original = path.read_bytes()
        for flag in ("provider_intent", "provider_verified"):
            receipt = self.receipt()
            entry = receipt["apps"][sab.APP]
            entry["provider_intent"], entry["provider_verified"] = False, False
            entry[flag] = True
            write_json(self.receipt_path, receipt, replace=True)
            self.vendor.calls.clear()
            with self.subTest(flag=flag), self.assertRaisesRegex(SafetyError, "creation was already attempted"):
                self.call(usenet.configure, {**PROVIDER, "host": "corrected.example.test"}, replace_unverified=True)
            self.assertFalse(self.vendor.calls)
            self.assertEqual(original, path.read_bytes())
        self.assertFalse((self.root / "backups").exists())

    def test_real_deployment_named_server_blocks_correction_even_without_private_intent(self):
        self.vendor.test_success = False
        with self.assertRaises(SafetyError):
            self.call(usenet.configure, PROVIDER)
        path = self.root / "secrets/usenet_provider.json"
        original = path.read_bytes()
        name = "samurai-" + self.c["deployment_id"]
        self.vendor.servers = [self.vendor.server(name)]
        self.assertTrue(usenet.saved_provider(self.c)["safely_editable"])
        with self.assertRaisesRegex(SafetyError, "provider already exists"):
            self.call(usenet.configure, {**PROVIDER, "host": "corrected.example.test"}, replace_unverified=True)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([name], [row["name"] for row in self.vendor.servers])
        self.assertFalse((self.root / "backups").exists())

    def test_saved_provider_summary_is_local_and_excludes_all_credentials(self):
        self.assertIsNone(usenet.saved_provider(self.c))
        self.vendor.test_success = False
        with self.assertRaises(SafetyError):
            self.call(usenet.configure, PROVIDER)
        self.vendor.calls.clear()
        self.targets.reset_mock()
        summary = usenet.saved_provider(self.c)
        self.assertEqual({"host": PROVIDER["host"], "port": 563, "connections": 10, "safely_editable": True}, summary)
        for secret in ("username", "password", PROVIDER["username"], PROVIDER["password"]):
            self.assertNotIn(secret, json.dumps(summary))
        self.assertFalse(self.vendor.calls)
        self.targets.assert_not_called()

    def test_correction_requires_explicit_details_and_refuses_concurrent_private_edit(self):
        self.vendor.test_success = False
        with self.assertRaises(SafetyError):
            self.call(usenet.configure, PROVIDER)
        with self.assertRaisesRegex(SafetyError, "requires explicit new details"):
            self.call(usenet.configure, replace_unverified=True)
        path = self.root / "secrets/usenet_provider.json"
        replacement = json.dumps({**PROVIDER, "host": "administrator-edit.example.test"}).encode()
        api = sab._Api

        class ChangedInventory(api):
            def servers(inner):
                value = super().servers()
                path.write_bytes(replacement)
                return value

        with mock.patch.object(sab, "_Api", ChangedInventory):
            with self.assertRaisesRegex(SafetyError, "changed during review"):
                self.call(usenet.configure, {**PROVIDER, "host": "corrected.example.test"}, replace_unverified=True)
        self.assertEqual(replacement, path.read_bytes())
        self.assertFalse((self.root / "backups").exists())

    def test_target_failure_precedes_secret_or_http_and_releases_locks(self):
        self.targets.side_effect = SafetyError("Wrong target")
        with self.assertRaisesRegex(SafetyError, "Wrong target"):
            self.call(usenet.configure, PROVIDER)
        self.assertFalse(self.vendor.calls)
        self.assertFalse((self.root / "secrets/usenet_provider.json").exists())
        self.assertFalse((self.root / "state/apply.lock").exists())
        self.assertFalse((self.root / "state/onboarding.lock").exists())

    def test_production_root_guard_runs_before_target_or_secret_handling(self):
        with mock.patch.object(accounts, "_guard", side_effect=REAL_GUARD), \
             mock.patch.object(engine, "linux_only"), mock.patch.object(accounts.os, "geteuid", return_value=1000, create=True):
            with self.assertRaisesRegex(SafetyError, "requires root"):
                self.call(usenet.configure, PROVIDER)
        self.targets.assert_not_called()
        self.assertFalse(self.vendor.calls)

    def test_foreign_owned_private_state_is_rejected_before_http(self):
        with mock.patch.object(accounts, "_root_owned", return_value=False):
            with self.assertRaisesRegex(SafetyError, "owned by root"):
                self.call(usenet.providers)
        self.targets.assert_not_called()
        self.assertFalse(self.vendor.calls)

    def test_unknown_legacy_needs_login_even_with_unrelated_saved_shared_pair(self):
        self.legacy()
        self.assertTrue(usenet.needs_login(self.c))
        with self.assertRaisesRegex(SafetyError, "administrator username/password in the terminal"):
            self.call(usenet.configure, PROVIDER)
        self.assertFalse(self.vendor.calls)
        self.assertFalse(self.writes())

    def test_legacy_explicit_login_authorizes_only_provider_and_reuses_private_pair(self):
        self.legacy()
        pair = {"admin_username": "legacy-admin", "admin_password": "fixture-legacy-password"}  # release-audit: synthetic - existing legacy administrator fixture
        self.vendor.username, self.vendor.password = pair["admin_username"], pair["admin_password"]
        result = self.call(usenet.configure, PROVIDER, credentials=pair)
        self.assertTrue(result["connection_verified"])
        entry = self.receipt()["apps"][sab.APP]
        self.assertFalse(entry["eligible"])
        self.assertTrue(entry["provider_scope_authorized"])
        self.assertNotIn("admin_verified", entry)
        self.assertNotIn("admin_intent", entry)
        self.assertEqual("manual", entry["state"])
        self.assertEqual(pair, json.loads((self.root / "secrets/usenet_admin_credentials.json").read_text()))
        self.assertFalse(usenet.needs_login(self.c))
        count = len(self.writes())
        self.assertTrue(self.call(usenet.configure)["connection_verified"])
        self.assertEqual(count, len(self.writes()))
        self.assertTrue(all(row["section"] == "servers" for row in self.writes()))
        operation = accounts._Enrollment(self.c, pair, self.receipt(), None, self.vendor.session, lambda name: KEY)
        with self.assertRaisesRegex(SafetyError, "not reserved"):
            sab.enroll(operation)

    def test_incorrect_legacy_login_is_not_saved_and_never_changes_ui_or_provider(self):
        self.legacy(remove_shared=True)
        pair = {"admin_username": self.vendor.username, "admin_password": "fixture-incorrect-password"}  # release-audit: synthetic - negative existing administrator fixture
        password = self.vendor.password
        with self.assertRaisesRegex(SafetyError, "login could not be verified"):
            self.call(usenet.configure, PROVIDER, credentials=pair)
        self.assertFalse((self.root / "secrets/usenet_admin_credentials.json").exists())
        self.assertEqual(password, self.vendor.password)
        self.assertFalse(self.writes())

    def test_saved_provider_login_is_never_silently_replaced(self):
        self.legacy(remove_shared=True)
        pair = {name: CREDS[name] for name in ("admin_username", "admin_password")}
        self.call(usenet.configure, PROVIDER, credentials=pair)
        path = self.root / "secrets/usenet_admin_credentials.json"
        before = path.read_bytes()
        self.vendor.calls.clear()
        with self.assertRaisesRegex(SafetyError, "provider-authorization login differs"):
            self.call(usenet.configure, credentials={**pair, "admin_password": pair["admin_password"] + "-other"})
        self.assertFalse(self.vendor.calls)
        self.assertEqual(before, path.read_bytes())

    def test_needs_login_uses_only_local_private_state(self):
        self.assertFalse(usenet.needs_login(self.c))
        self.assertFalse(self.vendor.calls)
        self.targets.assert_not_called()
        (self.root / "secrets/onboarding_credentials.json").unlink()
        self.assertTrue(usenet.needs_login(self.c))

    def test_no_receipt_readonly_does_not_create_one_but_provider_auth_can_record_scope(self):
        self.receipt_path.unlink()
        self.legacy_shared_path = self.root / "secrets/onboarding_credentials.json"
        self.call(usenet.providers)
        self.assertFalse(self.receipt_path.exists())
        pair = {name: CREDS[name] for name in ("admin_username", "admin_password")}
        self.call(usenet.configure, PROVIDER, credentials=pair)
        entry = self.receipt()["apps"][sab.APP]
        self.assertFalse(entry["eligible"])
        self.assertTrue(entry["provider_scope_authorized"])

    def test_hardlinked_or_malformed_provider_secret_is_not_replaced(self):
        target = self.root / "secrets/usenet_provider.json"
        target.write_text("not-json\n")
        target.chmod(0o600)
        with self.assertRaisesRegex(SafetyError, "not valid JSON"):
            self.call(usenet.configure, PROVIDER)
        self.assertEqual("not-json\n", target.read_text())
        target.write_text(json.dumps(PROVIDER))
        link = self.root / "same-secret"
        os.link(target, link)
        with self.assertRaisesRegex(SafetyError, "without hard links"):
            self.call(usenet.configure, PROVIDER)
        self.assertFalse(self.vendor.calls)

    @unittest.skipUnless(os.name == "posix", "POSIX private-file permissions")
    def test_world_readable_provider_file_is_refused(self):
        target = self.root / "secrets/usenet_provider.json"
        target.write_text(json.dumps(PROVIDER))
        target.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, "accessible to other users"):
            self.call(usenet.configure, PROVIDER)
        self.assertFalse(self.vendor.calls)

    def test_disabled_usenet_does_not_prompt_or_contact_any_service(self):
        self.c["features"]["usenet"] = False
        for function in (usenet.providers, usenet.verify, usenet.configure):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(SafetyError, "Enable Usenet"):
                self.call(function)
        self.assertFalse(self.vendor.calls)
        self.targets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
