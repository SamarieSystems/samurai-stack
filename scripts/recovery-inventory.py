#!/usr/bin/env python3
"""Read-only Samurai recovery inventory. No secrets, account files or changes."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys


def command(argv, timeout=20):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def fact(value):
    path = Path(value)
    result = {"path": str(path), "exists": False}
    try:
        item = path.lstat()
        result.update(exists=True, directory=stat.S_ISDIR(item.st_mode), symlink=stat.S_ISLNK(item.st_mode))
    except OSError:
        pass
    return result


def deleted_fds(identifier):
    code, data = command(["docker", "--context", "default", "top", identifier, "-eo", "pid"])
    pids = [int(line.strip()) for line in data.splitlines() if line.strip().isdigit()][:256] if code == 0 else []
    counts = {"regular": 0, "socket": 0, "pipe": 0, "other": 0, "unreadable": 0}
    checked = 0
    for pid in pids:
        try:
            descriptors = list((Path("/proc") / str(pid) / "fd").iterdir())[:4096]
        except OSError:
            counts["unreadable"] += 1
            continue
        for descriptor in descriptors:
            checked += 1
            try:
                target = os.readlink(descriptor)
                if not target.endswith(" (deleted)"):
                    continue
                mode = descriptor.stat().st_mode
                kind = "regular" if stat.S_ISREG(mode) else "socket" if stat.S_ISSOCK(mode) else "pipe" if stat.S_ISFIFO(mode) else "other"
                counts[kind] += 1
            except OSError:
                counts["unreadable"] += 1
    return {"processes_checked": len(pids), "descriptors_checked": checked, "deleted_counts_by_type": counts}


def main():
    if not sys.platform.startswith("linux"):
        print("Run this read-only inventory inside the intended Linux VM.", file=sys.stderr)
        return 1
    report = {"read_only": True, "hostname": socket.gethostname(), "root": os.geteuid() == 0,
              "containers": [], "candidate_install_roots": [], "systemd": [], "warnings": []}
    code, endpoint = command(["docker", "--context", "default", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"])
    if code or not endpoint.strip().startswith("unix://"):
        report["warnings"].append("Local default Docker context unavailable; no container inventory performed")
        print(json.dumps(report, indent=2))
        return 1
    code, data = command(["docker", "--context", "default", "ps", "--all", "--filter", "label=systems.samarie.deployment", "--format", "{{.ID}}"])
    identifiers = data.split() if code == 0 else []
    roots = set()
    template = '{"Id":{{json .Id}},"Name":{{json .Name}},"State":{{json .State.Status}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}}}'
    for identifier in identifiers[:128]:
        if not re.fullmatch(r"[a-fA-F0-9]{12,64}", identifier):
            continue
        code, data = command(["docker", "--context", "default", "inspect", identifier, "--format", template])
        try:
            item = json.loads(data) if code == 0 else None
            if not isinstance(item, dict):
                raise ValueError()
        except ValueError:
            report["warnings"].append("One selected container could not be inspected")
            continue
        labels = item.get("Labels") or {}
        chosen = {name: labels.get(name) for name in (
            "systems.samarie.deployment", "systems.samarie.stage", "com.docker.compose.project", "com.docker.compose.service",
            "com.docker.compose.project.working_dir", "com.docker.compose.project.config_files")}
        mounts = []
        for mount in item.get("Mounts", []):
            source, destination = mount.get("Source"), mount.get("Destination")
            row = {"type": mount.get("Type"), "source": source, "destination": destination, "rw": mount.get("RW")}
            if isinstance(source, str) and source.startswith("/"):
                row["source_status"] = fact(source)
                path = PurePosixPath(source)
                if destination in {"/config", "/app/config"} and len(path.parts) >= 4 and path.parent.name == "appdata":
                    roots.add(str(path.parent.parent))
            mounts.append(row)
        row = {"id": item.get("Id"), "name": item.get("Name", "").lstrip("/"), "status": item.get("State"),
               "labels": chosen, "mounts": mounts}
        if item.get("State") == "running":
            row["open_deleted_descriptors"] = deleted_fds(identifier)
        report["containers"].append(row)
    for unit in sorted(Path("/etc/systemd/system").glob("samurai-*.service"))[:128]:
        try:
            if unit.is_symlink() or unit.stat().st_size > 65536:
                continue
            lines = unit.read_text().splitlines()
            marker = [line.partition(": ")[2] for line in lines if line.startswith("# Managed by Samurai Stack: ")]
            if len(marker) != 1:
                continue
            entry = {"unit": unit.name, "deployment_id": marker[0], "config_candidates": []}
            for line in lines:
                if line.startswith("ExecStartPre=/usr/bin/python3 "):
                    parts = line.split()
                    if len(parts) == 4 and parts[2].startswith("/"):
                        entry["config_candidates"].append(fact(parts[2]))
            report["systemd"].append(entry)
        except (OSError, UnicodeError):
            report["warnings"].append("A Samurai unit could not be read")
    for value in sorted(roots):
        root = Path(value)
        candidate = {"root": fact(value), "appdata": fact(root / "appdata"),
                     "marker": fact(root / ".samurai-install.json"), "runtime_site": fact(root / "site.json"), "stage_sites": []}
        for container in report["containers"]:
            project = container["labels"].get("com.docker.compose.project")
            if isinstance(project, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,100}", project):
                path = root / "stages" / project / "site.json"
                if str(path) not in {row["path"] for row in candidate["stage_sites"]}:
                    candidate["stage_sites"].append(fact(path))
        report["candidate_install_roots"].append(candidate)
    code, data = command(["findmnt", "--json", "--mountpoint", "/mnt/media", "--output", "TARGET,SOURCE,FSTYPE,UUID"])
    try:
        report["media_mount"] = json.loads(data) if code == 0 else {"mounted_at_exact_path": False}
    except ValueError:
        report["media_mount"] = {"inventory_error": True}
    report["warnings"].append("Do not stop or recreate running containers while appdata is missing; deleted open files may be the remaining recoverable copy")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
