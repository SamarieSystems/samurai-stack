"""Owned management enrollment and the actual Socket.IO client protocol."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import management_setup as management, engine, catalog, onboarding
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json

CREDS = {"username": "fixture-admin", "password": "fixture-private-password"}  # release-audit: synthetic - isolated management account fixture


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class KumaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config()
        self.enterContext(patch.object(management.time, "sleep"))

    def test_initial_listener_race_retries_only_reads_before_existing_database_ready(self):
        with patch.object(engine, "request", side_effect=[SafetyError("fixture-private-vendor-error"),
                OSError("fixture-private-vendor-error"), {"needSetup": False, "runningSetup": False}]) as request:
            management._kuma_database(self.c, False, request)
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(row.args[0] == "GET" and row.kwargs["timeout"] == 2 for row in request.call_args_list))

    def test_post_setup_transient_reads_retry_without_replaying_database_write(self):
        values = [{"needSetup": True, "runningSetup": False}, {"ok": True},
                  OSError("fixture-private-vendor-error"), {"needSetup": True, "runningSetup": True},
                  {"needSetup": False, "runningSetup": False}]
        with patch.object(engine, "request", side_effect=values) as request:
            management._kuma_database(self.c, True, request)
        writes = [row for row in request.call_args_list if row.args[0] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].kwargs["json"], {"dbConfig": {"type": "sqlite"}})

    def test_unapproved_setup_after_listener_race_remains_refused(self):
        with patch.object(engine, "request", side_effect=[OSError("fixture-private-vendor-error"), {"needSetup": True}]) as request:
            with self.assertRaisesRegex(SafetyError, "reviewed first-run"):
                management._kuma_database(self.c, False, request)
        self.assertTrue(all(row.args[0] == "GET" for row in request.call_args_list))

    def test_unknown_json_schema_fails_immediately_in_both_phases(self):
        for bad in ("<html>startup</html>", {"needSetup": "false"}, {"needSetup": False, "runningSetup": "false"}):
            with self.subTest(bad=bad), patch.object(engine, "request", return_value=bad) as request:
                with self.assertRaisesRegex(SafetyError, "unknown schema"):
                    management._kuma_database(self.c, True, request)
                self.assertEqual(request.call_count, 1)
        with patch.object(engine, "request", side_effect=[{"needSetup": True}, {"ok": True}, {}]) as request:
            with self.assertRaisesRegex(SafetyError, "unknown schema"):
                management._kuma_database(self.c, True, request)
        self.assertEqual(request.call_count, 3)

    def test_failed_database_write_is_not_retried_or_exposed(self):
        with patch.object(engine, "request", side_effect=[{"needSetup": True}, OSError("fixture-private-vendor-error")]) as request:
            with self.assertRaisesRegex(SafetyError, "not confirmed") as caught:
                management._kuma_database(self.c, True, request)
        self.assertNotIn("fixture-private-vendor-error", str(caught.exception))
        self.assertEqual(request.call_count, 2)

    def test_permanent_unavailable_reads_are_attempt_bounded_and_redacted(self):
        with patch.object(engine, "request", side_effect=OSError("fixture-private-vendor-error")) as request:
            with self.assertRaisesRegex(SafetyError, "did not become ready") as caught:
                management._kuma_database(self.c, True, request)
        self.assertEqual(request.call_count, 30)
        self.assertNotIn("fixture-private-vendor-error", str(caught.exception))

    def test_one_shared_deadline_bounds_both_readiness_phases(self):
        with patch.object(management.time, "monotonic", side_effect=[0, 0, 46]), \
             patch.object(engine, "request", side_effect=[{"needSetup": True}, {"ok": True}]) as request:
            with self.assertRaisesRegex(SafetyError, "did not become ready"):
                management._kuma_database(self.c, True, request)
        self.assertEqual(request.call_count, 2)

    def test_transitioning_listener_must_finish_setup_before_socket_authentication(self):
        with patch.object(engine, "request", side_effect=[{"needSetup": False, "runningSetup": True},
                {"needSetup": False, "runningSetup": False}]) as request:
            management._kuma_database(self.c, False, request)
        self.assertEqual(request.call_count, 2)

    def test_initial_in_progress_setup_is_never_posted_again(self):
        with patch.object(engine, "request", side_effect=[{"needSetup": True, "runningSetup": True},
                {"needSetup": False, "runningSetup": False}]) as request:
            management._kuma_database(self.c, True, request)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(row.args[0] == "GET" for row in request.call_args_list))


class Host:
    def __init__(self, c, root):
        self.c, self.root = c, root
        self.model_root = Path(c["install_root"])
        self.calls, self.payloads = [], []
        self.bad, self.accept_wrong, self.lose_setup = None, False, False
        self.ids = {"dockge": "a" * 64, "uptime-kuma": "b" * 64}
        self.image_config = {"Cmd": ["node", "server"], "Entrypoint": ["init"], "WorkingDir": "/app"}

    def __call__(self, argv, **kwargs):
        argv = list(map(str, argv)); self.calls.append((argv, kwargs))
        value = None
        if argv[:3] == ["ip", "-j", "address"]:
            value = [{"addr_info": [{"local": self.c["bind_address"]}]}]
        else:
            assert argv[:3] == ["docker", "--context", "default"], argv
            args = argv[3:]
            if args[:2] == ["context", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, "unix:///var/run/docker.sock", "")
            if args[:2] == ["compose", "version"]:
                return subprocess.CompletedProcess(argv, 0, "2.30.0", "")
            if args[:1] == ["info"]:
                return subprocess.CompletedProcess(argv, 0, "linux", "")
            if args[:1] == ["ps"]:
                name = next(name for name in self.ids if "label=com.docker.compose.service=" + name in args)
                return subprocess.CompletedProcess(argv, 0, self.ids[name], "")
            if args[:2] == ["image", "inspect"]:
                if args[-1] == "{{.Id}}":
                    return subprocess.CompletedProcess(argv, 0, "sha256:" + "c" * 64, "")
                value = self.image_config
            elif args[:1] == ["inspect"]:
                name = next(name for name in self.ids if self.ids[name] == args[1])
                template = args[-1]
                if template == "{{json .Config.Labels}}":
                    value = {"systems.samarie.deployment": self.c["deployment_id"], "systems.samarie.stage": "4",
                        "com.docker.compose.project": self.c["deployment_name"] + "-4", "com.docker.compose.service": name}
                    if self.bad == "project": value["com.docker.compose.project"] = "foreign"
                elif template == "{{json .Image}}":
                    value = "sha256:" + ("d" if self.bad == "image" else "c") * 64
                elif template == "{{json .Config}}":
                    value = {**self.image_config, "Env": ["TZ=Etc/UTC"]}
                    if self.bad == "command": value["Cmd"] = ["node", "server", "--data-dir", "/foreign"]
                    if self.bad == "database": value["Env"].append("UPTIME_KUMA_DB_TYPE=mysql")
                elif template == "{{json .NetworkSettings.Ports}}":
                    port = str(catalog.PORTS[name]); value = {port + "/tcp": [{"HostIp": self.c["bind_address"], "HostPort": port}]}
                    if self.bad == "wildcard": value[port + "/tcp"][0]["HostIp"] = "0.0.0.0"
                elif template == "{{json .Mounts}}":
                    value = [{"Destination": "/app/data", "Type": "bind", "Source": str(self.model_root / "appdata" / name), "RW": True}]
                    if name == "dockge":
                        value += [{"Destination": str(self.model_root / "stages"), "Type": "bind", "Source": str(self.model_root / "stages")},
                                  {"Destination": "/var/run/docker.sock", "Type": "bind", "Source": "/var/run/docker.sock"}]
                    if self.bad == "mount": value[0]["Source"] = "/foreign"
                    if self.bad == "nested": value.append({"Destination": "/app/data/private", "Type": "bind", "Source": "/foreign"})
                else: raise AssertionError(template)
            elif args[:1] == ["exec"]:
                name = next(name for name in self.ids if self.ids[name] == args[4])
                p = json.loads(kwargs["input"]); self.payloads.append(p)
                assert CREDS["password"] not in " ".join(argv)
                if p["mode"] == "deny":
                    value = {"login_denied": not self.accept_wrong}
                else:
                    with database(self.root / "appdata" / name / management.DATABASES[name]) as db:
                        count = db.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]
                        if p["allow_setup"] and count == 0:
                            db.execute('INSERT INTO "user" (username,password) VALUES (?,?)', (p["username"], "fixture-hash"))
                            db.commit()
                            if self.lose_setup:
                                self.lose_setup = False
                                raise SafetyError("fixture-private-password vendor error")
                        users = db.execute('SELECT username FROM "user"').fetchall()
                    if not users or p["username"] not in {row[0] for row in users}:
                        return subprocess.CompletedProcess(argv, 2, "", "fixture-private-password vendor echo")
                    value = {"admin_login": True, "projects": [], "monitors": [], "authenticated_monitor_list": True}
            else: raise AssertionError(args)
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c = default_config("192.168.50.21")
        self.c["install_root"] = self.root.as_posix().removeprefix(self.root.drive)
        self.enterContext(patch.object(engine, "linux_only"))
        self.enterContext(patch.object(engine, "host_identity", return_value="fixture-host"))
        self.enterContext(patch.object(onboarding, "_root_owned", return_value=True))
        if os.name == "posix": self.enterContext(patch.object(os, "geteuid", return_value=0))
        for name in ("state", "secrets", "appdata", "stages", "backups"):
            (self.root / name).mkdir(mode=0o700)
        write_json(self.root / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        stage = self.root / "stages" / "homelab-4"; stage.mkdir(mode=0o700)
        write_json(stage / "compose.yaml", {"name": "homelab-4"})
        for name in management.SERVICES:
            directory = self.root / "appdata" / name; directory.mkdir(mode=0o700)
            with database(directory / management.DATABASES[name]) as db:
                db.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY, username TEXT, password TEXT)')
                if name == "uptime-kuma": db.execute('CREATE TABLE monitor (id INTEGER PRIMARY KEY)')
        self.host = Host(self.c, self.root)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert method == "GET" and url.endswith("/setup-database-info")
        return {"needSetup": False}

    def plan(self, claim=True):
        return management.plan(self.c, claim_unconfigured=claim, runner=self.host)

    def apply(self, document=None):
        document = document or self.plan()
        return management.apply(self.c, document, document["review_hash"], CREDS, runner=self.host, request=self.request)

    def add_user(self, name, username="fixture-admin"):
        with database(self.root / "appdata" / name / management.DATABASES[name]) as db:
            db.execute('INSERT INTO "user" (username,password) VALUES (?,?)', (username, "fixture-hash"))

    def test_blank_owner_needs_explicit_claim_and_review_contains_no_credentials(self):
        with self.assertRaisesRegex(SafetyError, "explicit reviewed"):
            self.plan(False)
        document = self.plan()
        self.assertEqual({row["action"] for row in document["apps"].values()}, {"create_first_administrator"})
        self.assertNotIn(CREDS["password"], json.dumps(document))
        self.assertFalse(self.host.payloads)

    def test_owned_targets_reject_foreign_ports_mounts_images_commands_and_database(self):
        for bad in ("project", "wildcard", "mount", "nested", "image", "command", "database"):
            with self.subTest(bad=bad), self.assertRaises(SafetyError):
                self.host.bad = bad; self.plan()
        self.assertFalse(self.host.payloads)

    def test_ownerless_existing_monitors_cannot_be_reclaimed(self):
        with database(self.root / "appdata/uptime-kuma/kuma.db") as db: db.execute("INSERT INTO monitor DEFAULT VALUES")
        with self.assertRaisesRegex(SafetyError, "existing monitors"):
            self.plan()

    def test_unknown_account_schema_refuses_without_creating_owner(self):
        path = self.root / "appdata/dockge/dockge.db"
        with database(path) as db: db.execute('DROP TABLE "user"')
        with self.assertRaisesRegex(SafetyError, "schema is unknown"):
            self.plan()
        self.assertFalse(self.host.payloads)

    def test_account_race_invalidates_review_before_credentials_or_exec(self):
        document = self.plan(); self.add_user("dockge", "someone-else")
        with self.assertRaisesRegex(SafetyError, "review changed"):
            self.apply(document)
        self.assertFalse((self.root / "secrets/management_credentials.json").exists())
        self.assertFalse(self.host.payloads)

    def test_success_stores_private_intent_and_credentials_then_proves_wrong_password_denial(self):
        report = self.apply()
        self.assertTrue(report["ok"])
        self.assertEqual(report["notification_delivery"], "not_configured_or_tested")
        value = json.loads((self.root / "state/management-onboarding.json").read_text())
        self.assertTrue(all(row["admin_verified"] and row["incorrect_password_rejected"] for row in value["apps"].values()))
        path = self.root / "secrets/management_credentials.json"
        self.assertEqual(json.loads(path.read_text()), CREDS)
        if os.name == "posix": self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sum(p["mode"] == "deny" for p in self.host.payloads), 2)
        self.assertFalse((self.root / "state/apply.lock").exists())

    def test_repeat_uses_existing_login_and_does_not_recreate_accounts(self):
        self.apply(); before = (self.root / "secrets/management_credentials.json").read_bytes()
        self.host.payloads.clear(); report = self.apply()
        self.assertTrue(report["ok"])
        self.assertFalse(any(p["allow_setup"] for p in self.host.payloads))
        self.assertEqual((self.root / "secrets/management_credentials.json").read_bytes(), before)
        self.assertTrue(all(len(management.database_state(self.c, name)["users"]) == 1 for name in management.SERVICES))

    def test_persistent_owner_receipts_survive_device_renumbering_without_account_writes(self):
        identity = onboarding._identity
        filesystem = {"target": "/", "source": "UUID=fixture-app-filesystem", "fstype": "ext4"}

        def durable(path, **kwargs):
            return {**identity(path, **kwargs), "filesystem": filesystem}

        with patch.object(onboarding, "_identity", side_effect=durable):
            self.apply()
            states = {name: management.database_state(self.c, name) for name in management.SERVICES}
        original_receipt = (self.root / "state/management-onboarding.json").read_bytes()
        self.host.payloads.clear()

        def renumbered(path, **kwargs):
            return {**durable(path, **kwargs), "device": durable(path, **kwargs)["device"] + 64}

        changed = copy.deepcopy(states)
        for row in changed.values(): row["database"]["device"] += 64
        with patch.object(onboarding, "_identity", side_effect=renumbered), \
             patch.object(management, "database_state", side_effect=lambda _c, name, **kw: changed[name]):
            self.assertEqual({row["action"] for row in self.plan()["apps"].values()}, {"verify_existing_login"})
            self.assertTrue(management.verify(self.c, runner=self.host, request=self.request)["ok"])
            changed["dockge"]["database"]["inode"] += 1
            with self.assertRaisesRegex(SafetyError, "database was replaced"):
                self.plan()
        self.assertEqual((self.root / "state/management-onboarding.json").read_bytes(), original_receipt)
        self.assertFalse(any(row["allow_setup"] for row in self.host.payloads))

    def test_lost_setup_response_preserves_intent_and_new_plan_can_verify_existing_login(self):
        self.host.lose_setup = True
        with self.assertRaises(SafetyError) as caught: self.apply()
        self.assertNotIn(CREDS["password"], str(caught.exception))
        value = json.loads((self.root / "state/management-onboarding.json").read_text())
        self.assertEqual(value["apps"]["dockge"]["state"], "intent")
        self.host.payloads.clear(); self.assertTrue(self.apply()["ok"])
        self.assertFalse(self.host.payloads[0]["allow_setup"])

    def test_missing_previously_verified_owner_is_never_recreated(self):
        self.apply()
        with database(self.root / "appdata/dockge/dockge.db") as db: db.execute('DELETE FROM "user"')
        self.host.payloads.clear()
        with self.assertRaisesRegex(SafetyError, "verified management owner is missing"):
            self.plan()
        self.assertFalse(self.host.payloads)

    def test_unknown_existing_owner_is_not_reset(self):
        self.add_user("dockge", "someone-else")
        with self.assertRaises(SafetyError): self.apply()
        self.assertFalse(self.host.payloads[0]["allow_setup"])
        self.assertEqual(management.database_state(self.c, "dockge")["users"][0]["username"], "someone-else")

    def test_missing_previously_observed_existing_owner_cannot_gain_first_user_authority(self):
        self.add_user("dockge", "someone-else")
        with self.assertRaises(SafetyError): self.apply()
        with database(self.root / "appdata/dockge/dockge.db") as db: db.execute('DELETE FROM "user"')
        with self.assertRaisesRegex(SafetyError, "verified management owner is missing"):
            self.plan()

    def test_before_start_reservation_allows_only_its_fresh_kuma_database_initialization(self):
        # Temporary appdirs belong to the ordinary test runner, which is not
        # necessarily UID 1000 (GitHub Ubuntu runners currently use UID 1001).
        # Make that real owner the intended media UID for this fresh fixture.
        owner = (self.root / "appdata/dockge").stat().st_uid
        if os.name == "posix" and owner != 0:
            self.c["puid"] = owner
        (self.root / "stages/homelab-4/compose.yaml").unlink()
        for name in management.SERVICES:
            (self.root / "appdata" / name / management.DATABASES[name]).unlink()
        empty = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
        receipt = management.reserve_fresh(self.c, runner=empty)
        self.assertTrue(all(row["eligible"] for row in receipt["apps"].values()))
        with database(self.root / "appdata/dockge/dockge.db") as db:
            db.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY, username TEXT, password TEXT)')
        (self.root / "appdata/uptime-kuma/upload").mkdir()
        document = self.plan(False)
        self.assertEqual(document["apps"]["uptime-kuma"]["database_state"]["database"], None)
        self.assertEqual(document["apps"]["uptime-kuma"]["action"], "create_first_administrator")

    def test_uid_1001_fresh_directories_require_matching_intended_media_uid(self):
        (self.root / "stages/homelab-4/compose.yaml").unlink()
        directories = {Path(self.c["install_root"]) / "appdata" / name for name in management.SERVICES}
        for name in management.SERVICES:
            (self.root / "appdata" / name / management.DATABASES[name]).unlink()
        original_stat = Path.stat

        def uid_1001(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path in directories:
                fields = list(result); fields[4] = 1001
                return os.stat_result(fields)
            return result

        empty = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(management, "os", SimpleNamespace(name="posix")), \
             patch.object(management, "_guard"), patch.object(Path, "stat", uid_1001):
            self.c["puid"] = 1000
            receipt = management.reserve_fresh(self.c, runner=empty)
            self.assertFalse(any(row["eligible"] for row in receipt["apps"].values()))
            (self.root / "state/management-onboarding.json").unlink()
            self.c["puid"] = 1001
            receipt = management.reserve_fresh(self.c, runner=empty)
            self.assertTrue(all(row["eligible"] for row in receipt["apps"].values()))

    def test_missing_legacy_database_with_unknown_files_is_never_assumed_fresh(self):
        path = self.root / "appdata/uptime-kuma/kuma.db"
        path.unlink()
        (path.parent / "important-evidence").write_text("fixture-sentinel")
        with self.assertRaisesRegex(SafetyError, "missing or unknown"):
            self.plan()
        self.assertEqual((path.parent / "important-evidence").read_text(), "fixture-sentinel")

    def test_reservation_does_not_expand_after_existing_compose_or_container(self):
        empty = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
        receipt = management.reserve_fresh(self.c, runner=empty)
        self.assertFalse(any(row["eligible"] for row in receipt["apps"].values()))
        self.assertEqual(management.reserve_fresh(self.c, runner=lambda *_a, **_k: self.fail("query")), receipt)

    def test_replaced_database_refuses_even_if_same_username_is_present(self):
        self.apply()
        path = self.root / "appdata/dockge/dockge.db"
        path.rename(path.with_suffix(".retained"))
        with database(path) as db:
            db.execute('CREATE TABLE "user" (id INTEGER PRIMARY KEY, username TEXT, password TEXT)')
            db.execute('INSERT INTO "user" (username,password) VALUES (?,?)', (CREDS["username"], "fixture-hash"))
        with self.assertRaisesRegex(SafetyError, "database was replaced"):
            self.plan()

    def test_accepting_wrong_password_cannot_mark_verified(self):
        self.host.accept_wrong = True
        with self.assertRaises(SafetyError): self.apply()
        value = json.loads((self.root / "state/management-onboarding.json").read_text())
        self.assertEqual(value["apps"]["dockge"]["state"], "intent")

    def test_saved_credentials_are_not_overwritten(self):
        management.credentials(self.c, CREDS)
        with self.assertRaisesRegex(SafetyError, "credentials differ"):
            management.credentials(self.c, {"username": "new-admin", "password": "fixture-other-password"})  # release-audit: synthetic - rejected credential replacement fixture
        self.assertEqual(management.credentials(self.c), CREDS)

    def test_verify_is_readonly_and_never_calls_setup_or_add_monitors(self):
        self.apply(); self.host.payloads.clear()
        self.assertTrue(management.verify(self.c, runner=self.host, request=self.request)["ok"])
        self.assertTrue(all(not p["allow_setup"] and not p["write_monitors"] for p in self.host.payloads))


JS_RUNNER = r'''
const fs=require("fs"),vm=require("vm"),input=JSON.parse(fs.readFileSync(0,"utf8"));
const p=input.payload,f=input.fixture||{},handlers=new Map(),calls=[],output=[];
let enrolled=!f.fresh,current=f.monitors||{},counter=Object.keys(current).length;
function fire(name,...args){for(const fn of [...(handlers.get(name)||[])])fn(...args);}
const s={on(name,fn){if(!handlers.has(name))handlers.set(name,[]);handlers.get(name).push(fn);return s;},
off(name,fn){handlers.set(name,(handlers.get(name)||[]).filter(x=>x!==fn));return s;},close(){},timeout(){return s;},
emit(...args){const done=args.pop(),name=args[0];calls.push(name==="agent"?args[2]:name);setImmediate(()=>{
 if(name==="needSetup")return done(null,f.invalidSetup?{}:!enrolled);
 if(name==="setup"){enrolled=true;return done(null,{ok:true});}
 if(name==="login")return done(null,{ok:enrolled&&!f.badLogin});
 if(name==="getMonitorList"){if(!f.missingEvent)fire("monitorList",current);return done(null,{ok:true});}
 if(name==="add"){const row={...args[1],id:++counter};current[row.id]=row;return done(null,{ok:true,monitorID:row.id});}
 if(name==="agent"&&args[2]==="requestStackList"){fire("agent","stackList",{ok:true,endpoint:"",stackList:f.missingStack?{}:{tools:{name:"tools",status:3}}});return done(null,{ok:true});}
 if(name==="agent"&&args[2]==="getStack")return done(null,{ok:true,stack:{name:args[3],isManagedByDockge:!f.foreign}});
 if(name==="agent"&&args[2]==="serviceStatusList")return done(null,{ok:true,serviceStatusList:{dockge:"healthy"}});
 done(new Error("unexpected"));});return s;}};
const mockRequire=name=>name==="fs"?{readFileSync:()=>JSON.stringify(p)}:name==="socket.io-client"?{io:()=>{setImmediate(()=>{if(f.fresh&&p.service==="dockge")fire("setup");fire("connect");});return s;}}:require(name);
vm.runInNewContext(input.source,{require:mockRequire,console:{log:x=>output.push(x)},
 process:{exit:code=>{process.stdout.write(JSON.stringify({code,calls,output,monitors:current}));process.exit(0);}},
 setTimeout:(fn,ms)=>setTimeout(fn,ms===60000?1000:Math.min(ms,50)),clearTimeout});
'''


@unittest.skipUnless(shutil.which("node"), "Node protocol tests run in hosted/disposable Linux and available local runtimes")
class SocketProtocolTests(unittest.TestCase):
    def execute(self, service="uptime-kuma", allow=True, write=True, mode="verify", **fixture):
        config = default_config()
        wanted = management.default_monitors(config)[:1]
        payload = {"service": service, "url": "http://127.0.0.1:3001", **CREDS,
            "allow_setup": allow, "write_monitors": write, "mode": mode,
            "projects": ["tools"], "monitors": wanted}
        result = subprocess.run([shutil.which("node"), "-e", JS_RUNNER],
            input=json.dumps({"source": management.SOCKET_SCRIPT, "payload": payload, "fixture": fixture}),
            capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout), wanted

    def test_fresh_kuma_creates_only_with_explicit_permission_and_reads_added_monitor(self):
        result, wanted = self.execute(fresh=True)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["calls"].count("setup"), 1)
        self.assertEqual(result["calls"].count("add"), 1)
        self.assertEqual(json.loads(result["output"][0])["monitors"][0]["name"], wanted[0]["name"])

    def test_unapproved_fresh_account_is_never_created(self):
        result, _ = self.execute(fresh=True, allow=False)
        self.assertEqual(result["code"], 2); self.assertNotIn("setup", result["calls"])

    def test_existing_bad_login_is_never_reset(self):
        result, _ = self.execute(badLogin=True)
        self.assertEqual(result["code"], 2); self.assertNotIn("setup", result["calls"])

    def test_invalid_needsetup_schema_cannot_write(self):
        result, _ = self.execute(invalidSetup=True)
        self.assertEqual(result["code"], 2); self.assertNotIn("setup", result["calls"])

    def test_missing_monitor_is_not_added_during_readonly_verification(self):
        result, _ = self.execute(write=False)
        self.assertEqual(result["code"], 2); self.assertNotIn("add", result["calls"])

    def test_custom_monitors_are_preserved_and_only_absent_named_monitor_is_added(self):
        custom = {"99": {"id": 99, "name": "My private monitor", "type": "http", "url": "http://example.invalid"}}
        result, _ = self.execute(monitors=custom)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["monitors"]["99"], custom["99"])
        self.assertNotIn("editMonitor", result["calls"])

    def test_matching_managed_monitor_does_not_add_duplicate(self):
        config = default_config(); row = dict(management.default_monitors(config)[0], id=1)
        result, _ = self.execute(monitors={"1": row})
        self.assertEqual(result["code"], 0); self.assertNotIn("add", result["calls"])

    def test_divergent_or_duplicate_same_name_is_preserved_and_refused(self):
        config = default_config(); wanted = management.default_monitors(config)[0]
        for rows in ({"1": dict(wanted, id=1, port=1)}, {"1": dict(wanted, id=1), "2": dict(wanted, id=2)}):
            with self.subTest(rows=rows):
                result, _ = self.execute(monitors=rows)
                self.assertEqual(result["code"], 2); self.assertNotIn("add", result["calls"])

    def test_monitor_ack_without_event_is_not_readback(self):
        result, _ = self.execute(missingEvent=True)
        self.assertEqual(result["code"], 2); self.assertEqual(result["output"], [])

    def test_dockge_verifies_managed_stack_and_refuses_missing_or_foreign(self):
        result, _ = self.execute(service="dockge", fresh=True)
        self.assertEqual(result["code"], 0); self.assertEqual(result["calls"].count("setup"), 1)
        for fixture in ({"missingStack": True}, {"foreign": True}):
            result, _ = self.execute(service="dockge", **fixture)
            self.assertEqual(result["code"], 2)

    def test_negative_login_requires_actual_rejection(self):
        result, _ = self.execute(mode="deny")
        self.assertEqual(result["code"], 2)
        result, _ = self.execute(mode="deny", badLogin=True)
        self.assertEqual(result["code"], 0)
        self.assertTrue(json.loads(result["output"][0])["login_denied"])


if __name__ == "__main__":
    unittest.main()
