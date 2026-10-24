#!/usr/bin/env bash
# VM prerequisites only. Default is read-only; no downloaded shell is executed.
set -euo pipefail
umask 077
exec python3 - "$0" "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import uuid

KEY_SHA256 = "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
# Both official Docker Debian/Ubuntu key endpoints verified 2026-10-23.
BASE = ["ca-certificates", "curl", "nfs-common", "qemu-guest-agent"]
DOCKER = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
CONFLICTS = ["docker.io", "docker-compose", "docker-compose-v2", "docker-doc", "docker-buildx", "podman", "podman-docker", "containerd", "runc"]
KEY = Path("etc/apt/keyrings/samurai-docker.asc")
SOURCE = Path("etc/apt/sources.list.d/samurai-docker.sources")
STATE = Path("var/lib/samurai-stack/host-bootstrap")


class BootstrapError(RuntimeError):
    pass


def runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)


def command(run, argv, allow_missing=False):
    try:
        result = run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"Cannot execute {argv[0]}: {type(exc).__name__}") from exc
    if result.returncode and not (allow_missing and result.returncode == 1):
        raise BootstrapError(f"{argv[0]} failed with exit {result.returncode}; inspect its local logs. Existing files and data were retained.")
    return result.stdout.strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe(path):
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise BootstrapError(f"Refusing symlink in managed target: {path}")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise BootstrapError(f"Refusing special-file target: {path}")
    return path


def write_private(path, data):
    safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def source_text(os_id, codename, arch):
    return ("# Managed by Samurai Stack host-bootstrap v1\n"
            f"Types: deb\nURIs: https://download.docker.com/linux/{os_id}\n"
            f"Suites: {codename}\nComponents: stable\nArchitectures: {arch}\n"
            "Signed-By: /etc/apt/keyrings/samurai-docker.asc\n")


