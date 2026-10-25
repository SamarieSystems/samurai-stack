"""The management phase uses backend enrollment without browser wizards."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from samurai_stack import engine, management_setup, onboarding, setup
from samurai_stack.cli import default_config
from samurai_stack.safety import write_json


class ManagementGuidedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = self.root / "app"; self.app.mkdir()
        (self.app / "secrets").mkdir()
        self.c = default_config()
        self.c["install_root"] = self.app.as_posix().removeprefix(self.app.drive)
        self.output = []
        self.ui = setup.GuidedSetup(self.root, self.root / "site.json", input_fn=Mock(return_value="yes"),
            output_fn=self.output.append, state_base=self.root / "state", registry_root=self.root / "registry")
        self.enterContext(patch.object(self.ui, "config", return_value=self.c))
        self.install = self.enterContext(patch.object(self.ui, "install", return_value=True))
        self.enterContext(patch.object(self.ui, "endpoints"))
        self.wire = self.enterContext(patch.object(self.ui, "wire", return_value=True))
        self.enterContext(patch.object(self.ui, "_state_dir", return_value=self.root))
        self.document = {"review_hash": "a" * 64, "apps": {name: {"action": "create_first_administrator"} for name in management_setup.SERVICES}}
        self.plan = self.enterContext(patch.object(management_setup, "plan", return_value=self.document))
        self.apply = self.enterContext(patch.object(management_setup, "apply", return_value={"ok": True}))
        self.attest = self.enterContext(patch.object(engine, "attest"))

    def test_fresh_accounts_reuse_saved_media_admin_without_browser_or_second_login_prompt(self):
        value = {"admin_username": "fixture-admin", "admin_password": "fixture-private-admin"}  # release-audit: synthetic - private management onboarding fixture
        write_json(self.app / "secrets/onboarding_credentials.json", value)
        with patch.object(onboarding, "_root_owned", return_value=True), \
             patch.object(self.ui, "account_name", side_effect=AssertionError("No duplicate username prompt")), \
             patch.object(self.ui, "private_input", side_effect=AssertionError("No duplicate password prompt")):
            self.assertTrue(self.ui.management())
        self.assertEqual(self.apply.call_args.args[3], {"username": value["admin_username"], "password": value["admin_password"]})
        self.wire.assert_called_once_with(4)
        self.attest.assert_not_called()
        self.assertNotIn(value["admin_password"], " ".join(self.output))
        self.assertTrue(any("not configured or delivery-tested" in row for row in self.output))

    def test_known_management_credentials_resume_without_reasking_or_manual_attestation(self):
        write_json(self.app / "secrets/management_credentials.json", {"fixture": True})
        self.document["apps"] = {name: {"action": "verify_existing_login"} for name in management_setup.SERVICES}
        with patch.object(self.ui, "account_name", side_effect=AssertionError("prompt")), \
             patch.object(self.ui, "private_input", side_effect=AssertionError("prompt")):
            self.assertTrue(self.ui.management())
        self.assertIsNone(self.apply.call_args.args[3]); self.attest.assert_not_called()
        self.ui.read.assert_not_called()

    def test_unknown_existing_owner_requests_hidden_current_password_and_never_resets(self):
        self.document["apps"] = {name: {"action": "verify_existing_login"} for name in management_setup.SERVICES}
        with patch.object(self.ui, "account_name", return_value="fixture-admin"), \
             patch.object(self.ui, "private_input", side_effect=["fixture-pass", "fixture-pass"]):
            self.assertTrue(self.ui.management())
        self.assertEqual(self.apply.call_args.args[3]["password"], "fixture-pass")
        self.assertNotIn("fixture-pass", " ".join(self.output))
        self.attest.assert_not_called()

    def test_decline_keeps_accounts_and_credentials_untouched(self):
        self.ui.read.return_value = "no"
        with patch.object(self.ui, "account_name", return_value="fixture-admin"), \
             patch.object(self.ui, "private_input", side_effect=["fixture-pass", "fixture-pass"]):
            self.assertFalse(self.ui.management())
        self.apply.assert_not_called(); self.wire.assert_not_called()
        self.assertFalse(list((self.app / "secrets").iterdir()))

    def test_pause_hidden_password_has_no_backend_write(self):
        with patch.object(self.ui, "account_name", return_value="fixture-admin"), \
             patch.object(self.ui, "private_input", return_value=None):
            self.assertFalse(self.ui.management())
        self.apply.assert_not_called(); self.attest.assert_not_called()

    def test_ten_character_password_is_accepted_after_repeat_without_logging(self):
        with patch.object(self.ui, "account_name", return_value="fixture-admin"), \
             patch.object(self.ui, "private_input", side_effect=["test10pass!", "test10pass!"]):
            self.assertTrue(self.ui.management())
        self.assertEqual(self.apply.call_args.args[3]["password"], "test10pass!")
        self.assertNotIn("test10pass!", " ".join(self.output))


if __name__ == "__main__":
    unittest.main()
