"""SAB API/UI enrollment and NNTP contracts with no infrastructure changes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import enroll_sabnzbd as sab, onboarding as accounts
from samurai_stack.cli import default_config
from samurai_stack.safety import write_json


CREDS = {"admin_username": "stackadmin", "admin_password": "fixture-admin-password",  # release-audit: synthetic - disposable account password
         "viewer_username": "household", "viewer_password": "fixture-viewer-password"}  # release-audit: synthetic - disposable viewer password
PROVIDER = {"host": "news.example.test", "port": 563, "username": "fixture-nntp-user",
            "password": "fixture-nntp-password", "connections": 10, "ssl": True}  # release-audit: synthetic - disposable NNTP password
KEY = "a" * 32


class Vendor:
    def __init__(self, operation):
        self.operation, self.calls = operation, []
        self.username, self.password = "", ""
        self.html_login, self.version, self.servers = True, sab.VERSION, []
        self.fail_after, self.bad_good_login, self.anonymous, self.accept_bad = None, False, False, False
        self.test_success, self.stored_success, self.foreign_redirect = True, True, False
        self.bad_test_shape, self.wizard_stuck = False, False
        self.inject_password_after_username = False
        self.observed_intents = []

    def session(self, base):
        assert base == self.operation.endpoints[sab.APP]
        return Session(self)

    def intent(self, **required):
        saved = json.loads(self.operation.state_path.read_text())["apps"][sab.APP]
        for name, value in required.items():
            assert saved.get(name) == value, "Every write requires prior durable intent"
        self.observed_intents.append(copy.deepcopy(saved))

    def server(self, name="existing-provider", **values):
        return {"name": name, "host": "old.example.test", "port": 563, "username": "fixture-existing-user",
                "password": "*" * 10, "ssl": True, "ssl_verify": 3, "enable": True, "connections": 5,
                "priority": 1, "notes": "preserve this provider", **values}


class Session:
    def __init__(self, vendor):
        self.vendor, self.authorized, self.cookies = vendor, False, False

    def has_cookies(self):
        return self.cookies

    def send(self, method, path, *, form=None, **kwargs):
        v = self.vendor
        assert "?" not in path and "#" not in path
        v.calls.append((method, path, copy.deepcopy(form)))
        if path == "/api":
            assert method == "POST" and form["output"] == "json" and form["apikey"] == KEY
            mode = form["mode"]
            if mode == "version":
                return accounts.HttpResult(200, {"version": v.version})
            if mode == "get_config":
                if form["section"] == "misc":
                    return accounts.HttpResult(200, {"config": {"misc": {
                        "username": v.username, "password": "*" * 10 if v.password else "",
                        "html_login": v.html_login, "unrelated": "preserve"}}})
                return accounts.HttpResult(200, {"config": {"servers": copy.deepcopy(v.servers)}})
            if mode == "test_server":
                # Actual SAB 5.1.3 rejects this unsupported top-level mode.
                return accounts.HttpResult(200, {"status": False, "error": "not implemented"})
            if mode == "config":
                assert form["name"] == "test_server"
                assert form["timeout"] == 10
                stored = "server" in form
                if stored:
                    row = next(row for row in v.servers if row["name"] == form["server"])
                    assert form["password"] == ("******" if row["password"] else "")
                success = v.stored_success if stored else v.test_success
                result = {"result": success, "message": "fixture-private-nntp-message"}
                return accounts.HttpResult(200, result if v.bad_test_shape else {"value": result})
            assert mode == "set_config"
            keyword = form["keyword"]
            if form["section"] == "misc":
                required = {"admin_intent": True}
                if keyword in {"username", "password"}:
                    required[keyword + "_write_intent"] = True
                else:
                    assert keyword == "html_login"
                    required["login_mode_intent"] = True
                v.intent(**required)
                if keyword == "password":
                    assert not v.password, "Never overwrite any existing password"
                setattr(v, keyword, form["value"])
                if keyword == "username" and v.inject_password_after_username:
                    v.password = "fixture-other-admin-password"  # release-audit: synthetic - concurrent account password fixture
            else:
                assert form["section"] == "servers"
                v.intent(provider_intent=True)
                assert not any(row["name"] == keyword for row in v.servers), "Never overwrite an existing provider"
                row = {key: value for key, value in form.items() if key not in {"mode", "output", "apikey", "section", "keyword"}}
                row.update(name=keyword, password="*" * 10)
                v.servers.append(row)
            if v.fail_after == keyword:
                v.fail_after = None
                raise RuntimeError("fixture-private-lost-response " + CREDS["admin_password"])
            return accounts.HttpResult(200, {"status": True})
        if path == "/login/":
            assert method == "POST"
            correct = form["username"] == v.username and form["password"] == v.password
            self.authorized = (correct and not v.bad_good_login) or (not correct and v.accept_bad)
            self.cookies = self.authorized
            return accounts.HttpResult(303 if self.authorized else 200, None,
                "https://foreign.example.test/" if v.foreign_redirect and self.authorized else "/" if self.authorized else "")
        assert method == "GET" and path in {sab.PROTECTED_UI, "/"}
        allowed = self.authorized or v.anonymous or not v.username or not v.password
        if not allowed:
            return accounts.HttpResult(303, None, "/login/")
        if path == "/" and (not v.servers or v.wizard_stuck):
            return accounts.HttpResult(303, None, "/wizard/")
        return accounts.HttpResult(200, "fixture-private-ui-content")


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("secrets", "state"):
            (self.root / name).mkdir(mode=0o700)
        c = default_config("192.168.50.20")
        c["install_root"], c["features"]["usenet"] = str(self.root), True
        receipt = {"apps": {sab.APP: {"eligible": True, "state": "reserved"}}}
        self.operation = accounts._Enrollment(c, copy.deepcopy(CREDS), receipt, None, None, lambda name: KEY)
        self.operation.state_path = self.root / "state/onboarding.json"
        self.operation.save = lambda: write_json(self.operation.state_path, receipt,
                                                 replace=self.operation.state_path.exists())
        self.operation.save()
        self.vendor = Vendor(self.operation)
        self.operation.session_factory = self.vendor.session

    @property
    def entry(self):
        return self.operation.receipt["apps"][sab.APP]

    def writes(self):
        return [call[2] for call in self.vendor.calls if call[2] and call[2].get("mode") == "set_config"]

    def test_fresh_form_account_key_and_positive_negative_authentication(self):
        detail = sab.enroll(self.operation)
        self.assertIn("form login verified", detail)
        self.assertEqual(["username", "password"], [row["keyword"] for row in self.writes()])
        self.assertTrue(self.entry["admin_verified"])
        self.assertTrue(self.entry["api_key_verified"])
        self.assertFalse(self.entry["wizard_complete"])
        self.assertEqual("verified", self.entry["state"])
        secret = self.root / "secrets/sab_api_key"
        self.assertEqual(KEY + "\n", secret.read_text())
        self.assertNotIn(CREDS["admin_password"], detail)
        if os.name == "posix":
            self.assertEqual(0o600, secret.stat().st_mode & 0o777)

    def test_retry_does_not_rotate_credentials_or_key(self):
        sab.enroll(self.operation)
        before = copy.deepcopy(self.writes())
        secret = self.root / "secrets/sab_api_key"
        mtime = secret.stat().st_mtime_ns
        sab.enroll(self.operation)
        self.assertEqual(before, self.writes())
        self.assertEqual(mtime, secret.stat().st_mtime_ns)

    def test_lost_username_or_password_write_response_resumes_without_reset(self):
        for setting in ("username", "password"):
            with self.subTest(setting=setting):
                self.setUp()
                self.vendor.fail_after = setting
                with self.assertRaises(accounts._Manual) as error:
                    sab.enroll(self.operation)
                self.assertNotIn(CREDS["admin_password"], str(error.exception))
                sab.enroll(self.operation)
                self.assertEqual(1, sum(row["keyword"] == setting for row in self.writes()))
                self.assertTrue(self.entry["admin_verified"])

    def test_different_existing_credentials_are_never_overwritten(self):
        self.vendor.username, self.vendor.password = "existing-admin", "fixture-existing-password"
        with self.assertRaisesRegex(accounts._Manual, "already has different"):
            sab.enroll(self.operation)
        self.assertFalse(self.writes())
        self.assertNotIn("admin_intent", self.entry)

    def test_matching_existing_account_is_verified_without_configuration_writes(self):
        self.vendor.username, self.vendor.password = CREDS["admin_username"], CREDS["admin_password"]
        self.assertIn("Existing SABnzbd", sab.enroll(self.operation))
        self.assertFalse(self.writes())
        self.assertTrue(self.entry["existing_account"])

    def test_existing_account_inspection_uses_no_password_login_or_write(self):
        self.entry["eligible"] = False
        self.vendor.username, self.vendor.password = "existing-admin", "fixture-existing-password"
        detail = sab.inspect_existing(self.operation)
        self.assertIn("password login not tested", detail)
        self.assertFalse(self.writes())
        self.assertFalse(any(path == "/login/" for _, path, _ in self.vendor.calls))
        self.assertFalse((self.root / "secrets/sab_api_key").exists())

    def test_existing_noauth_is_pending_and_unchanged(self):
        self.entry["eligible"] = False
        with self.assertRaisesRegex(accounts._Manual, "no complete UI account"):
            sab.inspect_existing(self.operation)
        with self.assertRaisesRegex(accounts._Manual, "not reserved"):
            sab.enroll(self.operation)
        self.assertFalse(self.writes())

    def test_form_mode_enabled_only_while_both_fields_are_blank(self):
        self.vendor.html_login = False
        sab.enroll(self.operation)
        self.assertEqual("html_login", self.writes()[0]["keyword"])
        self.vendor.html_login = False
        count = len(self.writes())
        with self.assertRaisesRegex(accounts._Manual, "authentication mode changed"):
            sab.enroll(self.operation)
        self.assertEqual(count, len(self.writes()))

    def test_password_appearing_after_username_is_not_overwritten(self):
        self.vendor.inject_password_after_username = True
        with self.assertRaisesRegex(accounts._Manual, "login could not be verified"):
            sab.enroll(self.operation)
        self.assertEqual(["username"], [row["keyword"] for row in self.writes()])

    def test_unexpected_anonymous_wrong_password_or_good_login_fail_closed(self):
        for setting, expected in (("anonymous", "anonymous access"), ("accept_bad", "incorrect password"),
                                  ("bad_good_login", "login could not"), ("foreign_redirect", "unexpected login redirect")):
            with self.subTest(setting=setting):
                self.setUp()
                setattr(self.vendor, setting, True)
                with self.assertRaisesRegex(accounts._Manual, expected):
                    sab.enroll(self.operation)
                self.assertFalse(self.entry.get("admin_verified", False))

    def test_missing_verified_account_is_not_recreated(self):
        sab.enroll(self.operation)
        self.vendor.username, self.vendor.password = "", ""
        count = len(self.writes())
        with self.assertRaisesRegex(accounts._Manual, "missing"):
            sab.enroll(self.operation)
        self.assertEqual(count, len(self.writes()))

    def test_bad_version_key_and_all_stars_refuse_before_account_writes(self):
        self.vendor.version = "future-unreviewed"
        with self.assertRaisesRegex(accounts._Manual, "running version differs"):
            sab.enroll(self.operation)
        self.vendor.version = sab.VERSION
        self.operation.secret_resolver = lambda name: "invalid"
        with self.assertRaisesRegex(accounts._Manual, "unsupported format"):
            sab.enroll(self.operation)
        self.operation.credentials["admin_password"] = "*" * 16
        with self.assertRaisesRegex(accounts._Manual, "all-asterisk"):
            sab.enroll(self.operation)
        self.assertFalse(self.writes())

    def test_generated_api_key_is_resolved_from_real_generated_config(self):
        appdata = self.root / "appdata/sabnzbd"
        appdata.mkdir(parents=True)
        (appdata / "sabnzbd.ini").write_text('[misc]\napi_key = ' + KEY + '\n')
        self.operation.secret_resolver = sab.engine.secret_resolver(self.operation.c)
        sab.enroll(self.operation)
        self.assertEqual(KEY, (self.root / "secrets/sab_api_key").read_text().strip())

    def test_existing_different_private_key_is_never_replaced(self):
        secret = self.root / "secrets/sab_api_key"
        other = "b" * 32
        secret.write_text(other + "\n")
        secret.chmod(0o600)
        with self.assertRaisesRegex(accounts._Manual, "Saved SABnzbd API key differs"):
            sab.enroll(self.operation)
        self.assertEqual(other + "\n", secret.read_text())

    def test_new_provider_requires_verified_admin_and_fresh_reservation(self):
        with self.assertRaisesRegex(accounts._Manual, "Verify SABnzbd"):
            sab.configure_provider(self.operation, PROVIDER)
        self.entry["eligible"] = False
        with self.assertRaisesRegex(accounts._Manual, "not reserved"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertEqual([], self.vendor.calls)

    def test_readonly_provider_listing_redacts_username_password_and_vendor_messages(self):
        self.vendor.servers = [self.vendor.server()]
        rows = sab.configured_providers(self.operation)
        self.assertEqual([{"name": "existing-provider", "host": "old.example.test", "port": 563,
                           "enabled": True, "ssl": True, "connections": 5}], rows)
        encoded = json.dumps(rows)
        self.assertNotIn("username", encoded)
        self.assertNotIn("password", encoded)
        self.assertFalse(self.writes())

    def test_existing_provider_test_uses_masked_stored_credentials_and_no_writes(self):
        self.vendor.servers = [self.vendor.server(), self.vendor.server("disabled", enable=False)]
        result = sab.verify_existing_providers(self.operation)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["verified"])
        self.assertEqual(["passed", "skipped"], [row["status"] for row in result["results"]])
        calls = [form for _, _, form in self.vendor.calls if form and form.get("mode") == "config" and form.get("name") == "test_server"]
        self.assertEqual(1, len(calls))
        self.assertEqual("******", calls[0]["password"])
        self.assertEqual("existing-provider", calls[0]["server"])
        self.assertFalse(self.writes())
        self.assertNotIn("fixture-private", json.dumps(result))

    def test_connection_test_uses_reviewed_config_dispatch_not_top_level_mode(self):
        api = sab._Api(self.operation)
        fields = {"host": "news.example.test", "port": 563, "connections": 10,
                  "username": PROVIDER["username"], "password": PROVIDER["password"],
                  "ssl": 1, "ssl_verify": 3, "timeout": 10}
        with self.assertRaisesRegex(accounts._Manual, "rejected an authenticated API operation"):
            api.call("test_server", **fields)
        self.assertTrue(api.test_server(**fields))
        method, path, form = self.vendor.calls[-1]
        self.assertEqual(("POST", "/api"), (method, path))
        self.assertEqual("config", form["mode"])
        self.assertEqual("test_server", form["name"])
        self.assertEqual(KEY, form["apikey"])
        self.assertEqual(PROVIDER["password"], form["password"])
        self.assertEqual(3, form["ssl_verify"])

    def test_fresh_missing_or_empty_servers_section_is_a_real_empty_collection(self):
        for result in ({"config": {}}, {"config": {"servers": {}}}, {"config": {"servers": []}}):
            with self.subTest(result=result):
                api = sab._Api(self.operation)
                with mock.patch.object(api, "call", return_value=result):
                    self.assertEqual([], api.servers())

    def test_keyed_provider_collection_requires_name_key_agreement(self):
        api = sab._Api(self.operation)
        row = self.vendor.server()
        without_name = {key: value for key, value in row.items() if key != "name"}
        for value in ({row["name"]: row}, {row["name"]: without_name}):
            with self.subTest(value=value), mock.patch.object(api, "call", return_value={"config": {"servers": value}}):
                self.assertEqual([row], api.servers())
        for value in ({"different": row}, {"invalid": "not-a-provider"}, {"": without_name}):
            with self.subTest(value=value), mock.patch.object(api, "call", return_value={"config": {"servers": value}}):
                with self.assertRaises(accounts._Manual):
                    api.servers()

    def test_unknown_servers_schema_does_not_claim_no_providers(self):
        api = sab._Api(self.operation)
        for result in ({}, {"config": None}, {"config": {"servers": None}}, {"config": {"misc": {}}}):
            with self.subTest(result=result), mock.patch.object(api, "call", return_value=result):
                with self.assertRaises(accounts._Manual):
                    api.servers()

    def test_existing_failed_or_missing_providers_are_not_reported_ready(self):
        self.assertFalse(sab.verify_existing_providers(self.operation)["ok"])
        self.vendor.servers = [self.vendor.server()]
        self.vendor.stored_success = False
        result = sab.verify_existing_providers(self.operation)
        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["results"][0]["status"])

    def test_new_provider_is_tested_saved_and_retains_other_servers(self):
        sab.enroll(self.operation)
        existing = self.vendor.server()
        self.vendor.servers = [copy.deepcopy(existing)]
        result = sab.configure_provider(self.operation, PROVIDER)
        self.assertTrue(result["connection_verified"])
        self.assertTrue(result["wizard_complete"])
        self.assertTrue(self.entry["provider_verified"])
        self.assertEqual(existing, self.vendor.servers[0])
        target = self.vendor.servers[1]
        self.assertEqual("samurai-" + self.operation.c["deployment_id"], target["name"])
        self.assertEqual(3, target["ssl_verify"])
        calls = [form for _, _, form in self.vendor.calls if form and form.get("mode") == "config" and form.get("name") == "test_server"]
        self.assertEqual(PROVIDER["password"], calls[-2]["password"])
        self.assertEqual("******", calls[-1]["password"])
        self.assertNotIn(PROVIDER["password"], json.dumps(result))
        self.assertNotIn(PROVIDER["password"], self.operation.state_path.read_text())

    def test_failed_connection_or_unknown_response_creates_no_server(self):
        sab.enroll(self.operation)
        for attribute, expected in (("test_success", "connection or authentication"), ("bad_test_shape", "unsupported NNTP")):
            with self.subTest(attribute=attribute):
                self.vendor.test_success, self.vendor.bad_test_shape = True, False
                setattr(self.vendor, attribute, attribute != "test_success")
                with self.assertRaisesRegex(accounts._Manual, expected):
                    sab.configure_provider(self.operation, PROVIDER)
                self.assertEqual([], self.vendor.servers)

    def test_lost_provider_write_response_resumes_without_duplicate_or_update(self):
        sab.enroll(self.operation)
        self.vendor.fail_after = "samurai-" + self.operation.c["deployment_id"]
        with self.assertRaisesRegex(accounts._Manual, "did not finish"):
            sab.configure_provider(self.operation, PROVIDER)
        count = len(self.writes())
        sab.configure_provider(self.operation, PROVIDER)
        self.assertEqual(count, len(self.writes()))
        self.assertEqual(1, len(self.vendor.servers))

    def test_concurrent_provider_edit_invalidates_plan_before_create(self):
        sab.enroll(self.operation)
        mark = self.operation.mark

        def concurrent_mark(app, **values):
            mark(app, **values)
            if values.get("provider_intent"):
                self.vendor.servers.append(self.vendor.server())

        self.operation.mark = concurrent_mark
        with self.assertRaisesRegex(accounts._Manual, "changed after review"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertEqual(["existing-provider"], [row["name"] for row in self.vendor.servers])
        self.assertFalse(any(row["section"] == "servers" for row in self.writes()))

    def test_existing_collision_or_changed_owned_provider_is_not_replaced(self):
        sab.enroll(self.operation)
        name = "samurai-" + self.operation.c["deployment_id"]
        self.vendor.servers = [self.vendor.server(name)]
        with self.assertRaisesRegex(accounts._Manual, "already exists"):
            sab.configure_provider(self.operation, PROVIDER)
        self.vendor.servers = []
        sab.configure_provider(self.operation, PROVIDER)
        self.vendor.servers[0]["port"] = 564
        count = len(self.writes())
        with self.assertRaisesRegex(accounts._Manual, "settings differ"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertEqual(count, len(self.writes()))

    def test_deleted_verified_provider_is_not_recreated(self):
        sab.enroll(self.operation)
        sab.configure_provider(self.operation, PROVIDER)
        self.vendor.servers = []
        count = len(self.writes())
        with self.assertRaisesRegex(accounts._Manual, "missing"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertEqual(count, len(self.writes()))

    def test_wizard_or_stored_password_failure_does_not_claim_verified(self):
        sab.enroll(self.operation)
        self.vendor.stored_success = False
        with self.assertRaisesRegex(accounts._Manual, "Stored NNTP credentials"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertFalse(self.entry.get("provider_verified", False))
        self.vendor.stored_success, self.vendor.wizard_stuck = True, True
        with self.assertRaisesRegex(accounts._Manual, "still requires its setup wizard"):
            sab.configure_provider(self.operation, PROVIDER)
        self.assertFalse(self.entry.get("provider_verified", False))

    def test_invalid_provider_input_fails_before_connection_or_config_write(self):
        sab.enroll(self.operation)
        count = len(self.vendor.calls)
        for update in ({"host": "https://news.example.test"}, {"host": "0.0.0.0"}, {"port": True},
                       {"connections": 0}, {"ssl": False}, {"password": "*" * 20},
                       {"password": chr(32) + PROVIDER["password"]}, {"extra": "unsupported"}):
            with self.subTest(update=update), self.assertRaises(accounts._Manual):
                sab.configure_provider(self.operation, {**PROVIDER, **update})
            self.assertEqual(count, len(self.vendor.calls))


if __name__ == "__main__":
    unittest.main()