def inspect(root, run, script_path):
    if platform.system() != "Linux" or sys.version_info < (3, 11):
        raise BootstrapError("Use Linux with Python 3.11 or newer inside the new guest")
    if (root / "etc/pve").exists():
        raise BootstrapError("Refusing a Proxmox host (/etc/pve exists); run this inside a new VM")
    if (root / "proc/1/comm").read_text().strip() != "systemd":
        raise BootstrapError("The guest must boot with systemd as PID 1")
    vm = command(run, ["systemd-detect-virt", "--vm"])
    if vm not in {"kvm", "qemu"}:
        raise BootstrapError("This bootstrap targets a new KVM/QEMU guest, not bare metal or another platform")
    container = command(run, ["systemd-detect-virt", "--container"], allow_missing=True)
    if container != "none":
        raise BootstrapError("Refusing to bootstrap inside a container")
    values = {}
    for line in (root / "etc/os-release").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            parts = shlex.split(value)
            values[key] = parts[0] if parts else ""
    pair = values.get("ID"), values.get("VERSION_ID")
    releases = {("debian", "12"): "bookworm", ("debian", "13"): "trixie", ("ubuntu", "24.04"): "noble", ("ubuntu", "26.04"): "resolute"}
    if pair not in releases or values.get("VERSION_CODENAME") != releases[pair]:
        raise BootstrapError("Supported guests: Debian 12/13 or Ubuntu 24.04/26.04, with their official release codenames")
    arch = command(run, ["dpkg", "--print-architecture"])
    if arch not in {"amd64", "arm64"}:
        raise BootstrapError("This release supports amd64 and arm64 guests")
    machine = (root / "etc/machine-id").read_text().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
        raise BootstrapError("A valid guest machine-id is required")
    state_path = safe(root / STATE / "receipt.json")
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous and previous.get("machine_id") != machine:
        raise BootstrapError("Existing bootstrap receipt belongs to another guest")
    packages = command(run, ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\t${Version}\n", *BASE, *DOCKER, *CONFLICTS], allow_missing=True)
    installed = {}
    for row in packages.splitlines():
        fields = row.split("\t")
        if len(fields) == 3 and fields[1] == "installed":
            installed[fields[0].split(":")[0]] = fields[2]
    conflicts = sorted(set(installed).intersection(CONFLICTS))
    if conflicts:
        raise BootstrapError("Conflicting packages are installed: " + ", ".join(conflicts) + ". Resolve deliberately; this script never uninstalls them.")
    expected = {str(KEY): KEY_SHA256, str(SOURCE): hashlib.sha256(source_text(pair[0], releases[pair], arch).encode()).hexdigest()}
    for target in (KEY, SOURCE):
        path = safe(root / target)
        if path.exists() and (not path.is_file() or previous.get("owned_files", {}).get(str(target)) != expected[str(target)] or file_hash(path) != expected[str(target)]):
            raise BootstrapError(f"Existing {target} is unowned or changed; it will not be overwritten")
    sources = []
    candidates = [root / "etc/apt/sources.list", *(root / "etc/apt/sources.list.d").glob("*.list"), *(root / "etc/apt/sources.list.d").glob("*.sources")]
    for path in sorted(candidates):
        if path.exists():
            safe(path)
            text = path.read_text()
            if path != root / SOURCE and "download.docker.com" in text:
                raise BootstrapError("Another Docker repository is already configured; inspect it manually rather than creating duplicates")
            sources.append({"path": str(path.relative_to(root)), "sha256": file_hash(path)})
    result = {"schema": 1, "kind": "guest-bootstrap", "machine_id": machine, "os": pair[0], "version": pair[1], "codename": releases[pair], "architecture": arch, "virtualization": vm, "script_sha256": file_hash(script_path), "installed_packages": installed, "apt_sources": sources, "packages": BASE + DOCKER, "key_sha256": KEY_SHA256, "key_fingerprint": KEY_FINGERPRINT, "owned_files": expected, "effects": ["Install missing packages from OS and official Docker repositories; no existing package upgrades or removals", "Create only managed Docker key/source files, retaining private receipts and source backups", "Enable/start Docker, which creates its normal Docker network/firewall rules; start the guest agent", "No partition, RAID, mount/fstab, host bridge, bootloader or media changes"]}
    result["review_hash"] = digest(result)
    return result


def verify_runtime(run):
    command(run, ["systemctl", "is-active", "--quiet", "docker"])
    command(run, ["systemctl", "is-active", "--quiet", "qemu-guest-agent"])
    engine = command(run, ["docker", "info", "--format", "{{.ServerVersion}}"])
    compose = command(run, ["docker", "compose", "version", "--short"])
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", compose)
    if not match or tuple(map(int, match.groups())) < (2, 24, 0):
        raise BootstrapError("Docker Compose 2.24.0 or newer is required")
    return {"state": "ready", "docker_server": engine, "compose": compose, "guest_agent": "active"}


def install_missing(run, packages):
    flags = ["--no-remove", "--no-upgrade", "--no-install-recommends"]
    simulation = command(run, ["apt-get", "--simulate", *flags, "install", *packages])
    if re.search(r"(?m)^(?:Remv\s|Inst\s+\S+\s+\[)", simulation):
        raise BootstrapError("APT would remove or upgrade an existing package; resolve dependencies manually instead")
    command(run, ["apt-get", "--yes", *flags, "install", *packages])


def apply(document, accepted, root, run, script_path):
    supplied = dict(document)
    recorded = supplied.pop("review_hash", None)
    if accepted != recorded or recorded != digest(supplied):
        raise BootstrapError("Plan integrity or --accept-plan does not match")
    current = inspect(root, run, script_path)
    if current != document:
        raise BootstrapError("Guest OS, machine, script, packages or apt sources changed; review a new --plan before retrying")
    state = safe(root / STATE)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = safe(state / "apply.lock")
    import fcntl
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BootstrapError("Another bootstrap apply is already running") from exc
        if inspect(root, run, script_path) != document:
            raise BootstrapError("Guest state changed while waiting for the apply lock; review a new plan")
        receipt = {"schema": 1, "machine_id": document["machine_id"], "review_hash": accepted, "owned_files": {}, "state": "applying"}
        receipt_path = state / "receipt.json"
        if receipt_path.exists():
            receipt["owned_files"] = json.loads(receipt_path.read_text()).get("owned_files", {})
        backup = state / ("backup-" + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        for index, source in enumerate(document["apt_sources"]):
            path = root / source["path"]
            with (backup / f"source-{index}.txt").open("xb") as out:
                out.write(path.read_bytes())
        write_private(backup / "manifest.json", {"machine_id": document["machine_id"], "apt_sources": document["apt_sources"], "installed_packages": document["installed_packages"]})
        write_private(receipt_path, receipt)
        try:
            missing = [name for name in BASE + DOCKER if name not in document["installed_packages"]]
            if missing:
                command(run, ["apt-get", "update"])
                install_missing(run, BASE)
            if not (root / KEY).exists():
                fd, name = tempfile.mkstemp(prefix="docker-key-", dir=state)
                os.close(fd)
                try:
                    command(run, ["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "--connect-timeout", "15", "--max-time", "60", f"https://download.docker.com/linux/{document['os']}/gpg", "--output", name])
                    if file_hash(Path(name)) != KEY_SHA256:
                        raise BootstrapError("Docker key differs from the pinned official artifact; review upstream key rotation instead of accepting it automatically")
                    safe(root / KEY).parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    receipt["owned_files"][str(KEY)] = KEY_SHA256
                    write_private(receipt_path, receipt)
                    descriptor = os.open(root / KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                    with os.fdopen(descriptor, "wb") as out:
                        out.write(Path(name).read_bytes())
                    os.chmod(root / KEY, 0o644)
                finally:
                    os.unlink(name)
            if not (root / SOURCE).exists():
                path = safe(root / SOURCE)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                receipt["owned_files"][str(SOURCE)] = document["owned_files"][str(SOURCE)]
                write_private(receipt_path, receipt)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(descriptor, "w", newline="\n") as out:
                    out.write(source_text(document["os"], document["codename"], document["architecture"]))
                os.chmod(path, 0o644)
            if missing:
                command(run, ["apt-get", "update"])
                install_missing(run, DOCKER)
            command(run, ["systemctl", "enable", "--now", "docker"])
            command(run, ["systemctl", "start", "qemu-guest-agent"])
            result = verify_runtime(run)
            receipt.update(state="ready", runtime=result)
            write_private(receipt_path, receipt)
            return result
        except Exception as exc:
            receipt.update(state="failed", error=type(exc).__name__)
            write_private(receipt_path, receipt)
            raise


def main(argv=None, *, root=Path("/"), run=runner, script_path=None):
    parser = argparse.ArgumentParser(description="New KVM/QEMU guest prerequisites; default is read-only planning")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-file")
    parser.add_argument("--accept-plan")
    parser.add_argument("--ack-new-guest", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        if not args.ack_new_guest or not args.plan_file or not args.accept_plan:
            raise BootstrapError("Apply requires --ack-new-guest, --plan-file and --accept-plan")
        if getattr(os, "geteuid", lambda: -1)() != 0:
            raise BootstrapError("Apply requires root inside the new VM")
        plan_path = safe(Path(args.plan_file))
        result = apply(json.loads(plan_path.read_text()), args.accept_plan, root, run, script_path)
    else:
        result = inspect(root, run, script_path)
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    try:
        main(sys.argv[2:], script_path=Path(sys.argv[1]))
    except (BootstrapError, OSError, ValueError, KeyError) as exc:
        print("bootstrap refused: " + str(exc), file=sys.stderr)
        sys.exit(1)
PY
