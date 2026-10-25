"""Start a new guided session after an intentional application-state wipe.

Only abandoned installer metadata is archived. Application roots must already
be absent or empty; storage paths, media markers and personal files are never
opened, adopted, moved or deleted by this module.
"""
from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import uuid

from . import installation_registry as registry, retired_units
from .cli import default_config
from .safety import SafetyError, no_symlinks, run, validate_config, write_json


_UNIT = re.compile(r"samurai-([a-z][a-z0-9-]{1,39})-([2-6])\.service\Z")
_SESSION = re.compile(r"fresh-([0-9a-f-]{36})\Z")


def _empty_application_root(value):
    path = no_symlinks(Path(value))
    if path.exists():
        registry._directory(path, private=False)
        if any(path.iterdir()):
            raise SafetyError("Fresh setup requires the old application directory to be absent or empty. Existing accounts, credentials and application files were retained: " + str(path))
    return path


def _unit_candidates(unit_root):
    root = no_symlinks(Path(unit_root))
    if not root.exists():
        return []
    registry._directory(root, private=False)
    paths = sorted(root.glob("samurai-*.service"))
    if len(paths) > registry.MAX_CANDIDATES:
        raise SafetyError("Too many old Samurai services to verify for fresh setup; no service was changed.")
    result = []
    for path in paths:
        match = _UNIT.fullmatch(path.name)
        if not match:
            # Prefix-only neighbours cannot collide with generated stage names.
            # Leave custom monitoring/isolation services completely untouched.
            continue
        encoded, text, info = retired_units._unit(path)
        lines = re.findall(r"^ExecStartPre=(.*)$", text, re.M)
        if len(lines) != 1:
            raise SafetyError("An old Samurai service has no unique generated guard command; it was retained.")
        try:
            argv = shlex.split(lines[0])
            if len(argv) != 4 or argv[0] != "/usr/bin/python3" or argv[3] != match[2]:
                raise ValueError()
            guard = PurePosixPath(argv[1])
            if guard.name != "runtime_guard.py":
                raise ValueError()
        except ValueError:
            raise SafetyError("An old Samurai service has an unrecognized guard command; it was retained.") from None
        c = default_config("127.0.0.1")
        c.update(deployment_name=match[1], install_root=str(guard.parent))
        validate_config(c)
        old = retired_units._generated(c, int(match[2]), text)
        if old is None:
            raise SafetyError("An old Samurai service identity could not be separated from this fresh session.")
        _empty_application_root(c["install_root"])
        result.append({"config": c, "stage": int(match[2]), "old_id": old, "unit": path,
                       "encoded": encoded, "inode": (info.st_dev, info.st_ino)})
    return result


def _registry_candidates(registry_root):
    root = no_symlinks(Path(registry_root))
    if not root.exists():
        return []
    registry._directory(root)
    children = sorted(root.iterdir())
    if len(children) > registry.MAX_CANDIDATES:
        raise SafetyError("Too many saved installations to verify for fresh setup; existing configuration was retained.")
    result = []
    for path in children:
        if path.name.startswith("."):
            continue
        registry._uuid(path.name)
        c = _entry_config(path)
        if c is not None:
            _empty_application_root(c["install_root"])
        info = path.stat()
        result.append({"config": c, "path": path, "inode": (info.st_dev, info.st_ino)})
    return result


def _entry_config(path):
    """Read only host-bound cleanup identity; never rebuild wizard choices."""
    registry._directory(path)
    if not any(path.iterdir()):
        return None
    if (path / "site.json").exists():
        return registry.load(path.name, registry_root=path.parent)
    metadata = registry._json(path / "identity.json")
    identity = metadata.get("identity")
    if not isinstance(identity, dict) or identity.get("deployment_id") != path.name:
        raise SafetyError("An incomplete saved installation has no valid host-bound identity; its files were retained.")
    try:
        c = default_config("127.0.0.1")
        for key in ("deployment_id", "deployment_name", "install_root", "puid", "pgid", "role"):
            c[key] = identity[key]
        c["storage"].update(data_root=identity["data_root"], mode=identity["storage_mode"],
                            expected_mount=identity["expected_mount"])
        validate_config(c)
        registry._metadata(path / "identity.json", c)
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("An incomplete saved installation has no valid host-bound identity; its files were retained.") from None
    return c


