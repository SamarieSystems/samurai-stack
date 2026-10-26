"""Owned management accounts and additive monitors, never password recovery.

The Socket.IO client runs inside each exact owned container. Credentials travel
only on stdin. An existing owner must authenticate; a blank owner needs a
reviewed, host/directory/database-bound first-run claim. No SQL writes, account
resets, custom-monitor edits or notification delivery claims are performed.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from . import catalog, engine, naming, onboarding
from .safety import SafetyError, digest, no_symlinks, private_read, run, validate_config, write_json

SERVICES = ("dockge", "uptime-kuma")
DATABASES = {"dockge": "dockge.db", "uptime-kuma": "kuma.db"}


def _guard(c):
    engine.linux_only()
    if os.name == "posix" and os.geteuid() != 0:
        raise SafetyError("Management enrollment requires root inside the Linux guest")
    validate_config(c)
    engine.stage_allowed(c, 4)


def _paths(c):
    root, _ = onboarding._paths(c)
    return root, root / "state" / "management-onboarding.json"


def _receipt(c):
    root, path = _paths(c)
    if not path.exists():
        return {"schema_version": 1, "deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
                "bind_address": c["bind_address"], "apps": {}}
    value = onboarding._private_json(path)
    if (value.get("schema_version") != 1 or value.get("deployment_id") != c["deployment_id"]
            or value.get("host_id") != engine.host_identity() or value.get("bind_address") != c["bind_address"]
            or not isinstance(value.get("apps"), dict) or set(value["apps"]) - set(SERVICES)):
        raise SafetyError("Management account receipt belongs to a different installation")
    for name, row in value["apps"].items():
        if not isinstance(row, dict) or not onboarding.same_identity(row.get("directory"), onboarding._identity(root / "appdata" / name)):
            raise SafetyError("A management account directory was replaced; existing accounts were preserved")
    return value


def _save(c, value):
    _, path = _paths(c)
    write_json(path, value, replace=path.exists())


def reserve_fresh(c, *, runner=run):
    """Reserve empty owned directories before their first Compose startup.

    Call inside the engine deployment lock after appdirs are prepared. Existing
    projects/Compose files are ineligible, and reservations are never enlarged.
    """
    _guard(c)
    root, path = _paths(c)
    if path.exists():
        return _receipt(c)
    containers = engine.docker(runner, "ps", "--all", "--filter",
        "label=com.docker.compose.project=" + naming.project_name(c, 4), "--format", "{{.ID}}").stdout.strip()
    legacy = bool(containers) or no_symlinks(engine.compose_path(c, 4)).exists()
    receipt = _receipt(c)
    for name in SERVICES:
        directory = no_symlinks(root / "appdata" / name)
        identity = onboarding._identity(directory)
        owned = os.name != "posix" or directory.stat().st_uid in {0, c["puid"]}
        eligible = not legacy and owned and not any(directory.iterdir())
        receipt["apps"][name] = {"directory": identity, "eligible": eligible,
            "state": "reserved" if eligible else "existing"}
    _save(c, receipt)
    return receipt


def check_targets(c, runner=run):
    """Prove exact local published ports, private datastores and container IDs."""
    _guard(c)
    root, _ = _paths(c)
    interfaces = json.loads(runner(["ip", "-j", "address", "show"]).stdout)
    if (not isinstance(interfaces, list) or any(not isinstance(row, dict) or not isinstance(row.get("addr_info", []), list)
            or any(not isinstance(item, dict) for item in row.get("addr_info", [])) for row in interfaces)
            or c["bind_address"] not in {item.get("local") for row in interfaces for item in row.get("addr_info", [])}):
        raise SafetyError("Cannot prove the management address belongs to this guest")
    engine.check_docker(runner)
    expected_services = engine.recipe(c, 4)["services"]
    found = {}
    for name in SERVICES:
        ids = engine.docker(runner, "ps", "--filter", "status=running", "--filter",
            "label=com.docker.compose.project=" + naming.project_name(c, 4), "--filter",
            "label=com.docker.compose.service=" + name, "--format", "{{.ID}}").stdout.split()
        if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", ids[0]):
            raise SafetyError("A management target is not a unique running owned service")
        def inspect(template):
            return json.loads(engine.docker(runner, "inspect", ids[0], "--format", template).stdout)
        labels = inspect("{{json .Config.Labels}}")
        expected = {"systems.samarie.deployment": c["deployment_id"], "systems.samarie.stage": "4",
                    "com.docker.compose.project": naming.project_name(c, 4), "com.docker.compose.service": name}
        if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in expected.items()):
            raise SafetyError("Management target container belongs to another installation")
        image = expected_services[name]["image"]
        wanted_id = engine.docker(runner, "image", "inspect", image, "--format", "{{.Id}}").stdout.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", wanted_id) or inspect("{{json .Image}}") != wanted_id:
            raise SafetyError("Management target image does not match its reviewed pinned image")
        actual_config = inspect("{{json .Config}}")
        image_config = json.loads(engine.docker(runner, "image", "inspect", image, "--format", "{{json .Config}}").stdout)
        if (not isinstance(actual_config, dict) or not isinstance(image_config, dict)
                or any(actual_config.get(key) != image_config.get(key) for key in ("Cmd", "Entrypoint", "WorkingDir"))):
            raise SafetyError("Management command overrides prevent default-datastore attestation")
        ports = inspect("{{json .NetworkSettings.Ports}}")
        port = str(catalog.PORTS[name])
        bindings = ports.get(port + "/tcp") if isinstance(ports, dict) else None
        if not isinstance(bindings, list) or not bindings or any(not isinstance(row, dict)
                or row.get("HostIp") != c["bind_address"] or row.get("HostPort") != port for row in bindings):
            raise SafetyError("Management port does not match its exact private LAN binding")
        mounts = inspect("{{json .Mounts}}")
        if not isinstance(mounts, list) or any(not isinstance(row, dict) or not isinstance(row.get("Destination"), str) for row in mounts):
            raise SafetyError("Cannot prove management datastore mounts")
        data = [row for row in mounts if row["Destination"] == "/app/data"]
        if (len(data) != 1 or data[0].get("Type") != "bind" or data[0].get("Source") != str(root / "appdata" / name)
                or data[0].get("RW") is not True or any(row["Destination"].startswith("/app/data/") for row in mounts)):
            raise SafetyError("Management target does not use its exact owned datastore")
        if name == "dockge":
            for target, source in ((str(root / "stages"), str(root / "stages")), ("/var/run/docker.sock", "/var/run/docker.sock")):
                matching = [row for row in mounts if row["Destination"] == target]
                if len(matching) != 1 or matching[0].get("Type") != "bind" or matching[0].get("Source") != source:
                    raise SafetyError("Dockge does not use the intended local stacks and Docker socket")
        runtime = actual_config.get("Env")
        if not isinstance(runtime, list) or any(not isinstance(row, str) for row in runtime):
            raise SafetyError("Cannot prove the management database environment")
        for row in runtime:
            key, _, value = row.partition("=")
            if value and (key.startswith("UPTIME_KUMA_DB_") or key in {"DOCKGE_DATA_DIR", "UPTIME_KUMA_DATA_DIR"}):
                if key not in {"DOCKGE_DATA_DIR", "UPTIME_KUMA_DATA_DIR"} or value not in {"/app/data", "./data", "./data/"}:
                    raise SafetyError("Management target has an alternate datastore; existing accounts were preserved")
        found[name] = {"container": ids[0], "directory": onboarding._identity(root / "appdata" / name)}
    return found


def database_state(c, name, *, allow_uninitialized=False):
    """Read only account/monitor counts from the proven default SQLite file."""
    root, _ = _paths(c)
    directory = no_symlinks(root / "appdata" / name)
    config = directory / "db-config.json"
    if config.exists():
        if config.stat().st_size > 64 * 1024:
            raise SafetyError("Management database configuration is unknown")
        try:
            value = json.loads(config.read_text())
        except (OSError, ValueError):
            raise SafetyError("Management database configuration could not be read safely") from None
        if not isinstance(value, dict) or value.get("type") != "sqlite":
            raise SafetyError("Automatic management enrollment supports the owned default SQLite datastore only")
    path = no_symlinks(directory / DATABASES[name])
    if not path.exists():
        # A reviewed claim may configure a wholly new Kuma database. Existing
        # unknown files prevent turning a damaged datastore into a fresh owner.
        if name != "uptime-kuma" or (not allow_uninitialized and any(directory.iterdir())):
            raise SafetyError("Management database is missing or unknown; no account was recreated")
        return {"database": None, "users": [], "monitor_count": 0, "empty_directory": True}
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 ** 3:
        raise SafetyError("Management database is not a bounded regular file")
    for suffix in ("-wal", "-shm"):
        no_symlinks(Path(str(path) + suffix))
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in db.execute('PRAGMA table_info("user")')}
            if not {"id", "username", "password"} <= columns:
                raise SafetyError("Management account database schema is unknown")
            count = db.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]
            if not 0 <= count <= 64:
                raise SafetyError("Management account inventory exceeds the safe bound")
            users = [{"id": row[0], "username": row[1]} for row in db.execute('SELECT id, username FROM "user" ORDER BY id')]
            if any(type(row["id"]) is not int or not isinstance(row["username"], str) or len(row["username"]) > 256 for row in users):
                raise SafetyError("Management account inventory has an unknown shape")
            monitors = db.execute('SELECT COUNT(*) FROM "monitor"').fetchone()[0] if name == "uptime-kuma" else 0
            schema = db.execute("PRAGMA schema_version").fetchone()[0]
    except (sqlite3.Error, OSError):
        raise SafetyError("Management account database could not be verified read-only") from None
    after = path.stat()
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        raise SafetyError("Management database changed identity during account inspection")
    identity = onboarding._identity(path, directory=False)
    if (identity["device"], identity["inode"]) != (info.st_dev, info.st_ino):
        raise SafetyError("Management database changed identity during account inspection")
    return {"database": {**identity, "schema": schema},
            "users": users, "monitor_count": monitors, "empty_directory": False}


def plan(c, *, claim_unconfigured=False, runner=run):
    if type(claim_unconfigured) is not bool:
        raise SafetyError("Management first-run claims require an explicit boolean")
    targets = check_targets(c, runner)
    receipt = _receipt(c)
    apps = {}
    for name in SERVICES:
        saved = receipt["apps"].get(name, {})
        fresh_database = saved.get("eligible") is True and saved.get("state") in {"reserved", "intent"} and not saved.get("database_identity")
        state = database_state(c, name, allow_uninitialized=fresh_database)
        if saved.get("database_identity") and not onboarding.same_identity(saved["database_identity"], state["database"]):
            raise SafetyError("A management database was replaced; no account was recreated")
        if not state["users"]:
            if saved.get("state") == "verified" or saved.get("existing_owner_seen") is True:
                raise SafetyError("A verified management owner is missing; no replacement will be created")
            if state["monitor_count"]:
                raise SafetyError("Management has existing monitors but no owner; account recovery is required")
            if not claim_unconfigured and not saved.get("admin_intent") and saved.get("eligible") is not True:
                raise SafetyError("Unconfigured management accounts need an explicit reviewed first-run claim")
            action = "create_first_administrator"
        else:
            action = "verify_existing_login"
        apps[name] = {**targets[name], "database_state": state, "action": action}
    document = {"schema_version": 1, "deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
                "config_digest": digest(c), "claim_unconfigured": claim_unconfigured, "apps": apps}
    document["review_hash"] = digest(document)
    return document


def credentials(c, supplied=None):
    root, _ = _paths(c)
    path = root / "secrets" / "management_credentials.json"
    saved = onboarding._private_json(path) if path.exists() else None
    value = supplied if supplied is not None else saved
    if (not isinstance(value, dict) or set(value) != {"username", "password"}
            or not isinstance(value["username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}", value["username"])
            or not isinstance(value["password"], str) or not 8 <= len(value["password"]) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value["password"])):
        raise SafetyError("Enter a management username and an 8-256 character password through hidden input")
    if saved is not None and saved != value:
        raise SafetyError("Saved management credentials differ; no account was reset and no secret was overwritten")
    if saved is None:
        write_json(path, value)
    return value


def default_monitors(c):
    """Listener checks, explicitly not full playback/download commissioning."""
    items = {}
    for stage in (2, 3, 4, 5, 6):
        if stage in {3, 5, 6} and not c["features"].get({3: "torrents", 5: "photos", 6: "music"}[stage]):
            continue
        for name in catalog.stage_endpoints(c, stage):
            # Keep monitoring private; no external URL or arbitrary scan input.
            host = catalog.role_host(c, stage)
            port = catalog.PORTS[name]
            items[name] = {"name": "Samurai " + c["deployment_name"] + " / " + name,
                "type": "port", "hostname": host, "port": port, "interval": 60,
                "retryInterval": 60, "maxretries": 2, "resendInterval": 0,
                "active": True, "notificationIDList": {}, "accepted_statuscodes": ["200-299"],
                "conditions": [], "kafkaProducerBrokers": [], "kafkaProducerSaslOptions": {}, "rabbitmqNodes": []}
    return [items[key] for key in sorted(items)]


SOCKET_SCRIPT = r'''
const fs=require("fs"),{io}=require("socket.io-client");
const p=JSON.parse(fs.readFileSync(0,"utf8"));
const s=io(p.url,{transports:["websocket"],reconnection:false,timeout:10000,extraHeaders:{Origin:p.url}});
const timer=setTimeout(()=>{s.close();process.exit(2);},60000);
let setupAnnounced=false;
s.on("setup",()=>{setupAnnounced=true;});
const call=(...args)=>new Promise((resolve,reject)=>s.timeout(15000).emit(...args,(error,value)=>error?reject(new Error("transport")):resolve(value)));
function good(value){if(!value||value.ok!==true)throw new Error("refused");return value;}
function event(name,predicate=()=>true){return new Promise((resolve,reject)=>{
 const timeout=setTimeout(()=>{s.off(name,listener);reject(new Error("event"));},15000);
 function listener(...args){if(!predicate(...args))return;clearTimeout(timeout);s.off(name,listener);resolve(args);}
 s.on(name,listener);
});}
async function needsSetup(){for(let i=0;i<3;i++){try{const value=await new Promise((resolve,reject)=>s.timeout(3000).emit("needSetup",(e,v)=>e?reject(e):resolve(v)));if(typeof value!=="boolean")throw new Error("shape");return value;}catch(e){if(i===2)throw e;}}}
async function monitors(){const pending=event("monitorList",x=>x&&typeof x==="object"&&!Array.isArray(x));good(await call("getMonitorList"));return(await pending)[0];}
s.on("connect_error",()=>{clearTimeout(timer);s.close();process.exit(2);});
s.on("connect",async()=>{try{
 let fresh=false;
 if(p.service==="uptime-kuma")fresh=await needsSetup();
 if(fresh&&p.allow_setup===true)good(await call("setup",p.username,p.password));
 let login=await call("login",{username:p.username,password:p.password});
 if(p.mode==="deny"){
   if(!login||login.ok!==false)throw new Error("password-not-denied");
   console.log(JSON.stringify({login_denied:true}));clearTimeout(timer);s.close();process.exit(0);return;
 }
 if((!login||login.ok!==true)&&p.service==="dockge"&&setupAnnounced&&p.allow_setup===true){
   good(await call("setup",p.username,p.password));login=await call("login",{username:p.username,password:p.password});
 }
 good(login);let result={admin_login:true};
 if(p.service==="dockge"){
   const pending=event("agent",(kind,value)=>kind==="stackList"&&value&&value.endpoint==="");
   good(await call("agent","","requestStackList"));const[,current]=await pending;good(current);
   result.projects=[];
   for(const name of p.projects){
     const detail=good(await call("agent","","getStack",name)).stack;
     const state=current.stackList&&current.stackList[name];
     const services=good(await call("agent","","serviceStatusList",name)).serviceStatusList;
     if(!detail||detail.name!==name||detail.isManagedByDockge!==true||!state||![1,2,3,4].includes(state.status)||!services||typeof services!=="object"||Array.isArray(services))throw new Error("project");
     result.projects.push({name,managed:true,status:state.status,service_status:services});
   }
 }else{
   let current=await monitors();const added=[];
   for(const wanted of p.monitors){
     const same=Object.values(current).filter(row=>row&&row.name===wanted.name);
     if(same.length>1)throw new Error("duplicate-monitor");
     if(!same.length){if(p.write_monitors!==true)throw new Error("missing-monitor");good(await call("add",wanted));added.push(wanted.name);continue;}
     const actual=same[0];
     if(actual.type!==wanted.type||actual.hostname!==wanted.hostname||Number(actual.port)!==wanted.port||Number(actual.interval)!==wanted.interval||Number(actual.retryInterval)!==wanted.retryInterval||Number(actual.maxretries)!==wanted.maxretries||actual.active!==true)throw new Error("existing-monitor-differs");
   }
   current=await monitors();result.monitors=[];
   for(const wanted of p.monitors){const rows=Object.values(current).filter(row=>row&&row.name===wanted.name);if(rows.length!==1||rows[0].type!==wanted.type||rows[0].hostname!==wanted.hostname||Number(rows[0].port)!==wanted.port||rows[0].active!==true)throw new Error("monitor-readback");result.monitors.push({name:wanted.name,id:rows[0].id,type:"port",hostname:wanted.hostname,port:wanted.port,created:added.includes(wanted.name)});}
   result.authenticated_monitor_list=true;
 }
 console.log(JSON.stringify(result));clearTimeout(timer);s.close();process.exit(0);
}catch(e){clearTimeout(timer);s.close();process.exit(2);}});
'''


def socket_check(c, service, target, creds, *, allow_setup=False, write_monitors=False, mode="verify", runner=run):
    root, _ = _paths(c)
    projects = [p.name for p in no_symlinks(root / "stages").iterdir() if p.is_dir() and no_symlinks(p / "compose.yaml").is_file()]
    if len(projects) > 32:
        raise SafetyError("Too many managed Compose sets for bounded management enrollment")
    payload = {"service": service, "url": "http://127.0.0.1:" + str(catalog.PORTS[service]),
        **creds, "allow_setup": allow_setup, "write_monitors": write_monitors, "mode": mode,
        "projects": sorted(projects), "monitors": default_monitors(c) if service == "uptime-kuma" else []}
    try:
        result = runner(["docker", "--context", "default", "exec", "-i", "-w", "/app", target["container"],
                         "node", "-e", SOCKET_SCRIPT], input=json.dumps(payload), timeout=75)
        if getattr(result, "returncode", 0) or len(result.stdout) > 512 * 1024:
            raise ValueError()
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or (report.get("login_denied") is not True if mode == "deny" else report.get("admin_login") is not True):
            raise ValueError()
        return report
    except Exception:
        raise SafetyError("Management authentication or exact monitor/project readback failed; no account was reset") from None


def _kuma_database(c, allow_setup, request):
    base = catalog.stage_endpoints(c, 4)["uptime-kuma"]
    deadline = time.monotonic() + 45

    def readiness(require_ready=False):
        for attempt in range(30):
            if time.monotonic() >= deadline:
                break
            try:
                value = request("GET", base + "/setup-database-info", timeout=2)
            except Exception:
                # The database can be present before its HTTP listener starts.
                # Probe reads alone are retried; vendor exceptions stay private.
                value = None
            else:
                if (not isinstance(value, dict) or type(value.get("needSetup")) is not bool
                        or "runningSetup" in value and type(value["runningSetup"]) is not bool):
                    raise SafetyError("Kuma database readiness has an unknown schema")
                if (not require_ready or value["needSetup"] is False) and value.get("runningSetup") is not True:
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt == 29:
                break
            time.sleep(min(1, remaining))
        raise SafetyError("Kuma database API did not become ready; saved first-run intent was retained")

    value = readiness()
    if value["needSetup"]:
        if not allow_setup:
            raise SafetyError("Kuma needs its reviewed first-run database setup; existing state was preserved")
        try:
            changed = request("POST", base + "/setup-database", json={"dbConfig": {"type": "sqlite"}}, timeout=5)
        except Exception:
            raise SafetyError("Kuma database setup was not confirmed; saved first-run intent was retained") from None
        if not isinstance(changed, dict) or changed.get("ok") is not True:
            raise SafetyError("Kuma did not confirm its reviewed SQLite setup")
        readiness(require_ready=True)


def apply(c, document, accepted, supplied=None, *, runner=run, request=engine.request, add_monitors=True):
    """Apply only the exact first-run claim; initialized owners authenticate."""
    _guard(c)
    if not isinstance(document, dict) or not isinstance(accepted, str) or type(add_monitors) is not bool:
        raise SafetyError("Management enrollment needs its exact reviewed plan and explicit monitor choice")
    fresh = plan(c, claim_unconfigured=document.get("claim_unconfigured", False), runner=runner)
    if document != fresh or accepted != fresh["review_hash"]:
        raise SafetyError("Management review changed; create a fresh plan before any account write")
    with engine.deployment_lock(c):
        if plan(c, claim_unconfigured=document["claim_unconfigured"], runner=runner) != document:
            raise SafetyError("Management identity changed while taking its lock; no account was changed")
        creds = credentials(c, supplied)
        receipt = _receipt(c)
        reports = {}
        for name in SERVICES:
            target = check_targets(c, runner)[name]
            row = document["apps"][name]
            if target != {key: row[key] for key in ("container", "directory")}:
                raise SafetyError("Management target changed after review")
            entry = receipt["apps"].setdefault(name, {"directory": target["directory"], "state": "existing"})
            allow_setup = row["action"] == "create_first_administrator"
            if not allow_setup:
                entry["existing_owner_seen"] = True
            if allow_setup:
                fresh_database = entry.get("eligible") is True and not entry.get("database_identity")
                if database_state(c, name, allow_uninitialized=fresh_database) != row["database_state"]:
                    raise SafetyError("Management account state changed after review; no owner was replaced")
                entry.update(admin_intent=True, state="intent")
                _save(c, receipt)
            if name == "uptime-kuma":
                _kuma_database(c, allow_setup, request)
            current = database_state(c, name)
            reviewed_database = row["database_state"]["database"]
            if reviewed_database and not onboarding.same_identity(reviewed_database, current["database"]):
                raise SafetyError("Management database changed after review; no owner was replaced")
            entry["database_identity"] = current["database"]
            _save(c, receipt)
            report = socket_check(c, name, target, creds, allow_setup=allow_setup,
                                  write_monitors=add_monitors, runner=runner)
            state = database_state(c, name)
            if not any(user["username"] == creds["username"] for user in state["users"]):
                raise SafetyError("Management authenticated owner is absent from its owned datastore")
            denied = socket_check(c, name, target, {"username": creds["username"], "password": secrets.token_urlsafe(36)},
                                  mode="deny", runner=runner)
            if denied.get("login_denied") is not True:
                raise SafetyError("Management incorrectly accepted an invalid password")
            entry.update(state="verified", admin_verified=True, incorrect_password_rejected=True)
            _save(c, receipt)
            reports[name] = report
        return {"ok": True, "status": "verified", "services": reports,
                "notification_delivery": "not_configured_or_tested", "monitor_scope": "private TCP listener checks"}


def has_enrollment(c):
    """Distinguish a completed enrollment from an empty-directory reservation."""
    candidate = no_symlinks(Path(c["install_root"]) / "state" / "management-onboarding.json")
    if not candidate.exists():
        return False
    root, path = _paths(c)
    if not path.exists():
        return False
    receipt = _receipt(c)
    return (all(receipt["apps"].get(name, {}).get("state") == "verified" for name in SERVICES)
            and no_symlinks(root / "secrets" / "management_credentials.json").is_file())


def verify(c, *, runner=run, request=engine.request):
    """Authenticate saved accounts and read existing monitors; never enroll."""
    targets = check_targets(c, runner)
    receipt = _receipt(c)
    creds = credentials(c)
    reports = {}
    for name in SERVICES:
        if receipt["apps"].get(name, {}).get("state") != "verified":
            raise SafetyError("Management accounts need a reviewed enrollment before verification")
        state = database_state(c, name)
        saved = receipt["apps"][name].get("database_identity")
        if not saved or not onboarding.same_identity(saved, state["database"]):
            raise SafetyError("Management database changed identity; verification did not recreate any owner")
        if name == "uptime-kuma":
            _kuma_database(c, False, request)
        reports[name] = socket_check(c, name, targets[name], creds, runner=runner)
    return {"ok": True, "status": "verified", "services": reports,
            "notification_delivery": "not_configured_or_tested", "monitor_scope": "private TCP listener checks"}
