"""Reviewed native capacity floors and an optional owned qBittorrent watcher."""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlencode

from . import capacity_guard as guard, engine, hardlinks, integrations
from .safety import SafetyError, atomic_write, digest, no_symlinks, private_read, validate_config, write_json

GIB = 1024 ** 3
UNIT_ROOT = Path("/etc/systemd/system")
LIMITATIONS = ["Reserves are pause/import thresholds, not filesystem quotas; checks cannot guarantee zero overshoot.",
               "Already-running extraction, other applications and filesystem metadata can still consume space.",
               "The watcher never deletes files, resumes downloads, stops seeders or changes VPN settings.",
               "Native SAB floors cover its configured temporary/completed download filesystem; the optional watcher also checks application storage."]


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except guard.GuardError as exc:
            raise SafetyError(str(exc)) from None
    return wrapped


def policy_path(c):
    return Path(c["install_root"]) / "state" / "capacity-policy.json"


def _reserve(value):
    if type(value) is not int or not 1 <= value <= 1024 * 1024:
        raise SafetyError("Capacity reserves must be whole GiB values from 1 to 1048576")
    return value * GIB


def _bytes(value):
    text = str(value).strip()
    if text == "":
        return 0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B)?", text, re.I)
    if not match:
        raise SafetyError("SAB returned an unsupported free-space threshold")
    from decimal import Decimal
    result = Decimal(match[1]) * 1024 ** (" KMGTPE".index(match[2].upper()) if match[2] else 0)
    if not 0 <= result <= 2 ** 60:
        raise SafetyError("SAB returned an excessive free-space threshold")
    return int(result)


