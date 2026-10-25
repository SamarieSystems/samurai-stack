"""Additive storage preparation with exact mount identity and ownership markers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from .safety import SafetyError, contained, no_symlinks, run, write_json

DIRECTORIES = [
    "library/movies", "library/tv", "library/music",
    "downloads/usenet/complete", "downloads/usenet/incomplete",
    "downloads/torrents/movies", "downloads/torrents/tv", "downloads/torrents/manual", "downloads/torrents/incomplete",
    "downloads/music/complete", "downloads/music/incomplete", "photos/upload", "photos/external",
]


def nearest_existing(path):
    path = no_symlinks(path)
    while not path.exists():
        path = path.parent
    return path


def mount_identity(path, runner=run):
    query = nearest_existing(path)
    result = runner(["findmnt", "--json", "--target", str(query), "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    try:
        rows = json.loads(result.stdout)["filesystems"]
        if len(rows) != 1:
            raise ValueError()
        row = rows[0]
        return {k: row[k] for k in ["target", "source", "fstype", "options"]}
    except (KeyError, ValueError, TypeError) as exc:
        raise SafetyError("Cannot establish a unique backing mount") from exc


def check_mount(c, runner=run):
    root = Path(c["storage"]["data_root"])
    no_symlinks(root)
    found = mount_identity(root, runner)
    expected = c["storage"]["expected_mount"]
    if any(found[k] != expected[k] for k in ["target", "source", "fstype"]):
        raise SafetyError("Storage mount identity mismatch; refusing local fallback or a different NAS export")
    if "rw" not in found["options"].split(","):
        raise SafetyError("Selected data mount is read-only")
    if c["storage"]["mode"] == "nfs" and root == Path("/"):
        raise SafetyError("An NFS share must have a dedicated mount")
    return found


def _mkdir_owned(path, root, uid, gid):
    path = contained(path, root)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        no_symlinks(item)
        item.mkdir(mode=0o775)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(item, uid, gid)


def marker(c):
    return Path(c["storage"]["data_root"]) / ".samurai-storage.json"


def verify(c, runner=run):
    identity = check_mount(c, runner)
    path = marker(c)
    try:
        document = json.loads(no_symlinks(path).read_text())
    except (OSError, ValueError) as exc:
        raise SafetyError("Storage marker missing; run ./setup 2 (Connect storage) before application deployment") from exc
    if document.get("deployment_id") != c["deployment_id"] or document.get("expected_mount") != c["storage"]["expected_mount"]:
        raise SafetyError("Storage marker belongs to a different deployment or mount")
    for relative in DIRECTORIES:
        path = contained(Path(c["storage"]["data_root"]) / relative, c["storage"]["data_root"])
        if not path.is_dir():
            raise SafetyError(f"Storage directory missing: {relative}")
    as_media_identity(c, lambda: check_permissions(c))
    return {"storage": "verified", "mount": identity}


def as_media_identity(c, operation):
    """Use the actual media identity, including root-squashed NFS permissions."""
    if os.name != "posix" or (os.geteuid() == c["puid"] and os.getegid() == c["pgid"]):
        return operation()
    if os.geteuid() != 0:
        raise SafetyError("Storage operations require sudo or the configured media UID/GID")
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setgid(c["pgid"])
            os.setuid(c["puid"])
            value = {"ok": True, "result": operation()}
        except BaseException:
            value = {"ok": False}
        with os.fdopen(write_fd, "w") as output:
            json.dump(value, output)
        os._exit(0 if value["ok"] else 1)
    os.close(write_fd)
    with os.fdopen(read_fd) as source:
        value = json.load(source)
    _, code = os.waitpid(child, 0)
    if code or not value.get("ok"):
        raise SafetyError("Storage access failed as the media UID/GID. Check NAS mapping/ACLs; existing permissions were not changed")
    return value["result"]


def check_permissions(c):
    root = Path(c["storage"]["data_root"])
    for relative in DIRECTORIES:
        path = contained(root / relative, root)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise SafetyError("Media identity cannot read/write/traverse managed directories")
    return {"media_identity_access": "verified"}


def permission_probe(c):
    root = Path(c["storage"]["data_root"])
    check_permissions(c)
    name = ".samurai-probe-" + uuid.uuid4().hex
    source, destination = root / "downloads/torrents" / name, root / "library/movies" / name
    created_source = created_destination = False
    try:
        with open(source, "xb") as stream:
            created_source = True
            stream.write(b"samurai hardlink probe\n")
        os.link(source, destination)
        created_destination = True
        if source.stat().st_ino != destination.stat().st_ino or source.stat().st_dev != destination.stat().st_dev:
            raise SafetyError("Download/library hardlink probe failed")
    finally:
        if created_destination:
            destination.unlink()
        if created_source:
            source.unlink()
    return {"media_identity_access": "verified", "download_library_hardlinks": "verified"}


def _prepare(c):
    os.umask(0o002)
    root = Path(c["storage"]["data_root"])
    target = marker(c)
    if target.exists():
        no_symlinks(target)
        existing = json.loads(target.read_text())
        if existing.get("deployment_id") != c["deployment_id"] or existing.get("expected_mount") != c["storage"]["expected_mount"]:
            raise SafetyError("Refusing to adopt another deployment's storage")
    elif root.exists() and any(root.iterdir()) and not c["storage"].get("allow_existing_contents", False):
        raise SafetyError("Data root is nonempty and unowned; choose a new subdirectory or explicitly review allow_existing_contents")
    _mkdir_owned(root, root, c["puid"], c["pgid"])
    # Marker first: an interrupted directory-creation run must be resumable.
    if not target.exists():
        write_json(target, {"deployment_id": c["deployment_id"], "expected_mount": c["storage"]["expected_mount"]}, mode=0o644)
    for relative in DIRECTORIES:
        _mkdir_owned(root / relative, root, c["puid"], c["pgid"])
    permission_probe(c)
    return {"prepared": True}


def prepare(c, runner=run):
    identity = check_mount(c, runner)  # Must precede even mkdir.
    if c["storage"]["mode"] == "local" and not Path(c["storage"]["data_root"]).exists():
        # A normal /srv parent is root-owned. Create/chown only new directories
        # before dropping to the application identity for all actual probes.
        _mkdir_owned(Path(c["storage"]["data_root"]), Path(c["storage"]["data_root"]), c["puid"], c["pgid"])
    as_media_identity(c, lambda: _prepare(c))
    return {"storage": "prepared", "mount": identity, "existing_files_moved": 0, "existing_permissions_changed": 0}
