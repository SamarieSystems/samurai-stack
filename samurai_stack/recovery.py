"""Read-only recovery evidence for lost source/configuration directories.

Container labels and mount paths locate possible original configuration; they
never authorize adoption, account changes or reconstruction by themselves.
Nothing here stops containers, copies open files, reads secrets, restores a
configuration or writes to the host. Missing appdata with running containers is
reported so the operator can preserve the remaining runtime state.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import uuid

from .safety import run


_IDENTIFIER = re.compile(r"[a-fA-F0-9]{12,64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_UNIT = re.compile(r"samurai-[a-z][a-z0-9-]{1,39}-[0-6]\.service\Z")
_TEMPLATE = ('{"id":{{json .Id}},"name":{{json .Name}},"status":{{json .State.Status}},'
    '"deployment_id":{{json (index .Config.Labels "systems.samarie.deployment")}},'
    '"stage":{{json (index .Config.Labels "systems.samarie.stage")}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"compose_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '"mounts":{{json .Mounts}}}')


def _name(value):
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _uuid(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _path(value):
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or ".." in PurePosixPath(value).parts):
        return None
    return str(PurePosixPath(value))


def _path_info(value):
    """Inspect only metadata, without following a candidate symlink."""
    result = {"path": value, "exists": False}
    try:
        item = Path(value).lstat()
        result.update(exists=True, directory=stat.S_ISDIR(item.st_mode),
                      symlink=stat.S_ISLNK(item.st_mode))
    except OSError:
        pass
    return result


def _command(runner, argv):
    try:
        result = runner(argv, timeout=20)
        if getattr(result, "returncode", 0) != 0 or not isinstance(result.stdout, str):
            return None
        if len(result.stdout) > 4 * 1024 * 1024:
            return None
        return result.stdout
    except Exception:
        # Neither command output nor exception strings belong in this report.
        return None


def _json_command(runner, argv):
    value = _command(runner, argv)
    try:
        return json.loads(value) if value is not None else None
    except ValueError:
        return None


def _container(value):
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or not _IDENTIFIER.fullmatch(value["id"]) or not isinstance(value.get("mounts"), list)):
        return None
    project, service = _name(value.get("project")), _name(value.get("service"))
    stage = value.get("stage")
    stage = int(stage) if isinstance(stage, str) and re.fullmatch(r"[0-6]", stage) else None
    row = {"id": value["id"], "name": _name(str(value.get("name", "")).lstrip("/")),
           "status": value.get("status") if value.get("status") in {
               "created", "running", "paused", "restarting", "removing", "exited", "dead"} else "unknown",
           "deployment_id": _uuid(value.get("deployment_id")), "stage": stage,
           "project": project, "service": service, "mounts": [],
           "compose_files": [], "working_dir": None}
    directory = _path(value.get("working_dir"))
    if directory:
        row["working_dir"] = _path_info(directory)
    compose = value.get("compose_files")
    if isinstance(compose, str):
        for item in compose.split(",")[:16]:
            candidate = _path(item)
            if candidate:
                row["compose_files"].append(_path_info(candidate))
    for item in value["mounts"][:128]:
        if not isinstance(item, dict):
            return None
        source, destination = _path(item.get("Source")), _path(item.get("Destination"))
        if not destination or type(item.get("RW")) is not bool:
            return None
        if item.get("Type") == "tmpfs" and item.get("Source") in {None, ""}:
            row["mounts"].append({"type": "tmpfs", "source": None, "destination": destination,
                                  "rw": item["RW"], "source_status": None, "appdata": False})
            continue
        if not source:
            return None
        appdata = ("appdata" in PurePosixPath(source).parts or destination in {
            "/config", "/app/config", "/cache", "/app/data", "/appdata", "/gluetun", "/var/lib/postgresql/data"})
        row["mounts"].append({"type": item.get("Type") if item.get("Type") in {"bind", "volume", "tmpfs"} else "unknown",
            "source": source, "destination": destination, "rw": item["RW"],
            "source_status": _path_info(source), "appdata": appdata})
    row["missing_appdata"] = any(item["appdata"] and not item["source_status"]["exists"] for item in row["mounts"])
    return row


def _systemd(systemd_dir):
    units = []
    try:
        paths = sorted(Path(systemd_dir).glob("samurai-*.service"))[:128]
    except OSError:
        return units
    for path in paths:
        try:
            item = path.lstat()
            if not _UNIT.fullmatch(path.name) or not stat.S_ISREG(item.st_mode) or item.st_size > 65536:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            markers = [line.partition(": ")[2] for line in lines if line.startswith("# Managed by Samurai Stack: ")]
            if len(markers) != 1 or not _uuid(markers[0]):
                continue
            config_paths = []
            for line in lines:
                if not line.startswith("ExecStartPre="):
                    continue
                parts = shlex.split(line.partition("=")[2])
                if (len(parts) == 4 and parts[0] == "/usr/bin/python3" and _path(parts[1])
                        and _path(parts[2]) and re.fullmatch(r"[0-6]", parts[3])):
                    config_paths.append(_path_info(_path(parts[2])))
            units.append({"unit": path.name, "deployment_id": _uuid(markers[0]), "config_candidates": config_paths})
        except (OSError, UnicodeError, ValueError):
            continue
    return units


def _deployment_candidates(containers, units):
    deployments = {}
    for row in containers:
        identifier = row["deployment_id"]
        if identifier is None:
            continue
        item = deployments.setdefault(identifier, {"deployment_id": identifier, "projects": set(),
                    "install_roots": set(), "data_roots": set(), "candidate_configs": set(), "running_missing_appdata": False})
        if row["project"]:
            item["projects"].add(row["project"])
        item["running_missing_appdata"] |= row["missing_appdata"] and row["status"] in {"running", "paused", "restarting"}
        for mount in row["mounts"]:
            if mount["source"] is None:
                continue
            source, destination = PurePosixPath(mount["source"]), mount["destination"]
            if mount["appdata"] and source.parent.name == "appdata":
                item["install_roots"].add(str(source.parent.parent))
            if destination == "/data" and not mount["appdata"]:
                item["data_roots"].add(str(source))
            elif destination == "/data/library" and source.name == "library":
                item["data_roots"].add(str(source.parent))
        for compose in row["compose_files"]:
            path = PurePosixPath(compose["path"])
            if path.name in {"compose.yaml", "compose.yml"}:
                item["candidate_configs"].add(str(path.parent / "site.json"))
    for unit in units:
        item = deployments.setdefault(unit["deployment_id"], {"deployment_id": unit["deployment_id"], "projects": set(),
            "install_roots": set(), "data_roots": set(), "candidate_configs": set(), "running_missing_appdata": False})
        project = unit["unit"][len("samurai-"):-len(".service")]
        item["projects"].add(project)
        item["candidate_configs"].update(row["path"] for row in unit["config_candidates"])
        for row in unit["config_candidates"]:
            path = PurePosixPath(row["path"])
            if path.name == "site.json" and path.parent.name == project and path.parent.parent.name == "stages":
                item["install_roots"].add(str(path.parent.parent.parent))
    result = []
    for value in deployments.values():
        for root in value["install_roots"]:
            for project in value["projects"]:
                value["candidate_configs"].add(str(PurePosixPath(root) / "stages" / project / "site.json"))
        value["projects"] = sorted(value["projects"])
        for name in ("install_roots", "data_roots", "candidate_configs"):
            value[name] = [_path_info(path) for path in sorted(value[name])]
        result.append(value)
    return sorted(result, key=lambda item: item["deployment_id"])


def _mount_report(value):
    if not isinstance(value, dict) or not isinstance(value.get("filesystems"), list):
        return None
    result = []
    for item in value["filesystems"][:32]:
        if not isinstance(item, dict):
            return None
        row = {}
        for name in ("target", "source", "fstype", "uuid"):
            entry = item.get(name)
            if entry is None or isinstance(entry, str) and len(entry) <= 4096 and not any(ord(char) < 32 for char in entry):
                # Never expose URL-style user:password credentials, even in an
                # unexpected custom mount source. Mount options are not read.
                row[name] = "<redacted source>" if name == "source" and isinstance(entry, str) and re.search(r":[^/@]+@", entry) else entry
        result.append(row)
    return {"filesystems": result}


def inventory(*, runner=run, systemd_dir=Path("/etc/systemd/system"), media_mount="/mnt/media"):
    """Discover recovery evidence without reading account/config/secret contents.

    Candidate paths and labels are evidence only. A caller must independently
    validate any saved site and the original deployment before offering restore.
    """
    report = {"schema_version": 1, "mode": "read_only_recovery_inventory", "containers": [],
              "deployments": [], "systemd_units": [], "media_mount": None, "warnings": []}
    # These host clues are independent of Docker. Preserve them even when the
    # daemon/CLI is unavailable or the default context points somewhere else.
    report["systemd_units"] = _systemd(systemd_dir)
    report["deployments"] = _deployment_candidates([], report["systemd_units"])
    mount = _path(media_mount)
    if mount:
        report["media_mount"] = _mount_report(_json_command(runner, ["findmnt", "--json", "--mountpoint", mount,
                                                                    "--output", "TARGET,SOURCE,FSTYPE,UUID"]))
    context = _command(runner, ["docker", "--context", "default", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"])
    if context is None or not context.strip().startswith("unix://"):
        report["warnings"].append("Local default Docker context unavailable; no container inventory performed")
        return report
    identifiers = _command(runner, ["docker", "--context", "default", "ps", "--all", "--filter",
                                    "label=systems.samarie.deployment", "--format", "{{.ID}}"])
    if identifiers is None:
        report["warnings"].append("Samurai container inventory was unavailable")
        return report
    for identifier in identifiers.split()[:128]:
        if not _IDENTIFIER.fullmatch(identifier):
            report["warnings"].append("An invalid container inventory entry was ignored")
            continue
        value = _json_command(runner, ["docker", "--context", "default", "inspect", identifier, "--format", _TEMPLATE])
        row = _container(value)
        if row is None:
            report["warnings"].append("One selected container could not be safely inspected")
        else:
            report["containers"].append(row)
    report["deployments"] = _deployment_candidates(report["containers"], report["systemd_units"])
    if any(item["running_missing_appdata"] for item in report["deployments"]):
        report["warnings"].append("Running containers have missing appdata paths; preserve them while checking recovery evidence")
    return report
