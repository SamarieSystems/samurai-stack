"""Installed optional-service floor guard. Stop only, never delete or resume."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def read_private(path):
    path = Path(path)
    for p in [path, *path.parents]:
        if p.is_symlink():
            raise ValueError("Optional capacity control paths cannot contain symlinks")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 262144:
        raise ValueError("Optional capacity input is not a bounded regular file")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077):
        raise ValueError("Optional capacity control must be private to root")
    return json.loads(path.read_text())


def command(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60)


def check(policy, *, usage=shutil.disk_usage, runner=command):
    """Validate both filesystems before making a narrowly scoped stop decision."""
    c = policy["config"]
    root = Path(c["storage"]["data_root"])
    expected = c["storage"]["expected_mount"]
    try:
        raw = runner(["findmnt", "--json", "--target", str(root), "--output", "TARGET,SOURCE,FSTYPE,UUID"])
        rows = json.loads(raw.stdout)["filesystems"]
        row = rows[0] if len(rows) == 1 else {}
        source = "UUID=" + row["uuid"] if expected["source"].startswith("UUID=") and row.get("uuid") else row.get("source")
        mount_ok = row.get("target") == expected["target"] and row.get("fstype") == expected["fstype"] and source == expected["source"]
    except Exception:
        mount_ok = False
    available = {}
    for key, path in (("data", root), ("app", c["install_root"])):
        try:
            available[key] = usage(path).free
        except OSError:
            available[key] = 0
    affected = []
    for stage in policy["stages"]:
        role = {5: "photos", 6: "music"}[stage]
        settings = c.get("extras", {}).get(role, {})
        if not mount_ok or any(available[key] <= settings.get(key + "_reserve_gib", default) * 1024**3
                               for key, default in (("data", 50), ("app", 20))):
            affected.append(stage)
    return affected, {"mount_verified": mount_ok, "available_bytes": available}


def enforce(policy, *, usage=shutil.disk_usage, runner=command, write_hold=None):
    affected, report = check(policy, usage=usage, runner=runner)
    c = policy["config"]
    if affected:
        if write_hold is None:
            raise ValueError("Capacity stop requires a durable hold writer")
        write_hold({"deployment_id": c["deployment_id"], "stages": affected,
                    "reason": "Capacity or backing-mount safety floor reached. No data removed or work resumed."})
        for stage in affected:
            # Exact generated installation units, not docker name prefixes or
            # all containers on the daemon. systemctl stop prevents restart loops.
            runner(["systemctl", "stop", "samurai-" + c["deployment_name"] + "-" + str(stage) + ".service"])
    return {"stopped_stages": affected, **report}


def main():
    path = Path(sys.argv[1])
    policy = read_private(path)
    c = policy["config"]
    if (policy.get("schema_version") != 1 or not policy.get("stages")
            or any(type(s) is not int or s not in {5, 6} for s in policy["stages"])
            or hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest() != policy["host_id"]
            or read_private(Path(c["install_root"]) / ".samurai-install.json")["deployment_id"] != c["deployment_id"]
            or path != Path(c["install_root"]) / "state" / "extras-capacity-policy.json"):
        raise ValueError("Optional capacity identity mismatch")
    for stage in policy["stages"]:
        unit = Path("/etc/systemd/system") / ("samurai-" + c["deployment_name"] + "-" + str(stage) + ".service")
        if unit.is_symlink() or ("# Managed by Samurai Stack: " + c["deployment_id"] + "\n") != unit.read_text().splitlines(keepends=True)[0]:
            raise ValueError("Optional capacity unit is not owned by this installation")
    def hold(value):
        target = path.with_name("extras-capacity-hold.json")
        old = read_private(target) if target.exists() else {"stages": []}
        value["stages"] = sorted(set(old["stages"]) | set(value["stages"]))
        fd, name = tempfile.mkstemp(prefix=".extras-hold-", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    print(json.dumps(enforce(policy, write_hold=hold)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Optional capacity guard could not prove its local prerequisites; inspect its private policy and units.", file=sys.stderr)
        raise SystemExit(1)
