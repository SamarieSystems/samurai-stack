"""Account enrollment safety boundaries; no production service is contacted."""
from contextlib import ExitStack
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from samurai_stack import catalog, engine, onboarding as accounts
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json


CREDS = {"admin_username": "stackadmin", "admin_password": "fixture-admin-password",  # release-audit: synthetic - disposable administrator credential
         "viewer_username": "household", "viewer_password": "fixture-viewer-password"}  # release-audit: synthetic - disposable viewer credential
ADMIN_ID, VIEWER_ID = "a" * 32, "b" * 32
ADMIN_TOKEN, VIEWER_TOKEN, API_TOKEN = "A" * 32, "V" * 32, "K" * 32
MASKED = "*" * 8
CORE_APPS = ("jellyfin", "sonarr", "radarr", "prowlarr")
ALL_APPS = accounts.SUPPORTED
STAGE_ENDPOINTS = catalog.stage_endpoints


def core_endpoints(config, stage):
    """This stateful vendor models only the original four applications."""
    endpoints = STAGE_ENDPOINTS(config, stage)
    return {name: url for name, url in endpoints.items() if name in CORE_APPS} if stage == 2 else endpoints


class Vendor:
    """Stateful API double models successful writes with lost responses."""
    def __init__(self, c):
        self.c, self.calls = c, []
        self.complete, self.keys, self.fail_after = False, [], None
        self.server_id = "server-one"
        self.network = {"EnableUPnP": False, "EnableRemoteAccess": True}
        self.users = [{"Id": ADMIN_ID, "Name": "root", "HasPassword": False,
                       "HasConfiguredPassword": False, "Policy": {"IsAdministrator": True}}]
        self.passwords = {"root": ""}
        self.starr = {name: {"id": 1, "username": "", "password": "", "authenticationMethod": "none",
                             "authenticationRequired": "enabled", "unrelated": 42} for name in SUPPORTED_STARR}
        self.starr_passwords = {}
        self.unsafe_viewer, self.unsafe_anonymous, self.unsafe_bad_password = False, False, False
        self.bad_good_login, self.foreign_login = False, False

    def write(self, service, path):
        private = Path(self.c["install_root"]) / "secrets" / "onboarding_credentials.json"
        assert private.is_file(), "Credentials must be durably saved before any API mutation"
        receipt = json.loads((Path(self.c["install_root"]) / "state" / "onboarding.json").read_text())
        entry = receipt["apps"][service]
        assert entry["admin_intent"] is True, "Owner write requires prior durable intent"
        if path == "/Users/New":
            assert entry["viewer_intent"] is True
        if path.endswith("/Policy"):
            assert entry["viewer_policy_intent"] is True
        if path.startswith("/Auth/Keys"):
            assert entry["api_key_intent"] is True
        if self.fail_after == (service, path):
            self.fail_after = None
            raise RuntimeError("Lost response: password=" + CREDS["admin_password"])

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, copy.deepcopy(json)))
        service = {catalog.PORTS[name]: name for name in accounts.SUPPORTED}.get(urlsplit(url).port)
        path = urlsplit(url).path
        if service in SUPPORTED_STARR:
            if method == "PUT":
                self.starr[service] = {**copy.deepcopy(json), "password": MASKED}
                self.starr_passwords[service] = json["password"]
                self.write(service, path)
            return copy.deepcopy(self.starr[service])
        if path == "/System/Info/Public":
            return {"Id": self.server_id, "StartupWizardCompleted": self.complete}
        if path == "/Startup/User":
            if method == "POST":
                assert not self.passwords.get(self.users[0]["Name"]), "Never reset an initialized administrator"
                self.users[0].update(Name=json["Name"], HasPassword=True, HasConfiguredPassword=True)
                self.passwords[json["Name"]] = json["Password"]
                self.write(service, path)
            return {"Name": self.users[0]["Name"]}
        if path == "/Users/Public":
            return copy.deepcopy(self.users)
        if path == "/Startup/Complete":
            self.complete = True
            self.write(service, path)
            return None
        if path == "/Users/AuthenticateByName":
            rows = [user for user in self.users if user["Name"] == json["Username"]]
            if len(rows) != 1 or self.passwords.get(json["Username"]) != json["Pw"]:
                raise RuntimeError("Incorrect password: " + json["Pw"])
            return {"User": copy.deepcopy(rows[0]), "AccessToken":
                    ADMIN_TOKEN if rows[0]["Id"] == ADMIN_ID else VIEWER_TOKEN}
        if path == "/System/Info":
            return {"Id": self.server_id, "Version": "fixture"}
        if path == "/System/Configuration/network":
            return copy.deepcopy(self.network)
        if path == "/Users":
            return copy.deepcopy(self.users)
        if path == "/Users/New":
            user = {"Id": VIEWER_ID, "Name": json["Name"], "HasPassword": True,
                    "Policy": {"IsAdministrator": False, "IsDisabled": False,
                               "EnableContentDownloading": True, "EnablePublicSharing": True,
                               "EnableMediaPlayback": True, "EnableAudioPlaybackTranscoding": True,
                               "EnableVideoPlaybackTranscoding": True,
                               "AuthenticationProviderId": "vendor-provider",
                               "PasswordResetProviderId": "vendor-reset"}}
            self.users.append(user)
            self.passwords[user["Name"]] = json["Password"]
            self.write(service, path)
            return copy.deepcopy(user)
        if path.endswith("/Policy"):
            self.users[-1]["Policy"] = copy.deepcopy(json)
            self.write(service, path)
            return None
        if path == "/Users/" + VIEWER_ID:
            return copy.deepcopy(self.users[-1])
        if path == "/Auth/Keys":
            if method == "POST":
                self.keys.append({"AppName": parse_qs(urlsplit(url).query)["app"][0], "AccessToken": API_TOKEN})
                self.write(service, path)
            return {"Items": copy.deepcopy(self.keys)}
        raise AssertionError("Unexpected API contract: " + method + " " + path)

    def session(self, base):
        return VendorSession(self, base)