class Inventory:
    def __init__(self, c, data_bytes, request, resolver, runner):
        self.config, self.request, self.resolver, self.runner = c, request, resolver, runner
        self.run = integrations._Run(c, 2, request, resolver, False)
        self.rows = {}
        if c["hosts"]["media"] != c["bind_address"]:
            return
        hardlinks.check_targets(c, runner)
        for service in ("sonarr", "radarr"):
            current = self.run.api(service, "/api/v3/config/mediamanagement")
            fields = {"skipFreeSpaceCheckWhenImporting": False,
                      "minimumFreeSpaceWhenImporting": data_bytes // (1024 ** 2)}
            if (not isinstance(current, dict) or type(current.get("id")) is not int or current["id"] < 1
                    or type(current.get("skipFreeSpaceCheckWhenImporting")) is not bool
                    or type(current.get("minimumFreeSpaceWhenImporting")) is not int
                    or not 0 <= current["minimumFreeSpaceWhenImporting"] <= 2147483647):
                raise SafetyError("Starr lacks supported native free-space fields; no capacity policy was accepted")
            fields["minimumFreeSpaceWhenImporting"] = max(fields["minimumFreeSpaceWhenImporting"], current["minimumFreeSpaceWhenImporting"])
            if "skipFreeSpaceCheckWhenGrabbing" in current:
                if type(current["skipFreeSpaceCheckWhenGrabbing"]) is not bool:
                    raise SafetyError("Starr grabbing free-space check has an unsupported schema")
                fields["skipFreeSpaceCheckWhenGrabbing"] = False
            self.rows[service] = {"before": {key: current[key] for key in fields}, "after": fields,
                                  "snapshot_digest": digest(current), "resource": current}
        if c.get("features", {}).get("usenet", True):
            guard.prove_service(c, "sabnzbd", 2, 8080, runner)
            current = self.sab("get_config", section="misc").get("config", {}).get("misc", {})
            if (not isinstance(current, dict) or not all(key in current for key in ("download_free", "complete_free", "fulldisk_autoresume"))
                    or type(current["fulldisk_autoresume"]) not in {bool, int}
                    or current["fulldisk_autoresume"] not in {False, True, 0, 1}):
                raise SafetyError("SAB lacks supported native free-space fields")
            fields = {key: str(data_bytes) if _bytes(current[key]) < data_bytes else current[key]
                      for key in ("download_free", "complete_free")}
            fields["fulldisk_autoresume"] = False
            before = {key: current[key] for key in fields}
            self.rows["sabnzbd"] = {"before": before, "after": fields, "snapshot_digest": digest(before)}

    def sab(self, mode, **fields):
        query = urlencode({"output": "json", "apikey": self.run.key("sab_api_key"), "mode": mode, **fields})
        value = self.run.call("POST" if mode == "set_config" else "GET", self.run.media["sabnzbd"] + "/api?" + query)
        if not isinstance(value, dict) or value.get("status") is False:
            raise SafetyError("SAB rejected the capacity operation")
        return value

    def public(self):
        return {name: {key: deepcopy(row[key]) for key in ("before", "after", "snapshot_digest")}
                for name, row in self.rows.items()}


def _saved(c):
    path = no_symlinks(policy_path(c))
    return guard.load_policy(path) if path.exists() else None


def _units(c, unit_root):
    root = Path(unit_root)
    name = "samurai-" + c["deployment_name"] + "-capacity-guard"
    marker = "# Managed by Samurai Stack: " + c["deployment_id"] + "\n"
    service = marker + "[Unit]\nDescription=Samurai capacity pause guard\nAfter=docker.service network-online.target\nRequires=docker.service\n[Service]\nType=oneshot\nUMask=0077\nExecStart=/usr/bin/python3 " + c["install_root"] + "/capacity_guard.py " + str(policy_path(c)) + "\nTimeoutStartSec=180\n"
    timer = marker + "[Unit]\nDescription=Samurai capacity pause checks\n[Timer]\nOnBootSec=60\nOnUnitActiveSec=30\nAccuracySec=1\n[Install]\nWantedBy=timers.target\n"
    return {root / (name + ".service"): service, root / (name + ".timer"): timer}


def _unit_snapshot(c, root):
    result = {}
    for path, expected in _units(c, root).items():
        no_symlinks(path)
        if path.exists():
            current = guard.read(path, private=False)
            if current != expected:
                raise SafetyError("An existing capacity service/timer is not this exact generated owned unit; it was retained")
            result[path.name] = digest(current)
        else:
            result[path.name] = None
    return result


def _inputs(c, data_reserve_gib, app_reserve_gib, qbit_guard, runner, request, resolver, unit_root):
    engine.linux_only()
    validate_config(c)
    engine.naming.check_installed_names(c)
    marker = json.loads(guard.read(Path(c["install_root"]) / ".samurai-install.json"))
    if not isinstance(marker, dict) or marker.get("deployment_id") != c["deployment_id"]:
        raise SafetyError("Capacity policy does not own this application directory")
    for name in ("state", "backups"):
        directory = no_symlinks(Path(c["install_root"]) / name).stat()
        guard.owner(directory)
        if not stat.S_ISDIR(directory.st_mode) or os.name == "posix" and directory.st_mode & 0o077:
            raise SafetyError("Capacity state and backup directories must remain private to root")
    data_bytes, app_bytes = _reserve(data_reserve_gib), _reserve(app_reserve_gib)
    previous = _saved(c)
    script = no_symlinks(Path(c["install_root"]) / "capacity_guard.py")
    if script.exists() and guard.read(script) != Path(guard.__file__).read_text():
        raise SafetyError("Existing capacity watcher source differs; it was retained")
    enabled = previous["qbit_guard"] if qbit_guard is None and previous else False if qbit_guard is None else qbit_guard
    if type(enabled) is not bool:
        raise SafetyError("The optional capacity watcher choice must be true or false")
    if enabled:
        if not c.get("features", {}).get("torrents", True) or c["hosts"]["torrent"] != c["bind_address"]:
            raise SafetyError("The optional capacity watcher must run on this installation's torrent guest")
        guard.qbit_backend(c, runner, None if request is engine.request else request, resolver)
    space = guard.capacities(c, runner)
    inventory = Inventory(c, data_bytes, request, resolver, runner)
    if not inventory.rows and not enabled:
        raise SafetyError("No local download applications were selected for capacity protection")
    return data_bytes, app_bytes, enabled, space, inventory, _unit_snapshot(c, unit_root)


@_safe
def plan(config, *, data_reserve_gib=50, app_reserve_gib=20, qbit_guard=None,
         runner=None, request=None, secret_resolver=None, unit_root=None):
    runner, request = runner or engine.run, request or engine.request
    resolver, root = secret_resolver or engine.secret_resolver(config), unit_root or UNIT_ROOT
    data_bytes, app_bytes, enabled, space, inventory, units = _inputs(config, data_reserve_gib, app_reserve_gib,
        qbit_guard, runner, request, resolver, root)
    document = {"schema_version": 1, "kind": "capacity-policy", "deployment_id": config["deployment_id"],
                "host_id": engine.host_identity(), "config_digest": digest(config),
                "data_reserve_bytes": data_bytes, "app_reserve_bytes": app_bytes, "qbit_guard": enabled,
                "capacity": space, "native": inventory.public(), "units": units, "limitations": LIMITATIONS}
    document["review_hash"] = digest(document)
    return document


def _comparison(document):
    result = deepcopy(document)
    result.pop("review_hash", None)
    # Available bytes are volatile while existing applications remain online.
    for value in result.get("capacity", {}).values():
        value.pop("available_bytes", None)
    return result


@_safe
def apply(config, reviewed_plan, *, runner=None, request=None, secret_resolver=None, unit_root=None):
    runner, request = runner or engine.run, request or engine.request
    resolver, root = secret_resolver or engine.secret_resolver(config), unit_root or UNIT_ROOT
    if os.name == "posix" and os.geteuid() != 0:
        raise SafetyError("Apply capacity policy with sudo/root inside the target guest")
    signed = dict(reviewed_plan)
    accepted = signed.pop("review_hash", None)
    if not accepted or digest(signed) != accepted or signed.get("kind") != "capacity-policy":
        raise SafetyError("Review an intact capacity plan before applying it")
    with engine.deployment_lock(config):
        current = plan(config, data_reserve_gib=reviewed_plan["data_reserve_bytes"] // GIB,
                       app_reserve_gib=reviewed_plan["app_reserve_bytes"] // GIB, qbit_guard=reviewed_plan["qbit_guard"],
                       runner=runner, request=request, secret_resolver=resolver, unit_root=root)
        if _comparison(current) != _comparison(reviewed_plan):
            raise SafetyError("Capacity settings or identities changed after review; no settings were accepted")
        inventory = Inventory(config, current["data_reserve_bytes"], request, resolver, runner)
        backup = no_symlinks(Path(config["install_root"]) / "backups" / ("capacity-" + accepted + ".json"))
        if not backup.parent.is_dir():
            raise SafetyError("The existing private backup directory is missing; capacity setup did not recreate it")
        if not backup.exists():
            write_json(backup, {"deployment_id": config["deployment_id"], "plan": reviewed_plan,
                                "previous_policy": _saved(config), "native": inventory.public()})
        for service, row in inventory.rows.items():
            if service == "sabnzbd":
                for key, expected in row["after"].items():
                    if row["before"][key] != expected:
                        inventory.sab("set_config", section="misc", keyword=key, value=int(expected) if type(expected) is bool else expected)
                saved = inventory.sab("get_config", section="misc").get("config", {}).get("misc", {})
                if any((_bytes(saved.get(key, "")) != _bytes(expected) if key != "fulldisk_autoresume" else saved.get(key) not in (False, 0))
                       for key, expected in row["after"].items()):
                    raise SafetyError("SAB did not retain the reviewed capacity floors")
            else:
                api = inventory.run.api
                latest = api(service, "/api/v3/config/mediamanagement")
                if digest(latest) != row["snapshot_digest"]:
                    raise SafetyError("Starr media settings changed during capacity review")
                if any(latest[key] != expected for key, expected in row["after"].items()):
                    proposed = {**deepcopy(latest), **row["after"]}
                    api(service, "/api/v3/config/mediamanagement/" + str(latest["id"]), "PUT", proposed)
                    if api(service, "/api/v3/config/mediamanagement") != proposed:
                        raise SafetyError("Starr capacity readback differs; inspect the saved private backup")
        policy = {"schema_version": 1, "deployment_id": config["deployment_id"], "host_id": engine.host_identity(),
                  "config_digest": digest(config), "config": config, "app_mount": current["capacity"]["app"]["mount"],
                  "data_reserve_bytes": current["data_reserve_bytes"], "app_reserve_bytes": current["app_reserve_bytes"],
                  "qbit_guard": current["qbit_guard"], "review_hash": accepted}
        target = policy_path(config)
        write_json(target, policy, replace=target.exists())
        units = _units(config, root)
        if policy["qbit_guard"]:
            script = no_symlinks(Path(config["install_root"]) / "capacity_guard.py")
            if script.exists() and guard.read(script) != Path(guard.__file__).read_text():
                raise SafetyError("Existing capacity watcher source differs; it was retained")
            if not script.exists():
                atomic_write(script, Path(guard.__file__).read_text())
            _unit_snapshot(config, root)
            for path, text in units.items():
                if not path.exists():
                    atomic_write(path, text, mode=0o644)
            guard.call(runner, ["systemctl", "daemon-reload"])
            timer = next(path.name for path in units if path.suffix == ".timer")
            guard.call(runner, ["systemctl", "enable", "--now", timer])
            guard.run_policy(target, runner=runner, request=None if request is engine.request else request)
        elif any(path.exists() for path in units):
            _unit_snapshot(config, root)
            for path in units:
                guard.call(runner, ["systemctl", "stop", path.name])
                if path.suffix == ".timer":
                    guard.call(runner, ["systemctl", "disable", path.name])
        return verify(config, runner=runner, request=request, secret_resolver=resolver, unit_root=root)


@_safe
def verify(config, *, runner=None, request=None, secret_resolver=None, unit_root=None):
    runner, request = runner or engine.run, request or engine.request
    resolver, root = secret_resolver or engine.secret_resolver(config), unit_root or UNIT_ROOT
    saved = _saved(config)
    if saved is None:
        return {"ok": False, "status": "pending", "reason": "No reviewed capacity policy has been saved"}
    if saved["config_digest"] != digest(config):
        raise SafetyError("Saved capacity policy belongs to a different site configuration; review it again")
    inventory = Inventory(config, saved["data_reserve_bytes"], request, resolver, runner)
    differences = [name for name, row in inventory.rows.items() if any(
        (_bytes(row["before"][key]) != _bytes(value) if name == "sabnzbd" and key != "fulldisk_autoresume"
         else row["before"][key] != value) for key, value in row["after"].items())]
    if saved["qbit_guard"]:
        guard.qbit_backend(config, runner, None if request is engine.request else request, resolver)
        script = no_symlinks(Path(config["install_root"]) / "capacity_guard.py")
        if not script.exists() or guard.read(script) != Path(guard.__file__).read_text():
            differences.append("watcher-source")
        _unit_snapshot(config, root)
        for path in _units(config, root):
            if not path.exists():
                differences.append("watcher-units")
            elif path.suffix == ".timer":
                guard.call(runner, ["systemctl", "is-enabled", "--quiet", path.name])
                guard.call(runner, ["systemctl", "is-active", "--quiet", path.name])
    space = guard.capacities(config, runner)
    if space["app"]["mount"] != saved["app_mount"]:
        raise SafetyError("Application storage identity changed after capacity review")
    return {"ok": not differences, "status": "verified" if not differences else "pending", "differences": differences,
            "data_reserve_gib": saved["data_reserve_bytes"] // GIB, "app_reserve_gib": saved["app_reserve_bytes"] // GIB,
            "qbit_guard": saved["qbit_guard"], "capacity": space, "limitations": LIMITATIONS}
