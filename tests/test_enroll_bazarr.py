"""Bazarr v1.6.2 first-account and real form-session boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from samurai_stack import onboarding as accounts, enroll_bazarr
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json


CREDS = {"admin_username": "stackadmin", "admin_password": "fixture-bazarr-admin-password",  # release-audit: synthetic - disposable first-run administrator
         "viewer_username": "household", "viewer_password": "fixture-bazarr-viewer-password"}  # release-audit: synthetic - separate disposable viewer
KEY = "ab" * 16


def digest(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


class Vendor:
    def __init__(self, case):
        self.case, self.calls, self.writes = case, [], []
        self.auth = {"type": None, "username": "", "password": "", "apikey": KEY}
        self.other = {"providers": ["untouched"], "languages": ["untouched"], "sonarr": {"enabled": False}}
        self.lose_write_response = self.reject_write = self.ignore_write = False
        self.unsafe_anonymous = self.unsafe_bad = self.unsafe_api = self.bad_good = self.no_cookie = False
        self.boom = False
        self.readiness = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, copy.deepcopy(headers), copy.deepcopy(json)))
        if self.boom:
            raise RuntimeError("Vendor failure " + CREDS["admin_password"] + " " + KEY)
        assert method == "GET" and urlsplit(url).path == "/api/system/settings"
        assert headers == {"X-API-KEY": KEY}
        return {"auth": copy.deepcopy(self.auth), **copy.deepcopy(self.other)}

    def session(self, base):
        assert base == self.case.operation.endpoints["bazarr"]
        return Session(self)


class Session:
    def __init__(self, vendor):
        self.vendor, self.authorized = vendor, False

    def has_cookies(self):
        return self.authorized and not self.vendor.no_cookie

    def send(self, method, path, headers=None, form=None, timeout=20):
        v = self.vendor
        v.calls.append((method, path, copy.deepcopy(headers), copy.deepcopy(form)))
        if v.boom:
            raise RuntimeError("Transport failure " + CREDS["admin_password"] + " " + KEY)
        if method == "POST" and path == "/api/system/settings":
            assert headers == {"X-API-KEY": KEY}
            assert set(form) == {"settings-auth-type", "settings-auth-username", "settings-auth-password"}
            private = v.case.root / "secrets" / "onboarding_credentials.json"
            assert json.loads(private.read_text()) == CREDS
            entry = json.loads((v.case.root / "state" / "onboarding.json").read_text())["apps"]["bazarr"]
            assert entry["admin_intent"] is True and entry["state"] == "intent"
            v.writes.append(copy.deepcopy(form))
            if v.reject_write:
                return accounts.HttpResult(406, CREDS["admin_password"])
            if not v.ignore_write:
                v.auth.update(type=form["settings-auth-type"], username=form["settings-auth-username"],
                              password=digest(form["settings-auth-password"]))
            if v.lose_write_response:
                v.lose_write_response = False
                raise RuntimeError("Lost response " + CREDS["admin_password"])
            return accounts.HttpResult(204)
        if path == "/api/system/account":
            assert method == "POST" and headers is None and form["action"] == "login"
            self.authorized = ((form["username"] == v.auth["username"]
                and digest(form["password"]) == v.auth["password"] and not v.bad_good) or v.unsafe_bad)
            return accounts.HttpResult(204 if self.authorized else 403)
        if path == "/api/system/settings":
            assert method == "GET" and form is None
            if headers == {"X-API-KEY": KEY}:
                assert timeout <= 5
                if v.readiness:
                    result = v.readiness.pop(0)
                    if isinstance(result, Exception):
                        raise result
                    return result
                return accounts.HttpResult(200, {"auth": copy.deepcopy(v.auth), **copy.deepcopy(v.other)})
            assert headers is None
            return accounts.HttpResult(200 if v.unsafe_api else 401, {})
        assert path == "/" and method == "GET" and headers is None and form is None
        if v.auth["type"] == "basic" and not self.authorized:
            return accounts.HttpResult(401)
        page = {"baseUrl": "", "apiKey": KEY} if self.authorized or v.unsafe_anonymous else {"baseUrl": ""}
        return accounts.HttpResult(200, "<script>" + json.dumps(page) + "</script>")


class BazarrEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"] = str(self.root)
        for name in ("state", "secrets", "appdata"):
            (self.root / name).mkdir(mode=0o700)
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        self.enterContext(mock.patch.object(accounts, "_root_owned", return_value=True))
        self.receipt = {"schema_version": 1, "apps": {"bazarr": {"eligible": True, "state": "reserved"}}}
        write_json(self.root / "state" / "onboarding.json", self.receipt)
        accounts._credentials(self.c, CREDS)
        self.vendor = Vendor(self)
        self.operation = accounts._Enrollment(self.c, CREDS, self.receipt,
            self.vendor.request, self.vendor.session, lambda name: KEY)

    def enroll(self):
        return enroll_bazarr.enroll(self.operation)

    def initialized(self, *, username=None, password=None, mode="form"):
        self.vendor.auth.update(type=mode, username=username or CREDS["admin_username"],
            password=digest(password or CREDS["admin_password"]))

    def fake_clock(self):
        state = {"now": 0}
        self.enterContext(mock.patch.object(enroll_bazarr.time, "monotonic", side_effect=lambda: state["now"]))

        def advance(seconds):
            state["now"] += seconds

        sleep = self.enterContext(mock.patch.object(enroll_bazarr.time, "sleep", side_effect=advance))
        return state, sleep

    def test_real_api_readiness_precedes_durable_intent_and_the_single_settings_write(self):
        _, sleep = self.fake_clock()
        self.vendor.readiness = [
            SafetyError("Enrollment HTTP check failed; inspect the service locally"),
            accounts.HttpResult(503, "fixture-private-startup-message"), accounts.HttpResult(502)]
        self.enroll()
        first_write = next(index for index, row in enumerate(self.vendor.calls)
                           if row[0] == "POST" and row[1] == "/api/system/settings")
        before = self.vendor.calls[:first_write]
        self.assertEqual(4, len(before))
        self.assertTrue(all(row[:2] == ("GET", "/api/system/settings") for row in before))
        self.assertEqual(3, sleep.call_count)
        self.assertEqual(1, len(self.vendor.writes))
        self.assertTrue(self.receipt["apps"]["bazarr"]["admin_verified"])

    def test_readiness_deadline_is_bounded_and_never_creates_account_intent(self):
        clock, sleep = self.fake_clock()
        self.vendor.readiness = [accounts.HttpResult(503)] * 100
        before = copy.deepcopy(self.receipt)
        with self.assertRaisesRegex(accounts._Manual, "within 120 seconds"):
            self.enroll()
        self.assertEqual(120, clock["now"])
        self.assertEqual(60, sleep.call_count)
        self.assertFalse(self.vendor.writes)
        self.assertEqual(before, self.receipt)
        self.assertTrue(all(row[0] == "GET" for row in self.vendor.calls))

    def test_unauthorized_unsupported_or_unknown_readiness_stops_immediately(self):
        _, sleep = self.fake_clock()
        for status in (401, 403, 302, 400, 404, 429):
            with self.subTest(status=status):
                self.vendor.readiness = [accounts.HttpResult(status)]
                with self.assertRaises(accounts._Manual):
                    self.enroll()
                self.assertFalse(self.vendor.writes)
                sleep.assert_not_called()
        self.vendor.readiness = [accounts.HttpResult(200, {"auth": {**self.vendor.auth, "apikey": "cd" * 16}})]
        with self.assertRaisesRegex(accounts._Manual, "authentication settings are unknown"):
            self.enroll()
        sleep.assert_not_called()
        self.assertNotIn("admin_intent", self.receipt["apps"]["bazarr"])

    def test_nonconnection_safety_refusals_are_not_retried_as_startup(self):
        _, sleep = self.fake_clock()
        self.vendor.readiness = [SafetyError("Enrollment HTTP response exceeded the safe size limit")]
        with self.assertRaises(SafetyError):
            self.enroll()
        self.assertEqual(1, len(self.vendor.calls))
        sleep.assert_not_called()
        self.assertFalse(self.vendor.writes)

    def test_existing_inspection_waits_for_api_without_account_or_credential_writes(self):
        _, sleep = self.fake_clock()
        self.initialized()
        self.operation.credentials = None
        self.vendor.readiness = [accounts.HttpResult(503),
            SafetyError("Enrollment HTTP check failed; inspect the service locally")]
        before = copy.deepcopy(self.receipt)
        detail = enroll_bazarr.inspect_existing(self.operation)
        self.assertIn("Existing Bazarr", detail)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(before, self.receipt)
        self.assertFalse(self.vendor.writes)
        self.assertTrue(all(row[0] == "GET" for row in self.vendor.calls))

    def test_new_account_uses_only_three_auth_fields_after_durable_intent(self):
        before = copy.deepcopy(self.vendor.other)
        detail = self.enroll()
        self.assertIn("form login verified", detail)
        self.assertEqual(len(self.vendor.writes), 1)
        self.assertEqual(self.vendor.auth["username"], CREDS["admin_username"])
        self.assertEqual(self.vendor.auth["password"], digest(CREDS["admin_password"]))
        self.assertEqual(self.vendor.other, before)
        self.assertEqual((self.root / "secrets" / "bazarr_api_key").read_text(), KEY + "\n")
        self.assertTrue(self.receipt["apps"]["bazarr"]["admin_verified"])
        self.assertEqual(self.receipt["apps"]["bazarr"]["state"], "verified")
        self.assertTrue(all("?" not in row[1] for row in self.vendor.calls))
        self.assertTrue(all(CREDS["admin_password"] not in row[1] and KEY not in row[1] for row in self.vendor.calls))

    def test_repeat_verifies_without_settings_writes_or_key_replacement(self):
        self.enroll()
        path = self.root / "secrets" / "bazarr_api_key"
        before = path.stat().st_mtime_ns
        self.enroll()
        self.assertEqual(len(self.vendor.writes), 1)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_initialized_matching_account_verified_readonly(self):
        self.initialized()
        self.enroll()
        self.assertFalse(self.vendor.writes)
        self.assertTrue(self.receipt["apps"]["bazarr"]["existing_account"])
        self.assertNotIn("admin_intent", self.receipt["apps"]["bazarr"])

    def test_initialized_other_name_wrong_password_or_basic_auth_never_reset(self):
        for kwargs in ({"username": "someoneelse"}, {"password": "fixture-different-bazarr-password"}, {"mode": "basic"}):  # release-audit: synthetic - negative credential fixture
            with self.subTest(fields=list(kwargs)):
                self.initialized(**kwargs)
                before = copy.deepcopy(self.vendor.auth)
                with self.assertRaises(accounts._Manual):
                    self.enroll()
                self.assertEqual(self.vendor.auth, before)
                self.assertFalse(self.vendor.writes)

    def test_lost_write_response_resumes_with_saved_login_and_no_second_write(self):
        self.vendor.lose_write_response = True
        with self.assertRaises(SafetyError):
            self.enroll()
        self.assertEqual(self.receipt["apps"]["bazarr"]["state"], "intent")
        self.enroll()
        self.assertEqual(len(self.vendor.writes), 1)
        self.assertEqual(self.receipt["apps"]["bazarr"]["state"], "verified")

    def test_unreached_write_intent_may_resume_only_wholly_blank_auth(self):
        self.operation.mark("bazarr", admin_intent=True, state="intent")
        self.enroll()
        self.assertEqual(len(self.vendor.writes), 1)

    def test_verified_missing_account_never_recreated(self):
        self.operation.mark("bazarr", state="verified", admin_intent=True)
        with self.assertRaisesRegex(accounts._Manual, "no replacement"):
            self.enroll()
        self.assertFalse(self.vendor.writes)

    def test_partial_or_malformed_auth_is_not_a_fresh_account(self):
        cases = [{"username": "someoneelse"}, {"password": digest("fixture-unrelated-password")},  # release-audit: synthetic - initialized account evidence
                 {"type": "form"}, {"type": []}, {"username": None}, {"apikey": "cd" * 16}]
        for changes in cases:
            with self.subTest(fields=list(changes)):
                self.vendor.auth = {"type": None, "username": "", "password": "", "apikey": KEY, **changes}
                with self.assertRaises(SafetyError):
                    self.enroll()
                self.assertFalse(self.vendor.writes)

    def test_anonymous_bad_password_api_or_cookie_failure_cannot_verify(self):
        self.initialized()
        for name in ("unsafe_anonymous", "unsafe_bad", "unsafe_api", "bad_good", "no_cookie"):
            with self.subTest(failure=name):
                setattr(self.vendor, name, True)
                with self.assertRaises(accounts._Manual):
                    self.enroll()
                setattr(self.vendor, name, False)
        self.assertFalse(self.vendor.writes)
        self.assertFalse((self.root / "secrets" / "bazarr_api_key").exists())
        self.assertNotEqual(self.receipt["apps"]["bazarr"]["state"], "verified")

    def test_rejected_or_ignored_write_is_not_green(self):
        for name in ("reject_write", "ignore_write"):
            with self.subTest(failure=name):
                setattr(self.vendor, name, True)
                with self.assertRaises(accounts._Manual):
                    self.enroll()
                setattr(self.vendor, name, False)
        self.assertFalse((self.root / "secrets" / "bazarr_api_key").exists())

    def test_existing_private_key_is_preserved_on_mismatch(self):
        self.initialized()
        path = self.root / "secrets" / "bazarr_api_key"
        write_json(path, {"unrelated": "preserve"})
        before = path.read_bytes()
        with self.assertRaisesRegex(accounts._Manual, "no key or private file was replaced"):
            self.enroll()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.vendor.writes)

    def test_no_reservation_refuses_before_any_request(self):
        self.receipt["apps"]["bazarr"]["eligible"] = False
        with self.assertRaises(accounts._Manual):
            self.enroll()
        self.assertFalse(self.vendor.calls)

    def test_vendor_error_and_success_detail_never_leak_secrets(self):
        self.vendor.boom = True
        with self.assertRaises(SafetyError) as failure:
            self.enroll()
        self.assertNotIn(CREDS["admin_password"], str(failure.exception))
        self.assertNotIn(KEY, str(failure.exception))
        self.vendor.boom = False
        detail = self.enroll()
        for value in (KEY, CREDS["admin_password"], CREDS["viewer_password"]):
            self.assertNotIn(value, detail)
            self.assertNotIn(value, json.dumps(self.receipt))

    def test_existing_inspection_requires_no_credentials_or_reservation_and_writes_nothing(self):
        self.operation.credentials = None
        self.receipt["apps"] = {}
        for mode in ("form", "basic"):
            with self.subTest(mode=mode):
                self.initialized(username="existing-owner", password="fixture-existing-owner-password", mode=mode)  # release-audit: synthetic - preserve unrelated existing account
                before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                detail = enroll_bazarr.inspect_existing(self.operation)
                self.assertIn("Existing Bazarr UI authentication", detail)
                self.assertNotIn("form login verified", detail)
                self.assertFalse(self.vendor.writes)
                after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
                self.assertFalse(any(row[0] != "GET" for row in self.vendor.calls))

    def test_existing_inspection_leaves_uninitialized_or_unsafe_state_pending(self):
        with self.assertRaisesRegex(accounts._Manual, "no initialized UI authentication"):
            enroll_bazarr.inspect_existing(self.operation)
        self.initialized()
        for name in ("unsafe_anonymous", "unsafe_api"):
            with self.subTest(failure=name):
                setattr(self.vendor, name, True)
                with self.assertRaises(accounts._Manual):
                    enroll_bazarr.inspect_existing(self.operation)
                setattr(self.vendor, name, False)
        self.assertFalse(self.vendor.writes)
        self.assertFalse((self.root / "secrets" / "bazarr_api_key").exists())

    def test_existing_inspection_sanitizes_vendor_errors(self):
        self.vendor.boom = True
        with self.assertRaises(SafetyError) as failure:
            enroll_bazarr.inspect_existing(self.operation)
        self.assertNotIn(KEY, str(failure.exception))
        self.assertNotIn(CREDS["admin_password"], str(failure.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX private mode contract")
    def test_generated_key_copy_is_private(self):
        self.enroll()
        path = self.root / "secrets" / "bazarr_api_key"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_nlink, 1)


if __name__ == "__main__":
    unittest.main()
