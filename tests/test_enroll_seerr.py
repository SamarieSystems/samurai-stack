"""Seerr 3.5.1 enrollment contracts with no real application traffic."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from samurai_stack import catalog, enroll_seerr
from samurai_stack.cli import default_config
from samurai_stack.onboarding import HttpResult
from samurai_stack.safety import SafetyError, write_json

CREDS = {"admin_username": "fixture-admin", "admin_password": "fixture-admin-password",  # release-audit: synthetic - disposable owner credential
         "viewer_username": "fixture-viewer", "viewer_password": "fixture-viewer-password"}  # release-audit: synthetic - disposable viewer credential
ADMIN_ID, VIEWER_ID, SERVER_ID = "a" * 32, "b" * 32, "fixture-server"
KEY, JF_KEY = "S" * 64, "J" * 32


def owner():
    return {"id": 1, "permissions": 2, "jellyfinUsername": CREDS["admin_username"], "jellyfinUserId": ADMIN_ID}


def viewer():
    return {"id": 2, "permissions": 32, "jellyfinUsername": CREDS["viewer_username"], "jellyfinUserId": VIEWER_ID}


class Operation:
    def __init__(self, c):
        self.c, self.credentials = c, copy.deepcopy(CREDS)
        self.receipt = {"apps": {"seerr": {"eligible": True, "state": "reserved"}}}
        self.endpoints = catalog.stage_endpoints(c, 2)
        self.vendor = Model(self)
        self.session_factory = self.vendor.session
        self.jf_logins = []

    def mark(self, app, **values):
        self.receipt["apps"][app].update(values)
        write_json(Path(self.c["install_root"]) / "state" / "receipt.json", self.receipt,
                   replace=(Path(self.c["install_root"]) / "state" / "receipt.json").exists())

    def login_jellyfin(self, username, password, *, administrator):
        self.jf_logins.append((username, administrator))
        expected_name = CREDS["admin_username" if administrator else "viewer_username"]
        expected_password = CREDS["admin_password" if administrator else "viewer_password"]
        if username != expected_name or password != expected_password:
            raise SafetyError("Wrong synthetic Jellyfin credentials")
        return {"Id": ADMIN_ID if administrator else VIEWER_ID}, {"Authorization": "fixture-authorization"}

    def call(self, method, app, path, **kwargs):
        if (method, app, path) == ("GET", "jellyfin", "/Users/" + ADMIN_ID):
            return {"Id": ADMIN_ID, "Name": CREDS["admin_username"], "Policy": {"IsAdministrator": True}}
        if (method, app, path) != ("GET", "jellyfin", "/System/Info"):
            raise AssertionError("Unexpected cross-service call")
        return {"Id": SERVER_ID}


class Model:
    def __init__(self, operation):
        self.operation, self.calls, self.mutations = operation, [], []
        self.owner = self.viewer = None
        self.type, self.initialized, self.fail_after = 4, False, None
        self.settings = {"main": {"apiKey": KEY, "mediaServerType": 4, "defaultPermissions": 32,
                                  "newPlexLogin": True, "mediaServerLogin": True},
                         "public": {"initialized": False}, "jellyfin": {"ip": ""}}
        self.unsafe_viewer = self.unsafe_anon = self.reject_login = False
        self.write_settings()

    def write_settings(self):
        path = Path(self.operation.c["install_root"]) / "appdata" / "seerr" / "settings.json"
        write_json(path, self.settings, replace=path.exists())

    def session(self, base):
        assert base == self.operation.endpoints["seerr"]
        return Session(self)

    def changed(self, name):
        receipt = json.loads((Path(self.operation.c["install_root"]) / "state" / "receipt.json").read_text())
        entry = receipt["apps"]["seerr"]
        assert entry["desired_identity"]["jellyfin_admin_id"] == ADMIN_ID
        assert (Path(self.operation.c["install_root"]) / "secrets" / "onboarding_credentials.json").is_file()
        if name == "owner":
            assert entry["owner_intent"] and entry["owner_absent_verified"]
        elif name == "viewer":
            assert entry["viewer_intent"]
        elif name == "initialize":
            assert entry["initialize_intent"]
        self.mutations.append(name)
        if self.fail_after == name:
            self.fail_after = None
            raise RuntimeError("private-body=" + CREDS["admin_password"])

    def make_existing(self, *, with_viewer=True):
        self.owner, self.viewer = owner(), viewer() if with_viewer else None
        self.type, self.initialized = 2, True
        self.settings["main"]["mediaServerType"] = 2
        self.settings["public"]["initialized"] = True
        self.settings["jellyfin"] = {"ip": catalog.role_host(self.operation.c, 2), "port": 8096,
            "useSsl": False, "urlBase": "", "serverId": SERVER_ID, "apiKey": JF_KEY}
        self.write_settings()


class Session:
    def __init__(self, model):
        self.model, self.user = model, None

    def has_cookies(self):
        return self.user is not None

    def send(self, method, path, *, headers=None, json=None):
        model, operation = self.model, self.model.operation
        model.calls.append((method, path, copy.deepcopy(headers), copy.deepcopy(json)))
        assert "?" not in path, "Credentials must never be encoded in URLs"
        auth = model.owner if (headers or {}).get("X-Api-Key") == KEY else self.user
        is_admin = bool(auth and auth["permissions"] & 2)
        if method == "GET" and path == "/api/v1/settings/public":
            return HttpResult(200, {"initialized": model.initialized, "mediaServerType": model.type})
        if path == "/api/v1/auth/me":
            return HttpResult(200, copy.deepcopy(auth)) if auth else HttpResult(403, {})
        if method == "GET" and path.startswith("/api/v1/settings/"):
            if not is_admin and not (auth and model.unsafe_viewer) and not (not auth and model.unsafe_anon):
                return HttpResult(403, {})
            return HttpResult(200, copy.deepcopy(model.settings["main" if path.endswith("/main") else "jellyfin"]))
        if path == "/api/v1/user":
            if not is_admin:
                return HttpResult(403, {})
            return HttpResult(200, {"results": copy.deepcopy([row for row in (model.owner, model.viewer) if row])})
        if path == "/api/v1/auth/jellyfin":
            if model.reject_login:
                return HttpResult(403, {})
            if json["username"] == CREDS["admin_username"] and json["password"] == CREDS["admin_password"]:
                if model.type == 4:
                    assert model.owner is None, "Never enter upstream's owner-recovery branch"
                    assert json["hostname"] == catalog.role_host(operation.c, 2)
                    assert json["serverType"] == 2 and "email" not in json
                    model.owner = owner()
                    model.type = model.settings["main"]["mediaServerType"] = 2
                    model.settings["jellyfin"] = {"ip": json["hostname"], "port": json["port"], "useSsl": False,
                        "urlBase": "", "serverId": SERVER_ID, "apiKey": JF_KEY}
                    model.write_settings()
                    model.changed("owner")
                else:
                    assert "hostname" not in json and "serverType" not in json
                self.user = model.owner
            elif json["username"] == CREDS["viewer_username"] and json["password"] == CREDS["viewer_password"]:
                assert "hostname" not in json and "serverType" not in json
                if model.viewer is None:
                    model.viewer = viewer()
                    model.changed("viewer")
                self.user = model.viewer
            else:
                return HttpResult(403, {})
            return HttpResult(200, copy.deepcopy(self.user))
        if path == "/api/v1/settings/initialize":
            assert is_admin
            model.initialized = model.settings["public"]["initialized"] = True
            model.write_settings()
            model.changed("initialize")
            return HttpResult(200, {"initialized": True})
        raise AssertionError("Unexpected Seerr contract")


class SeerrEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"] = str(self.root)
        if os.name == "posix" and os.geteuid() != 0:
            self.c["puid"], self.c["pgid"] = os.geteuid(), os.getegid()
        for name in ("state", "secrets", "appdata/seerr"):
            (self.root / name).mkdir(mode=0o700, parents=True)
        self.database = self.root / "appdata" / "seerr" / "db" / "db.sqlite3"
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT, permissions INTEGER, jellyfinUserId TEXT, jellyfinUsername TEXT)')
        write_json(self.root / "secrets" / "onboarding_credentials.json", CREDS)
        self.operation = Operation(self.c)
        self.model = self.operation.vendor

    def enroll(self):
        return enroll_seerr.enroll(self.operation)

    def test_fresh_enrollment_saves_identity_before_creating_owner_and_viewer(self):
        detail = self.enroll()
        self.assertIn("request-only viewer", detail)
        self.assertEqual(self.model.mutations, ["owner", "viewer", "initialize"])
        self.assertEqual(self.operation.receipt["apps"]["seerr"]["state"], "verified")
        self.assertEqual((self.root / "secrets" / "seerr_api_key").read_text().strip(), KEY)
        for secret in (KEY, JF_KEY, CREDS["admin_password"], CREDS["viewer_password"]):
            self.assertNotIn(secret, detail)

    def test_resume_uses_normal_logins_without_account_or_configuration_writes(self):
        self.enroll()
        before = list(self.model.mutations)
        self.model.calls.clear()
        self.enroll()
        self.assertEqual(self.model.mutations, before)
        login_bodies = [body for method, path, _, body in self.model.calls if path == "/api/v1/auth/jellyfin"]
        self.assertEqual(len(login_bodies), 2)
        self.assertTrue(all(set(body) == {"username", "password"} for body in login_bodies))
        self.assertFalse(any(path == "/api/v1/settings/initialize" for _, path, _, _ in self.model.calls))

    def test_no_reservation_means_no_requests_or_writes(self):
        self.operation.receipt["apps"]["seerr"]["eligible"] = False
        with self.assertRaisesRegex(SafetyError, "no proven fresh"):
            self.enroll()
        self.assertFalse(self.model.calls)
        self.assertFalse(self.model.mutations)

    def test_initialized_false_does_not_allow_existing_owner_recovery(self):
        self.model.owner = owner()
        with self.assertRaisesRegex(SafetyError, "owner recovery was refused"):
            self.enroll()
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))
        self.assertFalse(self.operation.jf_logins)

    def test_even_recorded_partial_owner_does_not_allow_recovery_branch(self):
        self.model.owner = owner()
        self.operation.mark("seerr", owner_intent=True, owner_absent_verified=True)
        with self.assertRaisesRegex(SafetyError, "owner recovery was refused"):
            self.enroll()
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))

    def test_foreign_existing_owner_cannot_be_rebound(self):
        self.model.make_existing()
        self.model.owner["jellyfinUserId"] = "c" * 32
        with self.assertRaisesRegex(SafetyError, "owner identity differs"):
            self.enroll()
        self.assertFalse(self.model.mutations)
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))

    def test_configured_existing_owner_and_viewer_are_only_authenticated_and_verified(self):
        self.model.make_existing()
        self.enroll()
        self.assertFalse(self.model.mutations)
        self.assertTrue(self.operation.receipt["apps"]["seerr"]["existing_accounts_only"])

    def test_existing_owner_does_not_get_an_implicitly_created_viewer(self):
        self.model.make_existing(with_viewer=False)
        with self.assertRaisesRegex(SafetyError, "no replacement account was created"):
            self.enroll()
        self.assertFalse(self.model.mutations)
        self.assertIsNone(self.model.viewer)

    def test_lost_completed_responses_resume_exact_accounts_without_duplicates(self):
        for stage in ("owner", "viewer", "initialize"):
            with self.subTest(stage=stage):
                self.setUp()
                self.model.fail_after = stage
                with self.assertRaisesRegex(SafetyError, "request failed") as error:
                    self.enroll()
                self.assertNotIn(CREDS["admin_password"], str(error.exception))
                self.enroll()
                self.assertEqual(self.model.mutations.count("owner"), 1)
                self.assertEqual(self.model.mutations.count("viewer"), 1)
                self.assertEqual(self.model.mutations.count("initialize"), 1)

    def test_changed_desired_jellyfin_identity_refuses_resume(self):
        self.enroll()
        self.operation.receipt["apps"]["seerr"]["desired_identity"]["jellyfin_admin_id"] = "c" * 32
        self.model.calls.clear()
        with self.assertRaisesRegex(SafetyError, "enrollment identity differs"):
            self.enroll()
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))

    def test_changed_private_key_is_not_replaced_or_hidden_by_owner_cookie(self):
        self.enroll()
        private = self.root / "secrets" / "seerr_api_key"
        private.write_text("X" * 64)
        with self.assertRaisesRegex(SafetyError, "keys differ"):
            self.enroll()
        self.assertEqual(private.read_text(), "X" * 64)

    def test_elevated_default_permissions_are_not_silently_used_or_overwritten(self):
        self.model.settings["main"]["defaultPermissions"] = 2
        self.model.write_settings()
        with self.assertRaisesRegex(SafetyError, "default request permissions"):
            self.enroll()
        self.assertIsNone(self.model.viewer)
        self.assertEqual(self.model.settings["main"]["defaultPermissions"], 2)

    def test_viewer_or_anonymous_admin_access_prevents_success(self):
        for attribute in ("unsafe_viewer", "unsafe_anon"):
            with self.subTest(attribute=attribute):
                self.setUp()
                setattr(self.model, attribute, True)
                with self.assertRaisesRegex(SafetyError, "unexpectedly"):
                    self.enroll()
                self.assertNotEqual(self.operation.receipt["apps"]["seerr"]["state"], "verified")

    def test_existing_viewer_permissions_are_never_changed(self):
        self.model.make_existing()
        self.model.viewer["permissions"] = 2
        with self.assertRaisesRegex(SafetyError, "request-only permissions differ"):
            self.enroll()
        self.assertEqual(self.model.viewer["permissions"], 2)
        self.assertFalse(self.model.mutations)

    def test_missing_recorded_owner_is_not_recreated(self):
        self.enroll()
        self.model.owner = None
        self.model.calls.clear()
        with self.assertRaisesRegex(SafetyError, "no replacement owner"):
            self.enroll()
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))

    def test_read_only_existing_inspection_never_logs_in_or_writes(self):
        self.model.make_existing()
        detail = enroll_seerr.inspect_existing(self.operation)
        self.assertIn("accounts and settings unchanged", detail)
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))
        self.assertFalse(self.operation.jf_logins)
        self.assertFalse((self.root / "secrets" / "seerr_api_key").exists())

    def test_resolve_api_key_is_read_only_and_rejects_malformed_or_credential_values(self):
        self.assertEqual(enroll_seerr.resolve_api_key(self.c), KEY)
        self.assertFalse((self.root / "secrets" / "seerr_api_key").exists())
        for value in ("short", "X" * 32 + chr(10), None, {"not": "a key"}):
            self.model.settings["main"]["apiKey"] = value
            self.model.write_settings()
            with self.assertRaisesRegex(SafetyError, "valid application API key"):
                enroll_seerr.resolve_api_key(self.c)

    def test_claimable_is_read_only_and_requires_zero_users_not_just_missing_owner(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertTrue(enroll_seerr.claimable(self.operation))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertTrue(all(method == "GET" for method, _, _, _ in self.model.calls))
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute('INSERT INTO "user" (id,email,permissions) VALUES (2,?,32)', ("fixture-viewer",))
        self.assertFalse(enroll_seerr.claimable(self.operation))
        with self.assertRaisesRegex(SafetyError, "absence could not be proved"):
            self.enroll()
        self.assertFalse(self.model.mutations)

    def test_claimable_reads_committed_users_still_in_active_wal(self):
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute('INSERT INTO "user" (id,email,permissions) VALUES (7,?,32)', ("fixture-wal-viewer",))
            writer.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            self.assertFalse(enroll_seerr.claimable(self.operation))

    def test_claimable_refuses_missing_database_and_unknown_schema_without_creating_it(self):
        self.database.unlink()
        self.assertFalse(enroll_seerr.claimable(self.operation))
        self.assertFalse(self.database.exists())
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY)')
        self.assertFalse(enroll_seerr.claimable(self.operation))

    def test_claimable_refuses_existing_owner_and_initialized_state(self):
        self.model.owner = owner()
        self.assertFalse(enroll_seerr.claimable(self.operation))
        self.model.owner = None
        self.model.initialized = True
        self.assertFalse(enroll_seerr.claimable(self.operation))


if __name__ == "__main__":
    unittest.main()