SUPPORTED_STARR = ("sonarr", "radarr", "prowlarr")


class VendorSession:
    def __init__(self, vendor, base):
        self.vendor, self.base, self.authorized = vendor, base, False
        self.service = {catalog.PORTS[name]: name for name in accounts.SUPPORTED}[urlsplit(base).port]

    def has_cookies(self):
        return self.authorized

    def send(self, method, path, *, headers=None, form=None):
        if self.service == "jellyfin":
            token = (headers or {}).get("Authorization", "")
            allowed = ADMIN_TOKEN in token or (VIEWER_TOKEN in token and self.vendor.unsafe_viewer)
            allowed |= not token and self.vendor.unsafe_anonymous
            return accounts.HttpResult(200 if allowed else 403, {})
        if path == "/login":
            self.authorized = (form["username"] == CREDS["admin_username"]
                and form["password"] == self.vendor.starr_passwords.get(self.service)
                and not self.vendor.bad_good_login) or self.vendor.unsafe_bad_password
            return accounts.HttpResult(302, None, "https://foreign.invalid/" if self.vendor.foreign_login else
                                       "/" if self.authorized else "/login?loginFailed=true")
        return accounts.HttpResult(200, "<html>private app</html>") if self.authorized or self.vendor.unsafe_anonymous else accounts.HttpResult(302, None, "/login")


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"] = str(self.root)
        self.c["features"]["usenet"] = False
        if os.name == "posix" and os.geteuid() != 0:
            self.c["puid"], self.c["pgid"] = os.geteuid(), os.getegid()
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(accounts, "SUPPORTED", CORE_APPS))
        stack.enter_context(mock.patch.object(catalog, "stage_endpoints", side_effect=core_endpoints))
        for name in ("state", "secrets", "stages", "appdata"):
            (self.root / name).mkdir(mode=0o700)
        for name in catalog.stage_endpoints(self.c, 2):
            (self.root / "appdata" / name).mkdir()
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        stack.enter_context(mock.patch.object(accounts, "_guard"))
        # Fixtures must run as an ordinary CI user. Only UID ownership is
        # synthetic here; real private modes, link counts and symlink guards
        # still execute, and production retains a strict UID-0 predicate.
        stack.enter_context(mock.patch.object(accounts, "_root_owned", return_value=True))
        stack.enter_context(mock.patch.object(engine, "host_identity", return_value="fixture-machine"))
        # This stateful vendor intentionally has no SQLite files. Model only
        # the independent passwordless seed proof here; the real DB/WAL and
        # custom-provider checks execute in test_enroll_jellyfin_existing.
        stack.enter_context(mock.patch("samurai_stack.enroll_jellyfin_existing.passwordless_seed",
                                       side_effect=self.passwordless_seed))
        self.no_containers = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        self.vendor = Vendor(self.c)

    def passwordless_seed(self, config, startup_name):
        rows = getattr(self.vendor, "users", [])
        passwords = getattr(self.vendor, "passwords", {})
        if len(rows) == 1 and rows[0].get("Name") == startup_name and passwords.get(startup_name) == "":
            return rows[0]["Id"].replace("-", "").lower()
        return None

    def reserve(self):
        return accounts.reserve_fresh_apps(self.c, runner=self.no_containers)

    def test_reservation_queries_the_custom_media_project_before_classifying_freshness(self):
        self.c['compose_projects'] = {'2': 'house-media'}
        receipt = self.reserve()
        self.assertTrue(receipt['apps']['jellyfin']['eligible'])
        self.assertIn('label=com.docker.compose.project=house-media', self.no_containers.call_args.args[0])

    def onboard(self, credentials=CREDS):
        with mock.patch.object(accounts, "check_targets"):
            return accounts.onboarding(self.c, credentials, request=self.vendor,
                session_factory=self.vendor.session, secret_resolver=lambda name: "fixture-api-key")

    def receipt(self):
        return json.loads((self.root / "state" / "onboarding.json").read_text())

    def supported_passed(self, report):
        self.assertFalse(report["errors"], report)
        self.assertEqual({check["name"] for check in report["checks"]}, {name + ":accounts" for name in accounts.SUPPORTED})

    def test_reservation_only_empty_owned_supported_apps(self):
        (self.root / "appdata" / "sonarr" / "config.xml").write_text("legacy")
        receipt = self.reserve()
        self.assertFalse(receipt["apps"]["sonarr"]["eligible"])
        self.assertNotIn("seerr", receipt["apps"])
        self.assertTrue(receipt["apps"]["jellyfin"]["eligible"])
        self.assertNotIn("sonarr", accounts.eligible_apps(self.c))

    def test_all_seven_apps_can_reserve_only_empty_managed_directories(self):
        self.c["features"]["usenet"] = True
        with mock.patch.object(accounts, "SUPPORTED", ALL_APPS), \
             mock.patch.object(catalog, "stage_endpoints", STAGE_ENDPOINTS):
            for app in STAGE_ENDPOINTS(self.c, 2):
                (self.root / "appdata" / app).mkdir(exist_ok=True)
            receipt = self.reserve()
            self.assertEqual(set(ALL_APPS), set(receipt["apps"]))
            self.assertTrue(all(row["eligible"] for row in receipt["apps"].values()))
            self.assertEqual(set(ALL_APPS), set(accounts.eligible_apps(self.c)))

    def test_existing_compose_or_container_prevents_all_reservations(self):
        for kind in ("compose", "container"):
            with self.subTest(kind=kind):
                path = self.root / "state" / "onboarding.json"
                if path.exists():
                    path.unlink()
                compose = engine.compose_path(self.c, 2)
                compose.parent.mkdir(parents=True, exist_ok=True)
                if kind == "compose":
                    compose.write_text("{}")
                else:
                    compose.unlink()
                    self.no_containers.return_value.stdout = "container-id\n"
                receipt = self.reserve()
                self.assertFalse(any(row["eligible"] for row in receipt["apps"].values()))

    def test_reservation_reused_not_refreshed_after_first_start(self):
        first = self.reserve()
        (self.root / "appdata" / "jellyfin" / "db").write_text("started")
        self.no_containers.side_effect = AssertionError("Do not rescan or re-reserve")
        self.assertEqual(self.reserve(), first)

    def test_legacy_needs_no_credentials_and_only_inspects_accounts_readonly(self):
        before_starr = copy.deepcopy(self.vendor.starr)
        before_passwords = copy.deepcopy(self.vendor.passwords)
        result = self.onboard(None)
        self.assertTrue(self.vendor.calls)
        self.assertTrue(all(method == "GET" for method, _, _ in self.vendor.calls))
        self.assertFalse(any(self.config_write(row) for row in self.vendor.calls))
        self.assertEqual(before_starr, self.vendor.starr)
        self.assertEqual(before_passwords, self.vendor.passwords)
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["errors"])
        self.assertEqual({app + ":accounts" for app in CORE_APPS}, {row["name"] for row in result["pending"]})

    def test_configured_legacy_api_accounts_verify_without_credentials_or_writes(self):
        self.vendor.complete = True
        for row in self.vendor.starr.values():
            row.update(username="existing-admin", password=MASKED, authenticationMethod="forms")
        before = copy.deepcopy(self.vendor.starr)
        report = self.onboard(None)
        self.supported_passed(report)
        self.assertTrue(report["ok"])
        self.assertFalse(any(self.config_write(row) for row in self.vendor.calls))
        self.assertTrue(all(method == "GET" for method, _, _ in self.vendor.calls))
        self.assertEqual(before, self.vendor.starr)
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())

    def test_fresh_accounts_and_restrictions_verified(self):
        self.reserve()
        result = self.onboard()
        self.supported_passed(result)
        viewer = self.vendor.users[-1]
        for key, expected in accounts.VIEWER_POLICY.items():
            self.assertEqual(viewer["Policy"][key], expected)
        self.assertTrue(viewer["Policy"]["EnableVideoPlaybackTranscoding"])
        self.assertEqual(viewer["Policy"]["AuthenticationProviderId"], "vendor-provider")
        self.assertEqual((self.root / "secrets" / "jellyfin_api_key").read_text().strip(), API_TOKEN)
        for name in SUPPORTED_STARR:
            self.assertEqual(self.vendor.starr[name]["unrelated"], 42)
        encoded = json.dumps(result)
        for secret in (CREDS["admin_password"], CREDS["viewer_password"], API_TOKEN, ADMIN_TOKEN, VIEWER_TOKEN):
            self.assertNotIn(secret, encoded)
            self.assertFalse(any(secret in url for _, url, _ in self.vendor.calls))

    def test_saved_pair_resumes_without_new_mutations(self):
        self.reserve()
        self.supported_passed(self.onboard())
        before = len([row for row in self.vendor.calls if self.config_write(row)])
        self.supported_passed(self.onboard(None))
        after = len([row for row in self.vendor.calls if self.config_write(row)])
        self.assertEqual(before, after)

    @staticmethod
    def config_write(row):
        return row[0] in {"PUT", "POST"} and urlsplit(row[1]).path != "/Users/AuthenticateByName"

    def test_changed_credentials_refused_before_network(self):
        self.reserve()
        self.onboard()
        self.vendor.calls.clear()
        changed = {**CREDS, "admin_password": "fixture-different-password"}  # release-audit: synthetic - refusal to replace saved credential
        with self.assertRaisesRegex(SafetyError, "refusing account rotation"):
            self.onboard(changed)
        self.assertFalse(self.vendor.calls)

    def test_password_and_username_validation_before_credential_or_account_writes(self):
        self.reserve()
        before = self.receipt()
        for changes in ({"viewer_username": "STACKADMIN"}, {"viewer_password": CREDS["admin_password"]},
                        {"admin_password": "test-tiny"}, {"admin_password": "fixture-password" + chr(10)},  # release-audit: synthetic - deliberately invalid credential values
                        {"admin_username": "name with space"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(SafetyError):
                    self.onboard({**CREDS, **changes})
        # Public startup/seed inspection may precede validation; none of the
        # supplied credentials may be used, persisted or sent in a login.
        self.assertTrue(all(method == "GET" for method, _, _ in self.vendor.calls))
        self.assertFalse(any(self.config_write(row) for row in self.vendor.calls))
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())
        self.assertEqual(self.receipt(), before)

    def test_initialized_jellyfin_wrong_credentials_are_not_adopted_or_reset(self):
        self.reserve()
        self.vendor.complete = True
        self.vendor.users[0].update(Name="previous-owner", HasPassword=True)
        self.vendor.passwords = {"previous-owner": "fixture-existing-owner-password"}  # release-audit: synthetic - preserve unrelated initialized owner
        before_users, before_passwords = copy.deepcopy(self.vendor.users), copy.deepcopy(self.vendor.passwords)
        with self.assertRaises(SafetyError):
            self.onboard()
        self.assertFalse(any(self.config_write(row) for row in self.vendor.calls))
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())
        self.assertEqual(self.vendor.users, before_users)
        self.assertEqual(self.vendor.passwords, before_passwords)

    def test_password_set_during_incomplete_startup_is_not_reset(self):
        self.reserve()
        self.vendor.users[0]["HasPassword"] = True
        self.vendor.passwords["root"] = CREDS["admin_password"]
        before = copy.deepcopy(self.vendor.passwords)
        with self.assertRaises(SafetyError):
            self.onboard()
        self.assertFalse(any(urlsplit(url).path == "/Startup/User" and method == "POST" for method, url, _ in self.vendor.calls))
        self.assertFalse(any(self.config_write(row) for row in self.vendor.calls))
        self.assertEqual(self.vendor.passwords, before)
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())

    def test_obsolete_true_password_flags_and_hidden_public_seed_do_not_block_real_empty_login(self):
        self.reserve()
        self.vendor.users[0].update(HasPassword=True, HasConfiguredPassword=True)
        # Modern UserDto flags are constant true even for a passwordless seed.
        # The vendor's startup write independently checks the real password.
        original = self.vendor.__class__.__call__
        def modern(vendor, method, url, **kwargs):
            if urlsplit(url).path == "/Users/Public":
                return []
            return original(vendor, method, url, **kwargs)
        with mock.patch.object(Vendor, "__call__", modern):
            self.supported_passed(self.onboard())
        self.assertFalse(any(urlsplit(url).path == "/Users/Public" for _, url, _ in self.vendor.calls))

    def test_disabling_optional_service_preserves_historical_manual_receipt(self):
        self.c["features"]["usenet"] = True
        with mock.patch.object(accounts, "SUPPORTED", ALL_APPS), \
             mock.patch.object(catalog, "stage_endpoints", STAGE_ENDPOINTS):
            for app in STAGE_ENDPOINTS(self.c, 2):
                (self.root / "appdata" / app).mkdir(exist_ok=True)
            (self.root / "appdata/sabnzbd/sabnzbd.ini").write_text("existing configuration\n")
            self.reserve()
            self.assertFalse(self.receipt()["apps"]["sabnzbd"]["eligible"])
            self.c["features"]["usenet"] = False
            self.assertEqual(set(accounts.eligible_apps(self.c)), set(ALL_APPS) - {"sabnzbd"})
            self.assertIn("sabnzbd", self.receipt()["apps"])

    def test_starr_blank_username_alone_does_not_authorize_password_write(self):
        self.reserve()
        self.vendor.starr["sonarr"]["password"] = MASKED
        self.onboard()
        self.assertFalse(any(method == "PUT" and urlsplit(url).port == 8989 for method, url, _ in self.vendor.calls))

    def test_starr_same_existing_username_is_not_rotated(self):
        self.reserve()
        self.vendor.starr["radarr"].update(username=CREDS["admin_username"], password=MASKED)
        self.onboard()
        self.assertFalse(any(method == "PUT" and urlsplit(url).port == 7878 for method, url, _ in self.vendor.calls))

    def test_existing_viewer_is_not_modified_even_with_desired_name(self):
        self.reserve()
        original = self.vendor.write
        def append_existing(service, path):
            original(service, path)
            if path == "/Startup/Complete":
                self.vendor.users.append({"Id": VIEWER_ID, "Name": CREDS["viewer_username"],
                                          "Policy": {"IsAdministrator": False}})
        self.vendor.write = append_existing
        result = self.onboard()
        self.assertTrue(any(row["name"] == "jellyfin:accounts" for row in result["pending"]))
        self.assertFalse(any(urlsplit(url).path.endswith("/Policy") for _, url, _ in self.vendor.calls))

    def test_lost_responses_resume_without_password_resets_or_duplicate_users(self):
        operations = [("jellyfin", "/Startup/User"), ("jellyfin", "/Startup/Complete"),
                      ("jellyfin", "/Users/New"), ("jellyfin", "/Users/" + VIEWER_ID + "/Policy"),
                      ("jellyfin", "/Auth/Keys"), ("sonarr", "/api/v3/config/host/1")]
        for operation in operations:
            with self.subTest(operation=operation):
                # A separate private installation for each interruption point.
                instance = OnboardingTests(methodName="runTest")
                instance.setUp()
                try:
                    instance.reserve()
                    instance.vendor.fail_after = operation
                    first = instance.onboard()
                    self.assertTrue(first["errors"])
                    instance.supported_passed(instance.onboard(None))
                    writes = [row for row in instance.vendor.calls if row[0] in {"PUT", "POST"}
                              and urlsplit(row[1]).path == operation[1]
                              and urlsplit(row[1]).port == catalog.PORTS[operation[0]]]
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(len(instance.vendor.users), 2)
                    self.assertEqual(len(instance.vendor.keys), 1)
                finally:
                    instance.doCleanups()

    def test_verified_viewer_policy_drift_is_not_overwritten(self):
        self.reserve()
        self.onboard()
        self.vendor.users[-1]["Policy"]["EnableContentDownloading"] = True
        self.vendor.calls.clear()
        result = self.onboard(None)
        self.assertTrue(any(row["name"] == "jellyfin:accounts" for row in result["pending"]))
        self.assertFalse(any(urlsplit(url).path.endswith("/Policy") for _, url, _ in self.vendor.calls))

    def test_network_readback_rejects_present_port_mapping_flags_without_changing_them(self):
        self.reserve()
        for flag in ("EnableUPnP", "EnableAutomaticPortMapping"):
            for value in (True, None, "false", 0):
                with self.subTest(flag=flag, value=value):
                    self.vendor.network = {flag: value, "EnableRemoteAccess": True}
                    result = self.onboard()
                    self.assertFalse(any(row["name"] == "jellyfin:accounts" for row in result["checks"]))
                    self.assertTrue(any(row["name"] == "jellyfin:accounts" for row in result["pending"]))
        self.assertFalse(any(method in {"POST", "PUT"} and urlsplit(url).path in
                         {"/Startup/RemoteAccess", "/System/Configuration/network"}
                         for method, url, _ in self.vendor.calls))
        self.vendor.network = {"EnableRemoteAccess": True}
        self.supported_passed(self.onboard())

    def test_wrong_viewer_privilege_or_anonymous_access_cannot_pass(self):
        self.reserve()
        self.vendor.unsafe_viewer = True
        result = self.onboard()
        self.assertFalse(any(row["name"] == "jellyfin:accounts" for row in result["checks"]))
        self.assertFalse(self.receipt()["apps"]["jellyfin"].get("viewer_policy_verified", False))

    def test_incorrect_password_acceptance_or_failed_login_cannot_pass_starr(self):
        self.reserve()
        self.vendor.unsafe_bad_password = True
        result = self.onboard()
        self.assertFalse(any(row["name"] in {app + ":accounts" for app in SUPPORTED_STARR} for row in result["checks"]))

    def test_cross_origin_login_redirect_is_not_followed_or_accepted(self):
        self.reserve()
        self.vendor.foreign_login = True
        result = self.onboard()
        self.assertFalse(any(row["name"] in {app + ":accounts" for app in SUPPORTED_STARR} for row in result["checks"]))

    def test_replaced_application_directory_refuses_reservation_resume(self):
        self.reserve()
        path = self.root / "appdata" / "jellyfin"
        path.rename(path.with_name("retired-jellyfin"))
        path.mkdir()
        with self.assertRaisesRegex(SafetyError, "directory was replaced"):
            accounts.eligible_apps(self.c)

    def test_changed_host_uuid_or_address_refuses_reservation(self):
        self.reserve()
        for field, value in (("host_id", "other"), ("deployment_id", "other"), ("bind_address", "192.168.50.21")):
            receipt = self.receipt()
            original = receipt[field]
            receipt[field] = value
            write_json(self.root / "state" / "onboarding.json", receipt, replace=True)
            with self.assertRaises(SafetyError):
                accounts.eligible_apps(self.c)
            receipt[field] = original
            write_json(self.root / "state" / "onboarding.json", receipt, replace=True)

    def test_vendor_exception_contents_never_reach_preflight_error_or_report(self):
        self.reserve()
        def failure(*args, **kwargs):
            raise RuntimeError("SECRET " + CREDS["admin_password"])
        self.vendor = failure
        with mock.patch.object(accounts, "check_targets"):
            with self.assertRaises(SafetyError) as error:
                accounts.onboarding(self.c, CREDS, request=failure)
            self.assertNotIn(CREDS["admin_password"], str(error.exception))
            self.assertNotIn("SECRET", str(error.exception))
            self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())
            # Also exercise sanitization inside the per-app report loop after
            # independently classifying this synthetic fresh seed.
            with mock.patch.object(accounts, "_existing_jellyfin", return_value=False):
                report = accounts.onboarding(self.c, CREDS, request=failure)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn(CREDS["admin_password"], json.dumps(report))
        self.assertNotIn("SECRET", json.dumps(report))

    def test_target_guard_runs_before_private_credentials_and_http(self):
        self.reserve()
        with mock.patch.object(accounts, "check_targets", side_effect=SafetyError("Wrong target")):
            with self.assertRaisesRegex(SafetyError, "Wrong target"):
                accounts.onboarding(self.c, CREDS, request=self.vendor)
        self.assertFalse(self.vendor.calls)
        self.assertFalse((self.root / "secrets" / "onboarding_credentials.json").exists())

    def test_private_files_owned_by_another_user_are_refused(self):
        with mock.patch.object(accounts, "_root_owned", return_value=False):
            with self.assertRaisesRegex(SafetyError, "owned by root"):
                self.reserve()
        self.assertFalse(self.vendor.calls)

    @unittest.skipUnless(os.name == "posix", "POSIX private-directory permissions")
    def test_group_readable_enrollment_parent_is_refused(self):
        (self.root / "state").chmod(0o750)
        with self.assertRaisesRegex(SafetyError, "private to root"):
            self.reserve()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "POSIX root ownership/mode contract")
    def test_reservation_and_credentials_are_private_root_owned_regular_files(self):
        self.reserve()
        self.onboard()
        for path in (self.root / "state" / "onboarding.json", self.root / "secrets" / "onboarding_credentials.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, 0)
            self.assertEqual(path.stat().st_nlink, 1)


class TargetProofTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config("192.168.50.20")
        self.address = self.c["bind_address"]
        self.labels = {"systems.samarie.deployment": self.c["deployment_id"], "systems.samarie.stage": "2",
                       "com.docker.compose.project": self.c["deployment_name"] + "-2"}
        self.foreign_labels = self.wrong_port = self.wrong_mount = self.missing_ip = False
        self.checked = []
        self.extra_mounts = {}
        self.runtime = {
            "jellyfin": {"Env": ["JELLYFIN_DATA_DIR=/config", "JELLYFIN_CONFIG_DIR=/config/config"],
                         "Cmd": None, "Entrypoint": ["/jellyfin/jellyfin"]},
            "seerr": {"Env": ["NODE_ENV=production"], "WorkingDir": "/app",
                      "Cmd": ["npm", "start"], "Entrypoint": ["docker-entrypoint.sh"]},
        }

    def runner(self, argv, **kwargs):
        if argv[0] == "ip":
            value = [{"addr_info": [{"local": "192.168.50.99" if self.missing_ip else self.address}]}]
        elif "ps" in argv:
            service = next(item.split("=", 2)[-1] for item in argv if item.startswith("label=com.docker.compose.service="))
            self.checked.append(service)
            return subprocess.CompletedProcess(argv, 0, stdout=service + "\n", stderr="")
        else:
            service = argv[argv.index("inspect") + 1]
            if ".Config.Labels" in argv[-1]:
                value = {**self.labels, "com.docker.compose.service": service}
                if self.foreign_labels:
                    value["systems.samarie.deployment"] = "foreign"
            elif ".Ports" in argv[-1]:
                port = str(catalog.PORTS[service])
                value = {port + "/tcp": [{"HostIp": "0.0.0.0" if self.wrong_port else self.address, "HostPort": port}]}
            elif argv[-1] == "{{json .Config}}":
                value = self.runtime[service]
            else:
                value = [{"Type": "bind", "Destination": "/app/config" if service == "seerr" else "/config", "Source":
                          "/foreign" if self.wrong_mount else str(Path(self.c["install_root"]) / "appdata" / service)}]
                value.extend(self.extra_mounts.get(service, []))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value), stderr="")

    def test_all_supported_services_are_proved_including_jellyfin_and_prowlarr(self):
        with mock.patch.object(engine, "check_docker") as docker:
            accounts.check_targets(self.c, accounts.SUPPORTED, self.runner)
        docker.assert_called_once_with(self.runner)
        self.assertEqual(self.checked, list(accounts.SUPPORTED))

    def test_custom_media_project_is_checked_in_both_query_and_live_labels(self):
        self.c['compose_projects'] = {'2': 'house-media'}
        self.labels['com.docker.compose.project'] = 'house-media'
        queries = []
        def runner(argv, **kwargs):
            if 'ps' in argv:
                queries.append(argv)
                self.assertIn('label=com.docker.compose.project=house-media', argv)
            return self.runner(argv, **kwargs)
        with mock.patch.object(engine, 'check_docker'):
            accounts.check_targets(self.c, ['jellyfin', 'sonarr'], runner)
        self.assertEqual(len(queries), 2)
        self.labels['com.docker.compose.project'] = 'homelab-2'
        with mock.patch.object(engine, 'check_docker'), self.assertRaisesRegex(SafetyError, 'another installation'):
            accounts.check_targets(self.c, ['jellyfin'], runner)

    def test_stale_address_foreign_labels_wrong_port_or_foreign_mount_fail(self):
        for field in ("missing_ip", "foreign_labels", "wrong_port", "wrong_mount"):
            with self.subTest(field=field), mock.patch.object(engine, "check_docker"):
                setattr(self, field, True)
                with self.assertRaises(SafetyError):
                    accounts.check_targets(self.c, ["jellyfin"], self.runner)
                setattr(self, field, False)

    def test_custom_database_env_workdir_or_startup_cannot_attest_local_sqlite(self):
        variants = {
            "jellyfin": [
                {"Env": ["JELLYFIN_DATA_DIR=/other", "JELLYFIN_CONFIG_DIR=/config/config"]},
                {"Env": ["JELLYFIN_DATA_DIR=/config", "JELLYFIN_CONFIG_DIR=/other"]},
                {"Env": ["JELLYFIN_DATA_DIR=/config"]},
                {"Cmd": ["--datadir", "/other"]},
                {"Entrypoint": ["/custom-startup"]},
            ],
            "seerr": [
                {"Env": ["NODE_ENV=production", "DB_TYPE=postgres"]},
                {"Env": ["NODE_ENV=production", "CONFIG_DIRECTORY=/other"]},
                {"Env": ["NODE_ENV=test"]}, {"Env": ["NODE_ENV=development"]}, {"Env": []},
                {"WorkingDir": "/other"}, {"Cmd": ["node", "other.js"]},
                {"Entrypoint": ["/custom-startup"]},
            ],
        }
        for app, cases in variants.items():
            original = copy.deepcopy(self.runtime[app])
            for changes in cases:
                with self.subTest(app=app, fields=list(changes)), mock.patch.object(engine, "check_docker"):
                    self.runtime[app] = {**original, **changes}
                    with self.assertRaisesRegex(SafetyError, "custom account datastore or startup command"):
                        accounts.check_targets(self.c, [app], self.runner)
            self.runtime[app] = original

    def test_default_explicit_sqlite_directory_and_unrelated_mount_are_allowed(self):
        self.runtime["seerr"]["Env"].extend(["DB_TYPE=sqlite", "CONFIG_DIRECTORY=/app/config"])
        self.extra_mounts["jellyfin"] = [{"Type": "bind", "Destination": "/cache", "Source": "/owned/cache"}]
        with mock.patch.object(engine, "check_docker"):
            accounts.check_targets(self.c, ["jellyfin", "seerr"], self.runner)

    def test_ambiguous_or_malformed_datastore_environment_is_refused(self):
        for app in ("jellyfin", "seerr"):
            original = copy.deepcopy(self.runtime[app])
            duplicate = "JELLYFIN_DATA_DIR=/other" if app == "jellyfin" else "NODE_ENV=test"
            for env in (None, "not-a-list", [None], ["missing-assignment"], original["Env"] + [duplicate]):
                with self.subTest(app=app, shape=type(env).__name__), mock.patch.object(engine, "check_docker"):
                    self.runtime[app] = {**original, "Env": env}
                    with self.assertRaises(SafetyError):
                        accounts.check_targets(self.c, [app], self.runner)
            self.runtime[app] = original

    def test_nested_config_mount_cannot_hide_a_different_active_database(self):
        for app, destination in (("jellyfin", "/config/data"), ("seerr", "/app/config/db")):
            with self.subTest(app=app), mock.patch.object(engine, "check_docker"):
                self.extra_mounts[app] = [{"Type": "bind", "Destination": destination, "Source": "/different/database"}]
                with self.assertRaisesRegex(SafetyError, "Nested application mounts"):
                    accounts.check_targets(self.c, [app], self.runner)
            self.extra_mounts[app] = []