def _docker_ready(runner, required):
    if not required and shutil.which("docker") is None:
        return False
    try:
        endpoint = retired_units._call(runner, ["docker", "--context", "default", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"])
        kind = retired_units._call(runner, ["docker", "--context", "default", "info", "--format", "{{.OSType}}"])
        if not endpoint.startswith("unix://") or kind != "linux":
            raise ValueError()
    except (SafetyError, ValueError):
        if not required:
            # A blank wizard may repair prerequisites in phase 1. No abandoned
            # service/registry metadata is retired while Docker is unknown.
            return False
        raise SafetyError("Fresh setup could not verify the local Docker engine. Run ./setup 1 to prepare or repair Debian first, then retry fresh setup. No application or media files were removed.") from None
    return True


def _no_containers(units, entries, runner, available):
    if not available:
        if units or entries:
            raise SafetyError("Old installation metadata needs a reachable local Docker engine before fresh setup.")
        return
    found = retired_units._call(runner, ["docker", "--context", "default", "ps", "--all", "--filter", "label=systems.samarie.deployment", "--format", "{{.ID}}"])
    if found:
        raise SafetyError("Samurai containers still exist, including stopped containers. Fresh setup will not remove or take over them.")
    for row in units:
        retired_units._empty(runner, row["config"]["deployment_name"] + "-" + str(row["stage"]), row["old_id"])
    for row in entries:
        c = row["config"]
        if c is None:
            continue
        for stage in range(2, 7):
            retired_units._empty(runner, c["deployment_name"] + "-" + str(stage), c["deployment_id"])


def _verify_snapshot(units, entries, runner, available):
    for row in units:
        _empty_application_root(row["config"]["install_root"])
        encoded, _, info = retired_units._unit(row["unit"])
        if encoded != row["encoded"] or (info.st_dev, info.st_ino) != row["inode"]:
            raise SafetyError("An old service changed during fresh-setup review; its current unit was retained.")
        retired_units._manager_matches(runner, row["unit"])
    for row in entries:
        c = row["config"]
        if _entry_config(row["path"]) != c:
            raise SafetyError("Saved installation state changed during fresh-setup review; it was retained.")
        info = row["path"].stat()
        if (info.st_dev, info.st_ino) != row["inode"]:
            raise SafetyError("A saved installation directory changed during fresh-setup review; it was retained.")
        if c is not None:
            _empty_application_root(c["install_root"])
    _no_containers(units, entries, runner, available)


def pending_fresh_configuration(*, setup_root=registry.SETUP_ROOT):
    """Resume only a validated same-host fresh session, without reading old ZIPs."""
    registry._guard()
    parent = no_symlinks(Path(setup_root).absolute() / registry.host_identity())
    pointer = no_symlinks(parent / "active-fresh.json")
    if not pointer.exists():
        return None
    registry._directory(parent)
    value = registry._json(pointer)
    session = value.get("session")
    match = _SESSION.fullmatch(session) if isinstance(session, str) else None
    if (value.get("schema_version") != 1 or value.get("host_id") != registry.host_identity()
            or match is None or registry._uuid(match[1]) != match[1]):
        raise SafetyError("The fresh-setup session pointer is invalid or belongs to another host; it was retained.")
    directory = registry._directory(parent / session)
    site = no_symlinks(directory / "site.json")
    if site.exists():
        registry._config(site)
    return site


def begin_fresh(*, registry_root=registry.REGISTRY_ROOT, setup_root=registry.SETUP_ROOT,
                unit_root=registry.UNIT_ROOT, archive_root=retired_units.ARCHIVE_ROOT,
                runner=run, output_fn=print):
    """Return a new empty private draft after archiving only abandoned metadata."""
    registry._guard()
    units = _unit_candidates(unit_root)
    entries = _registry_candidates(registry_root)
    available = _docker_ready(runner, bool(units or entries))
    _verify_snapshot(units, entries, runner, available)
    # Lock existing canonical reservations before the first service mutation.
    # A brand-new host has no registry and needs no registry directory yet.
    root = no_symlinks(Path(registry_root))
    with registry._lock(root, ".registry.lock") if root.exists() else nullcontext():
        _verify_snapshot(units, entries, runner, available)
        for index, row in enumerate(units):
            _verify_snapshot(units[index:], entries, runner, available)
            action = retired_units.retire_conflicting(row["config"], row["stage"], runner=runner,
                        unit_root=unit_root, archive_root=archive_root)
            if not action or action.get("status") != "obsolete_unit_archived":
                raise SafetyError("An old generated service could not be archived. Original metadata and media were retained.")
            output_fn("Archived obsolete service: " + str(action["archive_path"]))
        for index, row in enumerate(entries):
            _verify_snapshot([], entries[index:], runner, available)
            # Same-filesystem rename preserves all original metadata, histories
            # and old progress, without copying or touching application/media.
            archive = registry._mkdir(root / ".retired")
            destination = archive / (row["path"].name + "-" + uuid.uuid4().hex)
            os.rename(row["path"], destination)
            output_fn("Archived abandoned installation configuration: " + str(destination))
    parent = registry.draft_directory(setup_root=setup_root)
    session = "fresh-" + str(uuid.uuid4())
    directory = registry._mkdir(parent / session)
    pointer = no_symlinks(parent / "active-fresh.json")
    if pointer.exists():
        registry._read(pointer)
    write_json(pointer, {"schema_version": 1, "host_id": registry.host_identity(), "session": session}, replace=pointer.exists())
    output_fn("Fresh setup: no previous configuration, credentials or step status was reused.")
    output_fn("Start with step 1. Existing media and mounted filesystems were not changed.")
    return directory / "site.json"
