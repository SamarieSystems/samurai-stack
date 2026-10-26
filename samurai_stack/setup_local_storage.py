"""Select mounted local media storage without adopting, moving or erasing data."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from . import naming, engine, installation_registry as registry, storage
from .safety import SafetyError, no_symlinks, private_read, run, validate_config, write_json


_SYSTEM = {"/boot", "/proc", "/run", "/dev", "/sys", "/etc", "/usr", "/bin", "/sbin",
           "/lib", "/lib64", "/var", "/opt", "/home", "/root", "/tmp", "/snap"}


def _capacity(target):
    try:
        item = os.statvfs(target)
        return {"size_bytes": item.f_frsize * item.f_blocks, "available_bytes": item.f_frsize * item.f_bavail}
    except (OSError, AttributeError):
        return {}


def mounted_local_choices(runner=run):
    """List writable non-system local filesystem mounts, never physical disks."""
    from .setup_storage import LOCAL_FILESYSTEMS, _safe_mount_rows
    choices, seen = [], set()
    for row in _safe_mount_rows(runner):
        if any(not isinstance(row.get(key), str) for key in ("target", "source", "fstype", "options")):
            raise SafetyError("The mounted filesystem inventory has unsupported fields.")
        target = PurePosixPath(row["target"])
        if (row["fstype"] not in LOCAL_FILESYSTEMS or str(target) == "/" or not str(target).startswith("/")
                or any(str(target) == prefix or PurePosixPath(prefix) in target.parents for prefix in _SYSTEM)
                or re.search(r"[\x00-\x20\x7f\\]", row["target"] + row["source"])
                or ".." in target.parts or str(target) != row["target"]):
            continue
        options = set(row["options"].split(","))
        if "rw" not in options or options.intersection({"ro", "bind", "rbind"}):
            continue
        if row["target"] in seen:
            raise SafetyError("More than one local filesystem uses the same mountpoint; inspect findmnt before choosing storage.")
        no_symlinks(Path(row["target"]))
        seen.add(row["target"])
        choices.append({**row, **_capacity(row["target"])})
    return sorted(choices, key=lambda row: row["target"])


def _empty_child(input_fn, output_fn, runner, selected):
    from .setup_storage import _new_directory
    default = str(PurePosixPath(selected["target"]) / "samurai")
    while True:
        path = _new_directory(input_fn, output_fn, "New empty media directory on this filesystem", default)
        target = Path(selected["target"])
        if path == target or target not in path.parents:
            output_fn("Choose a new empty child of " + str(target) + ". The mount itself and existing media are kept.")
            continue
        actual = storage.mount_identity(path, runner)
        if (any(actual.get(key) != selected[key] for key in ("target", "source", "fstype"))
                or "rw" not in actual.get("options", "").split(",") or "ro" in actual.get("options", "").split(",")):
            raise SafetyError("The selected child is on a different or read-only filesystem. No configuration was changed.")
        return path, actual


def choose_local_directory(*, input_fn=input, output_fn=print, runner=run, require_mounted=False):
    """Return a reviewed empty child and exact backing identity; create nothing."""
    from .setup_storage import LOCAL_FILESYSTEMS, _choice, _new_directory
    choices = mounted_local_choices(runner)
    if choices:
        output_fn("Mounted local media filesystems (these are existing mounts, not disks to format):")
        for number, row in enumerate(choices, 1):
            capacity = ""
            if row.get("size_bytes"):
                capacity = ", " + format(row["size_bytes"] / (1024 ** 4), ".2f") + " TiB total"
            output_fn(str(number) + ". " + row["target"] + " | " + row["source"] + " | " + row["fstype"] + capacity)
        if not require_mounted:
            output_fn("0. Another local directory, including the VM OS filesystem")
        options = {str(i): row for i, row in enumerate(choices, 1)}
        if not require_mounted:
            options["0"] = None
        selected = _choice(input_fn, output_fn, "Media filesystem number", options, "1")
        if selected is not None:
            return _empty_child(input_fn, output_fn, runner, selected)
    elif require_mounted:
        raise SafetyError("No writable non-system local data filesystem is mounted. Mount the intended data disk first; no filesystem was changed.")
    output_fn("Using a local directory can consume the VM OS disk. Select a mounted data filesystem when one is available.")
    while True:
        path = _new_directory(input_fn, output_fn, "New dedicated downloads/library directory", "/srv/samurai-data")
        actual = storage.mount_identity(path, runner)
        if (actual["fstype"] not in LOCAL_FILESYSTEMS or "rw" not in actual["options"].split(",")
                or "ro" in actual["options"].split(",")):
            output_fn("Choose writable ext4, XFS, Btrfs or ZFS storage. No filesystem was changed.")
            continue
        return path, actual


def _empty_prepared_tree(c):
    root = no_symlinks(Path(c["storage"]["data_root"]))
    if not root.exists():
        return
    if not root.is_dir():
        raise SafetyError("The current data path is not an empty directory; storage correction was refused.")
    allowed = set()
    for value in storage.DIRECTORIES:
        path = PurePosixPath(value)
        allowed.add(str(path))
        allowed.update(str(parent) for parent in path.parents if str(parent) != ".")
    for base, directories, files in os.walk(root, followlinks=False):
        relative_base = Path(base).relative_to(root)
        for name in directories:
            child = no_symlinks(Path(base) / name)
            if str(PurePosixPath(*(relative_base / name).parts)) not in allowed or not child.is_dir():
                raise SafetyError("The current data tree has unmanaged directories; no storage path was changed.")
        for name in files:
            if relative_base != Path(".") or name != ".samurai-storage.json":
                raise SafetyError("The current data tree contains files. Moving or adopting media is outside storage correction.")
            marker = no_symlinks(Path(base) / name)
            info = marker.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise SafetyError("The existing storage marker is invalid; it was not changed.")
            try:
                # storage.prepare intentionally publishes this non-secret
                # media-owned marker as mode 644, not a private credential.
                with marker.open("rb") as stream:
                    encoded = stream.read(65537)
                if len(encoded) > 65536:
                    raise ValueError()
                value = json.loads(encoded)
            except (OSError, ValueError, SafetyError):
                raise SafetyError("The existing storage marker is invalid; it was not changed.") from None
            if (not isinstance(value, dict) or value.get("deployment_id") != c["deployment_id"]
                    or value.get("expected_mount") != c["storage"]["expected_mount"]):
                raise SafetyError("The current storage marker belongs to another installation or mount; it was not adopted.")


def _unregistered(c, registry_root):
    # Deliberately inspect the canonical registry only, not stale service units.
    root = no_symlinks(Path(registry_root))
    if (root / c["deployment_id"]).exists():
        raise SafetyError("This installation is already registered. Its storage identity cannot be changed by the fresh-setup correction.")
    records = registry.discover(registry_root=root, unit_root=root / ".no-service-discovery")
    if any(row["config"]["install_root"] == c["install_root"] for row in records):
        raise SafetyError("A registered installation already reserves this application root. Recover that installation before choosing another storage path.")


def _inactive_service_units(c, runner, unit_root):
    """A restart-looping owner can recreate containers after a zero-object read."""
    root = no_symlinks(Path(unit_root))
    if not root.exists():
        return
    expected = {"samurai-" + c["deployment_name"] + "-" + str(stage) + ".service"
                for stage in (2, 3, 4, 5, 6)}
    try:
        units = sorted(root.glob("samurai-*.service"))
        if len(units) > registry.MAX_CANDIDATES:
            raise ValueError()
        selected = []
        for path in units:
            same_project = path.name in expected
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                if same_project:
                    raise ValueError()
                continue
            with path.open("rb") as stream:
                encoded = stream.read(65537)
            if len(encoded) > 65536:
                raise ValueError()
            text = encoded.decode("utf-8").replace("\r\n", "\n")
            markers = re.findall(r"^# Managed by Samurai Stack: ([^\r\n]+)$", text, re.M)
            if same_project or c["deployment_id"] in markers:
                if not re.fullmatch(r"samurai-[a-z][a-z0-9-]{1,39}-[2-6]\.service", path.name):
                    raise ValueError()
                selected.append(path.name)
    except (OSError, ValueError, UnicodeError):
        raise SafetyError("Matching Samurai service units could not be inspected safely. No storage configuration was changed.") from None
    for unit in selected:
        try:
            response = runner(["systemctl", "show", unit,
                "--property=LoadState,ActiveState,SubState,Job", "--no-pager"], timeout=20)
            if getattr(response, "returncode", 0) != 0 or not isinstance(response.stdout, str) or len(response.stdout) > 8192:
                raise ValueError()
            values = {}
            for line in response.stdout.splitlines():
                name, separator, value = line.partition("=")
                if not separator or name in values:
                    raise ValueError()
                values[name] = value
            if (values.get("LoadState") not in {"loaded", "not-found"}
                    or values.get("ActiveState") not in {"inactive", "failed"}
                    or values.get("SubState") not in {"dead", "failed"} or values.get("Job") != "0"):
                raise ValueError()
        except Exception:
            raise SafetyError("A matching Samurai service is active, restarting, queued or could not be verified inactive. Storage correction does not stop services; resolve that service state before retrying.") from None


def _no_containers(c, runner, unit_root=registry.UNIT_ROOT):
    _inactive_service_units(c, runner, unit_root)
    context = engine.docker(runner, "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}").stdout.strip()
    if not context.startswith("unix://"):
        raise SafetyError("Storage correction requires the local default Docker context.")
    filters = ["label=systems.samarie.deployment=" + c["deployment_id"]]
    filters.extend("label=com.docker.compose.project=" + naming.project_name(c, stage) for stage in (2, 3, 4, 5, 6))
    for value in filters:
        if engine.docker(runner, "ps", "--all", "--filter", value, "--format", "{{.ID}}").stdout.strip():
            raise SafetyError("Existing Docker containers still use this project or identity. Storage correction does not repoint existing applications.")
    _inactive_service_units(c, runner, unit_root)


def _guards(c, registry_root, runner, unit_root=registry.UNIT_ROOT):
    _unregistered(c, registry_root)
    _no_containers(c, runner, unit_root)
    storage.check_mount(c, runner)
    _empty_prepared_tree(c)
    app_root = no_symlinks(Path(c["install_root"]))
    marker = no_symlinks(app_root / ".samurai-install.json")
    if marker.exists():
        try:
            if marker.stat().st_size > 65536:
                raise ValueError()
            value = json.loads(private_read(marker))
        except (OSError, ValueError, SafetyError):
            raise SafetyError("The application ownership marker is invalid; no storage path was changed.") from None
        if not isinstance(value, dict) or value.get("deployment_id") != c["deployment_id"]:
            raise SafetyError("The application root belongs to another installation; no ownership was adopted.")
    elif app_root.exists() and (not app_root.is_dir() or any(app_root.iterdir())):
        raise SafetyError("The application root has unmanaged contents; no storage path was changed.")
    appdata = no_symlinks(Path(c["install_root"]) / "appdata")
    if appdata.exists():
        if not appdata.is_dir():
            raise SafetyError("The application data path is not a directory; storage correction was refused.")
        for base, directories, files in os.walk(appdata, followlinks=False):
            if files:
                raise SafetyError("Application data already exists; fresh-setup storage correction was refused.")
            for name in directories:
                no_symlinks(Path(base) / name)


def choose_replacement(config, config_path, *, input_fn=input, output_fn=print, runner=run,
                       registry_root=registry.REGISTRY_ROOT, setup_root=registry.SETUP_ROOT, unit_root=registry.UNIT_ROOT):
    """Correct only a fresh unregistered OS-disk selection, preserving its UUID."""
    from .setup_storage import _yes
    registry._guard()
    validate_config(config)
    if config["storage"]["mode"] != "local" or config["storage"]["expected_mount"]["target"] != "/":
        raise SafetyError("This correction is limited to fresh local storage currently placed on the VM OS filesystem.")
    path = no_symlinks(Path(config_path).absolute())
    if path.stat().st_size > registry.MAX_BYTES:
        raise SafetyError("The saved site is too large; it was not read or replaced.")
    text = private_read(path)
    registry._owner(path.stat())
    try:
        saved = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        raise SafetyError("The saved site is invalid; it was not replaced.") from None
    if saved != config or path.stat().st_size > registry.MAX_BYTES:
        raise SafetyError("The saved site differs from the reviewed configuration; it was not replaced.")
    original = path.read_bytes()
    _guards(config, registry_root, runner, unit_root)
    output_fn("The current media path uses the VM OS filesystem: " + config["storage"]["data_root"])
    output_fn("Choose a mounted data filesystem and a NEW EMPTY child. Existing media and old markers are never adopted, moved or erased.")
    data, identity = choose_local_directory(input_fn=input_fn, output_fn=output_fn, runner=runner, require_mounted=True)
    identity = storage.persistent_identity(identity)
    updated = deepcopy(config)
    updated["storage"].update(mode="local", data_root=str(data), allow_existing_contents=False,
                               expected_mount={key: identity[key] for key in ("target", "source", "fstype")})
    validate_config(updated)
    output_fn("Review media path: " + str(data) + " on " + identity["source"] + " at " + identity["target"])
    output_fn("Installation name, UUID, application root, credentials and feature choices remain unchanged. Step 2 will verify the actual media UID/GID before applications start.")
    if not _yes(input_fn, output_fn, "Save this storage correction with a private backup?", False):
        raise SafetyError("Storage correction paused. The saved configuration and existing data were kept.")
    _guards(config, registry_root, runner, unit_root)
    no_symlinks(data)
    if data.exists() and (not data.is_dir() or any(data.iterdir())):
        raise SafetyError("The new directory became occupied during review. No configuration was changed.")
    actual = storage.persistent_identity(storage.mount_identity(data, runner))
    if (any(actual.get(key) != identity[key] for key in ("target", "source", "fstype"))
            or "rw" not in actual.get("options", "").split(",") or "ro" in actual.get("options", "").split(",")):
        raise SafetyError("The selected backing mount changed during review. No configuration was changed.")
    private_read(path)
    if path.read_bytes() != original:
        raise SafetyError("The saved site changed during review. No configuration was replaced.")
    directory = registry.draft_directory(setup_root=setup_root)
    backup = no_symlinks(directory / ("site-before-storage-" + uuid.uuid4().hex + ".json"))
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != original:
        raise SafetyError("The saved site changed during backup. The original backup was kept and no configuration was replaced.")
    write_json(path, updated, replace=True)
    output_fn("Saved storage correction. Private original: " + str(backup) + ". No media or application files were changed.")
    return updated