class SessionTransportTests(unittest.TestCase):
    def test_redirects_not_followed_cookies_isolated_and_passwords_only_in_body(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append(("GET", self.path, ""))
                if self.path == "/foreign":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/never-contact")
                elif "Auth=good" not in self.headers.get("Cookie", ""):
                    self.send_response(302)
                    self.send_header("Location", "/login")
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b"private")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                calls.append(("POST", self.path, body))
                self.send_response(302)
                self.send_header("Set-Cookie", "Auth=good; Path=/; HttpOnly")
                self.send_header("Location", "/")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_address[1])
            session = accounts.HttpSession(base)
            self.assertEqual(session.send("GET", "/").status, 302)
            self.assertEqual(session.send("POST", "/login", form={"username": "admin", "password": CREDS["admin_password"]}).status, 302)
            self.assertTrue(session.has_cookies())
            self.assertEqual(session.send("GET", "/").status, 200)
            self.assertEqual(accounts.HttpSession(base).send("GET", "/").status, 302)
            self.assertEqual(session.send("GET", "/foreign").status, 302)
            self.assertEqual(len(calls), 5)
            self.assertTrue(all("?" not in path and CREDS["admin_password"] not in path for _, path, _ in calls))
            self.assertEqual(parse_qs(calls[1][2])["password"], [CREDS["admin_password"]])
            with self.assertRaises(SafetyError):
                session.send("GET", "//foreign.invalid/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_public_hostname_and_credential_urls_are_refused(self):
        for base in ("https://example.com", "http://8.8.8.8:8096", "http://fixture-user:" + "fixture-password@127.0.0.1:8096",
                     "http://0.0.0.0:8096", "http://127.0.0.1:8096/path"):
            with self.subTest(base=base), self.assertRaises(SafetyError):
                accounts.HttpSession(base)


if __name__ == "__main__":
    unittest.main()
