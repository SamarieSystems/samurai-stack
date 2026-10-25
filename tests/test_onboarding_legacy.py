"""Historical fresh reservations do not authorize replacing initialized owners."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import urlsplit

from samurai_stack import onboarding as accounts
from samurai_stack.safety import SafetyError
import test_onboarding as fixtures
from test_onboarding import ADMIN_ID, CREDS, Vendor


class MixedVendor(Vendor):
    """A manually initialized Jellyfin plus still-blank Starr applications."""
    def write(self, service, path):
        if service != "jellyfin":
            return super().write(service, path)
        if path == "/Auth/Keys":
            receipt = json.loads((Path(self.c["install_root"]) / "state" / "onboarding.json").read_text())
            assert receipt["apps"][service]["api_key_intent"] is True
            return
        raise AssertionError("An existing Jellyfin account or policy must never be mutated")


class HistoricalReservationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.OnboardingTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.c, self.root = fixture.c, fixture.root
        fixture.reserve()
        self.vendor = MixedVendor(self.c)
        self.vendor.complete = True
        self.vendor.users[0].update(Name=CREDS["admin_username"], HasPassword=True, HasConfiguredPassword=True)
        self.vendor.passwords = {CREDS["admin_username"]: CREDS["admin_password"]}

    def onboard(self, credentials=CREDS):
        with mock.patch.object(accounts, "check_targets"):
            return accounts.onboarding(self.c, credentials, request=self.vendor,
                session_factory=self.vendor.session, secret_resolver=lambda name: "fixture-api-key")

    @staticmethod
    def settings_write(row):
        method, url, body = row
        return method in {"POST", "PUT", "DELETE", "PATCH"} and urlsplit(url).path != "/Users/AuthenticateByName"

    def test_wrong_existing_owner_login_does_not_freeze_pair_or_initialize_other_apps(self):
        self.vendor.users[0]["Name"] = "existing-owner"
        self.vendor.passwords = {"existing-owner": "fixture-unrelated-existing-owner-password"}  # release-audit: synthetic - unrelated already initialized owner
        before = copy.deepcopy(self.vendor.users)
        try:
            report = self.onboard()
        except SafetyError:
            report = None
        if report is not None:
            self.assertTrue(report["pending"] or report["errors"])
        self.assertEqual(self.vendor.users, before)
        self.assertFalse(any(self.settings_write(row) for row in self.vendor.calls))
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())
        receipt = json.loads((self.root / "state" / "onboarding.json").read_text())
        self.assertNotIn("identity", receipt)

    def test_historical_eligible_owner_is_authenticated_before_other_account_creation(self):
        original = copy.deepcopy(self.vendor.users[0])
        report = self.onboard()
        self.assertFalse(report["pending"], report)
        self.assertFalse(report["errors"], report)
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.vendor.users[0], original)
        self.assertFalse(any(method in {"POST", "PUT"} and urlsplit(url).path in
            {"/Startup/User", "/Startup/Complete", "/Users/" + ADMIN_ID + "/Policy"}
            for method, url, body in self.vendor.calls))
        first_login = next(index for index, row in enumerate(self.vendor.calls)
                           if urlsplit(row[1]).path == "/Users/AuthenticateByName")
        first_new_account = next(index for index, row in enumerate(self.vendor.calls) if row[0] == "PUT")
        self.assertLess(first_login, first_new_account)
        receipt = json.loads((self.root / "state" / "onboarding.json").read_text())
        self.assertTrue(receipt["apps"]["jellyfin"]["eligible"])
        self.assertTrue(receipt["apps"]["jellyfin"]["legacy_connection_verified"])

    def test_initialized_owner_resume_does_not_reenter_first_run_or_rewrite_other_accounts(self):
        first = self.onboard()
        self.assertTrue(first["ok"], first)
        before = sum(self.settings_write(row) for row in self.vendor.calls)
        result = self.onboard(None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(sum(self.settings_write(row) for row in self.vendor.calls), before)

    def test_starr_recheck_requires_explicit_credential_fields_before_any_write(self):
        receipt = json.loads((self.root / "state" / "onboarding.json").read_text())
        accounts._credentials(self.c, CREDS)
        for omitted in ("username", "password"):
            with self.subTest(omitted=omitted):
                calls = []
                def request(method, url, **kwargs):
                    calls.append(method)
                    if method == "GET":
                        return {key: value for key, value in self.vendor.starr["sonarr"].items() if key != omitted}
                    return self.vendor(method, url, **kwargs)
                operation = accounts._Enrollment(self.c, CREDS, receipt, request,
                    self.vendor.session, lambda name: "fixture-api-key")
                with self.assertRaises(SafetyError):
                    operation.starr("sonarr")
                self.assertNotIn("PUT", calls)


if __name__ == "__main__":
    unittest.main()
