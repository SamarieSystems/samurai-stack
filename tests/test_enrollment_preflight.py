"""Existing-alpha absence proofs do not reset or write application accounts."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from samurai_stack.enrollment_preflight import claimable
from samurai_stack.onboarding import HttpResult
from samurai_stack.safety import SafetyError


KEY = "ab" * 16
MASKED = "*" * 8
SPACE = " "


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.settings = {"sonarr": {"username": "", "password": "", "unrelated": 42},
            "radarr": {"username": None, "password": None},
            "prowlarr": {"username": "", "password": None},
            "bazarr": {"auth": {"type": None, "username": "", "password": "", "apikey": KEY}},
            "sabnzbd": {"config": {"misc": {"username": "", "password": "", "other": "preserve"}}}}
        self.fail = False
        self.status = 200
        self.operation = SimpleNamespace(call=self.api, secret_resolver=lambda name: KEY,
            endpoints={"sabnzbd": "http://192.168.50.20:8080"}, session_factory=self.session,
            receipt={"apps": {"sonarr": {"eligible": True, "state": "reserved"}}}, credentials=None)

    def api(self, method, app, path, headers=None):
        self.calls.append((method, app, path, copy.deepcopy(headers)))
        if self.fail:
            raise RuntimeError("Vendor secret " + KEY)
        return copy.deepcopy(self.settings[app])

    def session(self, base):
        self.assertEqual(base, self.operation.endpoints["sabnzbd"])
        return SimpleNamespace(send=self.send)

    def send(self, method, path, form=None):
        self.calls.append((method, "sabnzbd", path, copy.deepcopy(form)))
        if self.fail:
            raise RuntimeError("Vendor secret " + KEY)
        return HttpResult(self.status, copy.deepcopy(self.settings["sabnzbd"]))

    def credentials(self, app):
        value = self.settings[app]
        return value["auth"] if app == "bazarr" else value["config"]["misc"] if app == "sabnzbd" else value

    def test_only_complete_explicit_blank_credentials_are_claimable(self):
        for app in self.settings:
            with self.subTest(app=app):
                self.assertIs(claimable(self.operation, app), True)
        for app in ("sonarr", "radarr", "prowlarr"):
            prefix = "/api/v1" if app == "prowlarr" else "/api/v3"
            self.assertIn(("GET", app, prefix + "/config/host", {"X-Api-Key": KEY}), self.calls)
        self.assertIn(("GET", "bazarr", "/api/system/settings", {"X-API-KEY": KEY}), self.calls)
        self.assertIn(("POST", "sabnzbd", "/api", {"mode": "get_config", "output": "json",
                      "apikey": KEY, "section": "misc"}), self.calls)

    def test_partial_accounts_masked_passwords_and_matching_names_are_not_absence(self):
        for app in self.settings:
            original = copy.deepcopy(self.credentials(app))
            for changes in ({"username": "existing-owner"}, {"password": MASKED},
                            {"username": "stackadmin", "password": MASKED},
                            {"username": SPACE}, {"password": SPACE}, {"username": False},
                            {"password": 0}, {"password": []}):
                with self.subTest(app=app, fields=list(changes)):
                    current = self.credentials(app)
                    current.clear()
                    current.update({**original, **changes})
                    self.assertIs(claimable(self.operation, app), False)
            self.credentials(app).clear()
            self.credentials(app).update(original)

    def test_missing_fields_never_count_as_blank(self):
        for app in self.settings:
            for name in ("username", "password"):
                current = self.credentials(app)
                original = current.pop(name)
                with self.subTest(app=app, field=name):
                    self.assertIs(claimable(self.operation, app), False)
                current[name] = original

    def test_null_is_accepted_only_for_explicit_starr_fields(self):
        for app in self.settings:
            current = self.credentials(app)
            current.update(username=None, password=None)
            with self.subTest(app=app):
                self.assertIs(claimable(self.operation, app), app in {"sonarr", "radarr", "prowlarr"})

    def test_bazarr_enabled_or_unknown_auth_mode_is_not_claimable(self):
        auth = self.settings["bazarr"]["auth"]
        for value in ("form", "basic", "none", "", False, [], {}):
            with self.subTest(mode=type(value).__name__):
                auth["type"] = value
                self.assertIs(claimable(self.operation, "bazarr"), False)
        auth.pop("type")
        self.assertIs(claimable(self.operation, "bazarr"), False)

    def test_multiuser_or_unsupported_apps_are_never_claimed_by_singleuser_proof(self):
        for app in ("jellyfin", "seerr", "anything", None):
            with self.subTest(app=app):
                self.assertIs(claimable(self.operation, app), False)
        self.assertFalse(self.calls)

    def test_transport_and_key_errors_are_sanitized_and_never_true(self):
        for app in self.settings:
            with self.subTest(app=app):
                self.fail = True
                with self.assertRaises(SafetyError) as failure:
                    claimable(self.operation, app)
                self.assertNotIn(KEY, str(failure.exception))
                self.assertNotIn("Vendor", str(failure.exception))
                self.fail = False
        with mock.patch.object(self.operation, "secret_resolver", side_effect=RuntimeError(KEY)):
            with self.assertRaises(SafetyError) as failure:
                claimable(self.operation, "bazarr")
            self.assertNotIn(KEY, str(failure.exception))
        for key in (None, "", " ", False, KEY + "\n", [KEY]):
            with self.subTest(key_type=type(key).__name__), mock.patch.object(self.operation, "secret_resolver", return_value=key):
                self.calls.clear()
                with self.assertRaises(SafetyError):
                    claimable(self.operation, "sonarr")
                self.assertFalse(self.calls)

    def test_unknown_schema_or_rejected_api_read_fails_closed(self):
        for app in self.settings:
            original = self.settings[app]
            for value in (None, [], "unsupported"):
                with self.subTest(app=app, shape=type(value).__name__):
                    self.settings[app] = value
                    with self.assertRaises(SafetyError):
                        claimable(self.operation, app)
            self.settings[app] = original
        self.settings["bazarr"]["auth"]["apikey"] = "cd" * 16
        with self.assertRaises(SafetyError):
            claimable(self.operation, "bazarr")
        for status in (401, 403, 500):
            self.status = status
            with self.assertRaises(SafetyError):
                claimable(self.operation, "sabnzbd")
        self.status = 200
        self.settings["sabnzbd"] = {"status": False, "error": KEY}
        with self.assertRaises(SafetyError) as failure:
            claimable(self.operation, "sabnzbd")
        self.assertNotIn(KEY, str(failure.exception))

    def test_readonly_state_no_account_or_media_operations_and_no_secrets_in_urls(self):
        before_settings, before_receipt = copy.deepcopy(self.settings), copy.deepcopy(self.operation.receipt)
        for app in self.settings:
            claimable(self.operation, app)
        self.assertEqual(self.settings, before_settings)
        self.assertEqual(self.operation.receipt, before_receipt)
        for method, app, path, values in self.calls:
            self.assertNotIn(KEY, path)
            self.assertNotIn("?", path)
            if method == "POST":
                self.assertEqual((app, path, values["mode"]), ("sabnzbd", "/api", "get_config"))
            else:
                self.assertEqual(method, "GET")
        self.assertNotIn(KEY, json.dumps(self.operation.receipt))


if __name__ == "__main__":
    unittest.main()
