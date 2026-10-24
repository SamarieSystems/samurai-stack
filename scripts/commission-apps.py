#!/usr/bin/env python3
"""Synthetic first-run enrollment for an explicitly marked disposable Linux VM.

This is a commissioning harness, not the production account installer. It never
claims provider connectivity, notification delivery, backups, or media playback.
All generated credentials remain in the deployment's private secrets directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from samurai_stack import catalog, engine
from samurai_stack.integrations import jellyfin_headers
from samurai_stack.safety import SafetyError, atomic_write, load_config, no_symlinks, private_read


MARKER = Path("/run/samurai-disposable-lab")
USER = "stacklab"
EMAIL = "stacklab@example.invalid"


def guard(config, confirmation, marker=MARKER):
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise SafetyError("Commissioning requires root inside the disposable Linux guest")
    no_symlinks(marker)
    info = marker.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise SafetyError("Disposable lab marker must be a root-owned mode-600 regular file")
    if confirmation != config["deployment_id"] or private_read(marker).strip() != confirmation:
        raise SafetyError("Disposable lab identity does not match this deployment")
    if any(host != config["bind_address"] for host in config["hosts"].values()):
        raise SafetyError("Commissioning refuses cross-host service targets")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((config["bind_address"], 0))


def secret(config, name, value=None):
    path = engine.secret_path(config, name)
    if path.exists():
        current = private_read(path).rstrip("\r\n")
        if value is not None and current != value:
            raise SafetyError("Existing private commissioning credential differs")
        return current
    created = value if value is not None else secrets.token_hex(24)
    atomic_write(path, created + "\n")
    return created


def password(config, service):
    return secret(config, "commission_" + service.replace("-", "_") + "_password")


def api(method, base, path, *, body=None, headers=None, form=None):
    return engine.request(method, base + path, headers=headers, json=body, form=form)


def assert_fields(actual, **expected):
    if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in expected.items()):
        raise SafetyError("Commissioning API readback differs from the expected synthetic account")
    return actual


def jellyfin(config):
    base = catalog.stage_endpoints(config, 2)["jellyfin"]
    pw = password(config, "jellyfin")
    public = api("GET", base, "/System/Info/Public")
    if public.get("StartupWizardCompleted", public.get("IsStartupWizardCompleted")) is not True:
        api("GET", base, "/Startup/User")
        api("POST", base, "/Startup/Configuration", body={"ServerName": "Samurai disposable lab",
            "UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
        api("POST", base, "/Startup/User", body={"Name": USER, "Password": pw})
        api("POST", base, "/Startup/RemoteAccess", body={"EnableRemoteAccess": True,
                                                               "EnableAutomaticPortMapping": False})
        api("POST", base, "/Startup/Complete")
    auth = api("POST", base, "/Users/AuthenticateByName", body={"Username": USER, "Pw": pw},
        headers={"Authorization": 'MediaBrowser Client="Samurai Commission", Device="Disposable VM", '
                 'DeviceId="samurai-disposable-commission", Version="0.1"'})
    user = assert_fields(auth.get("User"), Name=USER)
    assert_fields(user.get("Policy"), IsAdministrator=True)
    headers = jellyfin_headers(auth["AccessToken"])
    keys = api("GET", base, "/Auth/Keys", headers=headers).get("Items", [])
    managed = [k for k in keys if k.get("AppName") == "Samurai disposable commissioning"]
    if not managed:
        api("POST", base, "/Auth/Keys?" + urlencode({"app": "Samurai disposable commissioning"}), headers=headers)
        keys = api("GET", base, "/Auth/Keys", headers=headers).get("Items", [])
        managed = [k for k in keys if k.get("AppName") == "Samurai disposable commissioning"]
    if len(managed) != 1 or not managed[0].get("AccessToken"):
        raise SafetyError("Jellyfin API-key readback failed")
    secret(config, "jellyfin_api_key", managed[0]["AccessToken"])
    info = api("GET", base, "/System/Info", headers=jellyfin_headers(managed[0]["AccessToken"]))
    if not info.get("Id"):
        raise SafetyError("Jellyfin authenticated system readback failed")
    return {"admin_login": True, "api_key_readback": True, "server_version": info.get("Version")}


def seerr(config):
    endpoints = catalog.stage_endpoints(config, 2)
    base = endpoints["seerr"]
    public = api("GET", base, "/api/v1/settings/public")
    body = {"username": USER, "password": password(config, "jellyfin")}
    if public.get("mediaServerType") in {None, 4}:
        body.update({"hostname": catalog.role_host(config, 2), "port": 8096,
                     "useSsl": False, "urlBase": "", "email": EMAIL, "serverType": 2})
    account = api("POST", base, "/api/v1/auth/jellyfin", body=body)
    if account.get("id") != 1 or account.get("jellyfinUsername") != USER:
        raise SafetyError("Seerr synthetic owner login failed")
    settings = api("GET", base, "/api/v1/settings/main")
    key = settings.get("apiKey")
    if not isinstance(key, str) or len(key) < 16:
        raise SafetyError("Seerr owner API-key readback failed")
    secret(config, "seerr_api_key", key)
    initialized = api("POST", base, "/api/v1/settings/initialize")
    assert_fields(initialized, initialized=True)
    authenticated = api("GET", base, "/api/v1/auth/me", headers={"X-Api-Key": key})
    assert_fields(authenticated, id=1)
    return {"jellyfin_admin_login": True, "api_key_readback": True}


def starr(config, service):
    base = catalog.stage_endpoints(config, 2)[service]
    prefix = "/api/v1" if service == "prowlarr" else "/api/v3"
    headers = {"X-Api-Key": engine.secret_resolver(config)(service + "_api_key")}
    current = api("GET", base, prefix + "/config/host", headers=headers)
    if current.get("username") not in {None, "", USER}:
        raise SafetyError("Starr already has a different administrator")
    if not current.get("username"):
        pw = password(config, service)
        changed = {**current, "authenticationMethod": "forms", "authenticationRequired": "enabled",
                   "username": USER, "password": pw, "passwordConfirmation": pw}
        api("PUT", base, prefix + "/config/host/" + str(current.get("id", 1)), body=changed, headers=headers)
    saved = api("GET", base, prefix + "/config/host", headers=headers)
    assert_fields(saved, username=USER, authenticationMethod="forms", authenticationRequired="enabled")
    return {"forms_auth_configured": True, "authentication_required": True}


def bazarr(config):
    base = catalog.stage_endpoints(config, 2)["bazarr"]
    headers = {"X-API-KEY": engine.secret_resolver(config)("bazarr_api_key")}
    current = api("GET", base, "/api/system/settings", headers=headers)
    auth = current.get("auth", {})
    if auth.get("username") not in {None, "", USER}:
        raise SafetyError("Bazarr already has a different administrator")
    if not auth.get("username"):
        api("POST", base, "/api/system/settings", headers=headers, form={
            "settings-auth-type": "form", "settings-auth-username": USER,
            "settings-auth-password": password(config, "bazarr")})
    saved = api("GET", base, "/api/system/settings", headers=headers)
    assert_fields(saved.get("auth"), username=USER, type="form")
    return {"forms_auth_configured": True, "authenticated_settings_readback": True}


def sabnzbd(config):
    base = catalog.stage_endpoints(config, 2)["sabnzbd"]
    key = engine.secret_resolver(config)("sab_api_key")
    def call(mode, **fields):
        query = urlencode({"mode": mode, "output": "json", "apikey": key, **fields})
        data = api("POST" if mode == "set_config" else "GET", base, "/api?" + query)
        if isinstance(data, dict) and data.get("status") is False:
            raise SafetyError("SABnzbd setting was rejected")
        return data
    current = call("get_config", section="misc").get("config", {}).get("misc", {})
    if current.get("username") not in {None, "", USER}:
        raise SafetyError("SABnzbd already has a different administrator")
    if not current.get("username"):
        call("set_config", section="misc", keyword="username", value=USER)
        call("set_config", section="misc", keyword="password", value=password(config, "sabnzbd"))
    saved = call("get_config", section="misc").get("config", {}).get("misc", {})
    assert_fields(saved, username=USER)
    if not saved.get("password"):
        raise SafetyError("SABnzbd authentication password readback is empty")
    return {"ui_auth_configured": True, "authenticated_settings_readback": True,
            "nntp_provider_verified": False}


def immich(config):
    base = catalog.stage_endpoints(config, 5)["immich-server"]
    pw = password(config, "immich")
    public = api("GET", base, "/api/server/config")
    if public.get("isInitialized") is False:
        api("POST", base, "/api/auth/admin-sign-up", body={"email": EMAIL, "password": pw,
                                                                   "name": "Disposable lab administrator"})
    login = api("POST", base, "/api/auth/login", body={"email": EMAIL, "password": pw})
    assert_fields(login, isAdmin=True, userEmail=EMAIL)
    headers = {"Authorization": "Bearer " + login["accessToken"]}
    api("POST", base, "/api/system-metadata/admin-onboarding", body={"isOnboarded": True}, headers=headers)
    api("PUT", base, "/api/users/me/onboarding", body={"isOnboarded": True}, headers=headers)
    saved = api("GET", base, "/api/server/config")
    assert_fields(saved, isInitialized=True, isOnboarded=True)
    user = api("GET", base, "/api/users/me", headers=headers)
    assert_fields(user, isAdmin=True, email=EMAIL)
    secret(config, "commission_immich_token", login["accessToken"])
    return {"admin_login": True, "onboarding_readback": True,
            "backup_verified": False, "upload_playback_verified": False}


def navidrome(config):
    base = catalog.stage_endpoints(config, 6)["navidrome"]
    credentials = {"username": USER, "password": password(config, "navidrome")}
    try:
        login = api("POST", base, "/auth/login", body=credentials)
    except SafetyError:
        # The vendor rejects this endpoint if any administrator already exists.
        login = api("POST", base, "/auth/createAdmin", body=credentials)
    assert_fields(login, username=USER, isAdmin=True)
    token = login.get("token")
    if not token:
        raise SafetyError("Navidrome administrator token missing")
    users = api("GET", base, "/api/user", headers={"X-ND-Authorization": "Bearer " + token})
    if not isinstance(users, list) or not any(u.get("userName") == USER and u.get("isAdmin") is True for u in users):
        raise SafetyError("Navidrome administrator user readback failed")
    return {"admin_login": True, "authenticated_user_readback": True}


def music_assistant(config):
    base = catalog.stage_endpoints(config, 6)["music-assistant"]
    credentials = {"username": USER, "password": password(config, "music-assistant")}
    try:
        login = api("POST", base, "/auth/login", body={"provider_id": "builtin", "credentials": credentials})
    except SafetyError:
        login = api("POST", base, "/setup", body={**credentials, "display_name": "Disposable lab administrator"})
    assert_fields(login, success=True)
    assert_fields(login.get("user"), username=USER, role="admin")
    token = login.get("token")
    if not token:
        raise SafetyError("Music Assistant administrator token missing")
    me = api("GET", base, "/auth/me", headers={"Authorization": "Bearer " + token})
    # Upstream wraps the user in current versions; reject unknown identity shapes.
    user = me.get("user", me)
    assert_fields(user, username=USER, role="admin")
    return {"admin_login": True, "authenticated_user_readback": True,
            "provider_player_enrollment_verified": False}


SOCKET_SCRIPT = r'''
const fs = require("fs");
const { io } = require("socket.io-client");
const p = JSON.parse(fs.readFileSync(0, "utf8"));
const s = io(p.url, {transports:["websocket"], reconnection:false, timeout:10000,
    extraHeaders:{Origin:p.url}});
const timer = setTimeout(() => {s.close();process.exit(2);}, 45000);
let setupAnnounced=false;
s.on("setup", () => {setupAnnounced=true;});
const call = (...args) => new Promise((resolve,reject) => {
    s.timeout(15000).emit(...args, (err,res) => err ? reject(new Error("socket-timeout")) : resolve(res));
});
// Kuma registers handlers after an asynchronous connection callback. Only retry
// this read-only readiness query, never setup or another potentially written call.
async function kumaNeedsSetup() {
    for (let attempt=0;attempt<3;attempt++) {
        try {
            const value=await new Promise((resolve,reject) => {
                s.timeout(3000).emit("needSetup", (err,res) => err ? reject(err) : resolve(res));
            });
            if (typeof value !== "boolean") throw new Error("invalid-setup-state");
            return value;
        } catch (_) {if (attempt===2) throw new Error("socket-not-ready");}
    }
}
function eventValue(name, predicate=() => true) {
    return new Promise((resolve,reject) => {
        const timeout=setTimeout(() => {s.off(name,listener);reject(new Error("event-timeout"));},15000);
        function listener(...args) {
            if (!predicate(...args)) return;
            clearTimeout(timeout);s.off(name,listener);resolve(args);
        }
        s.on(name,listener);
    });
}
function good(res) {if (!res || res.ok !== true) throw new Error("socket-check-failed");return res;}
s.on("connect_error", () => {clearTimeout(timer);s.close();process.exit(2);});
s.on("connect", async () => {try {
    if (p.service === "uptime-kuma" && await kumaNeedsSetup()) {
        good(await call("setup", p.username, p.password));
    }
    let login = await call("login", {username:p.username,password:p.password});
    if ((!login || login.ok !== true) && p.service === "dockge" && setupAnnounced) {
        good(await call("setup", p.username, p.password));
        login = await call("login", {username:p.username,password:p.password});
    }
    good(login);
    let result={admin_login:true};
    if (p.service === "dockge") {
        const stackEvent=eventValue("agent", (event,reply) => event==="stackList" && reply && reply.endpoint==="");
        good(await call("agent", "", "requestStackList"));
        const [,current]=await stackEvent;
        good(current);
        const checked=[];
        for (const name of p.projects) {
            const reply=good(await call("agent", "", "getStack", name));
            if (!reply.stack || reply.stack.name!==name || reply.stack.isManagedByDockge!==true)
                throw new Error("stack-discovery-failed");
            // getStack creates a new detail object with UNKNOWN=0. The UI's
            // stackList and serviceStatusList query Docker for actual state.
            const stack=current.stackList && current.stackList[name];
            const services=good(await call("agent", "", "serviceStatusList", name)).serviceStatusList;
            if (!stack || ![1,2,3,4].includes(stack.status) || !services ||
                typeof services!=="object" || Array.isArray(services))
                throw new Error("stack-runtime-readback-failed");
            checked.push({name,managed:true,status:stack.status,service_status:services});
        }
        result.projects=checked;
    } else {
        const monitorEvent=eventValue("monitorList", list => list && typeof list==="object" && !Array.isArray(list));
        good(await call("getMonitorList"));
        await monitorEvent;
        result.authenticated_monitor_list=true;
    }
    console.log(JSON.stringify(result));
    clearTimeout(timer);s.close();process.exit(0);
} catch (_) {clearTimeout(timer);s.close();process.exit(2);}});
'''


def socket_app(config, service):
    if service == "uptime-kuma":
        base = catalog.stage_endpoints(config, 4)[service]
        setup = api("GET", base, "/setup-database-info")
        if setup.get("needSetup") is True:
            assert_fields(api("POST", base, "/setup-database", body={"dbConfig": {"type": "sqlite"}}), ok=True)
            for _ in range(30):
                try:
                    if api("GET", base, "/setup-database-info").get("needSetup") is False:
                        break
                except SafetyError:
                    pass
                time.sleep(1)
            else:
                raise SafetyError("Kuma database enrollment did not finish")
    args = ["docker", "--context", "default", *engine.compose_args(config, 4), "ps", "-q", service]
    found = subprocess.run(args, check=True, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
    if len(found) != 1:
        raise SafetyError("Commissioning container lookup failed")
    payload = {"service": service, "url": "http://127.0.0.1:" + str(catalog.PORTS[service]),
               "username": USER, "password": password(config, service), "projects": []}
    if service == "dockge":
        stages = Path(config["install_root"]) / "stages"
        payload["projects"] = sorted(p.name for p in stages.iterdir() if (p / "compose.yaml").is_file())
    result = subprocess.run(["docker", "exec", "-i", "-w", "/app", found[0], "node", "-e", SOCKET_SCRIPT],
                            input=json.dumps(payload), capture_output=True, text=True, timeout=55)
    if result.returncode:
        raise SafetyError("Socket account enrollment or authenticated readback failed")
    return json.loads(result.stdout)


def commission(config, stage):
    functions = {2: [("jellyfin", jellyfin), ("sonarr", lambda c: starr(c, "sonarr")),
                     ("radarr", lambda c: starr(c, "radarr")), ("prowlarr", lambda c: starr(c, "prowlarr")),
                     ("bazarr", bazarr), ("seerr", seerr)],
                 4: [("dockge", lambda c: socket_app(c, "dockge")),
                     ("uptime-kuma", lambda c: socket_app(c, "uptime-kuma"))],
                 5: [("immich", immich)], 6: [("navidrome", navidrome)]}
    if stage == 6 and config.get("features", {}).get("music_assistant"):
        functions[6].append(("music-assistant", music_assistant))
    if stage == 2 and config.get("features", {}).get("usenet"):
        functions[2].append(("sabnzbd", sabnzbd))
    report = {"stage": stage, "scope": "synthetic disposable account enrollment only", "checks": [], "errors": []}
    for name, function in functions[stage]:
        try:
            report["checks"].append({"service": name, "result": function(config)})
        except SafetyError as exc:
            # Only our own validation/transport messages are rendered. Vendor
            # bodies, raw exception text and credential-bearing URLs are not.
            report["errors"].append({"service": name, "error": str(exc)})
        except Exception:
            report["errors"].append({"service": name, "error": "Enrollment/readback failed; inspect the disposable service locally"})
    report["ok"] = not report["errors"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", type=int, choices=[2, 4, 5, 6], required=True)
    parser.add_argument("--confirm-disposable", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        guard(config, args.confirm_disposable)
        report = commission(config, args.stage)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 2
    except Exception:
        print("Commissioning refused or failed validation; no credential values displayed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
