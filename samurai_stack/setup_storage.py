"""Guided first-run storage choices without adopting or migrating existing data."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import storage
from .safety import (SafetyError, absolute_path, atomic_write, load_config,
                     no_symlinks, run, validate_config, write_json)


LOCAL_FILESYSTEMS = {"ext4", "xfs", "btrfs", "zfs"}
NFS_FILESYSTEMS = {"nfs", "nfs4"}


def _ask(input_fn, label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input_fn(label + suffix + ": ").strip()
    except EOFError as exc:
        raise SafetyError("Setup needs an interactive terminal. No new site configuration was saved.") from exc
    return value or default


def _yes(input_fn, output_fn, label, default=False):
    while True:
        answer = _ask(input_fn, label + " (yes/no)", "yes" if default else "no").lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output_fn("Enter yes or no.")


def _choice(input_fn, output_fn, label, choices, default):
    while True:
        answer = _ask(input_fn, label, default).lower()
        if answer in choices:
            return choices[answer]
        output_fn("Choose one of: " + ", ".join(choices))


def _safe_mount_rows(runner):
    try:
        result = runner(["findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"])
        document = json.loads(result.stdout)
        rows = []

        def walk(items):
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError()
                rows.append({key: item[key] for key in ("target", "source", "fstype", "options")})
                walk(item.get("children", []))

        walk(document["filesystems"])
        return rows
    except (KeyError, TypeError, ValueError) as exc:
        raise SafetyError("Cannot read the current mount table. Check findmnt before choosing storage.") from exc


def discover_lan_addresses(runner=run):
    """Prefer the default-route interface, without probing or changing the LAN."""
    try:
        interfaces = json.loads(runner(["ip", "-j", "-4", "address", "show"]).stdout)
        routes = json.loads(runner(["ip", "-j", "-4", "route", "show", "default"]).stdout)
        preferred = {route.get("dev") for route in routes}
        found = []
        for iface in interfaces:
            if iface.get("ifname", "").startswith(("docker", "veth", "br-", "virbr")):
                continue
            for item in iface.get("addr_info", []):
                address = ipaddress.ip_address(item.get("local", ""))
                if (address.version == 4 and address.is_private and not address.is_loopback
                        and not address.is_link_local and not address.is_unspecified
                        and not address.is_multicast):
                    found.append((iface.get("ifname") not in preferred, str(address), iface.get("ifname", "unknown")))
        return [(address, interface) for _, address, interface in sorted(set(found))]
    except (ValueError, TypeError, KeyError) as exc:
        raise SafetyError("Cannot identify this VM's LAN address. Check its network configuration first.") from exc


def detected_timezone():
    try:
        path = Path("/etc/timezone")
        if path.is_file():
            candidate = path.read_text().strip()
        else:
            candidate = str(Path("/etc/localtime").resolve()).split("/zoneinfo/", 1)[-1]
        ZoneInfo(candidate)
        return candidate
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return "Etc/UTC"


def _new_directory(input_fn, output_fn, label, default, *, installation=False):
    while True:
        value = _ask(input_fn, label, default)
        try:
            path = no_symlinks(absolute_path(value, label))
            if path.exists() and not path.is_dir():
                raise SafetyError("That path is a file, not a new directory.")
            if path.exists() and any(path.iterdir()):
                if installation and (path / ".samurai-install.json").exists():
                    raise SafetyError("A previous Samurai installation already uses " + str(path)
                                      + ". Recover its original site.json and rerun setup; no files were changed.")
                output_fn("That directory already contains files. Choose a new empty child directory; setup will not adopt, move or erase them.")
                continue
            return path
        except SafetyError as exc:
            if installation and "previous Samurai installation" in str(exc):
                raise
            output_fn(str(exc))
        except OSError as exc:
            raise SafetyError("Cannot inspect " + str(value) + ". Check its access permissions; setup did not change them.") from exc


def _media_id(input_fn, output_fn, label):
    while True:
        value = _ask(input_fn, label, "1000")
        try:
            number = int(value)
            if 1 <= number <= 2147483647:
                return number
        except ValueError:
            pass
        output_fn("Use a non-root numeric ID, usually 1000. Root/0 is not supported.")


def _compose_names(config, input_fn, output_fn):
    from . import naming
    defaults = naming.default_projects(config['deployment_name'])
    labels = {'2': 'Media applications', '3': 'VPN and torrents', '4': 'Management',
              '5': 'Photos (if enabled later)', '6': 'Music (if enabled later)'}
    output_fn('Choose the Compose stack names shown in Dockge. Enter keeps each readable default.')
    selected = {}
    for stage, default in defaults.items():
        while True:
            value = _ask(input_fn, labels[stage] + ' stack name', default)
            try:
                naming.validate_projects(dict(config, compose_projects={**selected, stage: value}))
            except SafetyError as exc:
                output_fn(str(exc))
                continue
            selected[stage] = value
            break
    config['compose_projects'] = selected


def _nas_server(value):
    if not value or len(value) > 253:
        raise SafetyError("Enter the NAS LAN IPv4 address or its DNS hostname, without http:// or a path.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in value.rstrip(".").split(".")):
            raise SafetyError("Use a DNS hostname or LAN IPv4 address, without spaces, credentials or URL punctuation.") from None
    else:
        if address.version != 4 or not address.is_private or address.is_unspecified or address.is_multicast or address.is_link_local:
            raise SafetyError("Use the NAS's private LAN IPv4 address.")
    return value


def _export_path(value):
    if (not value or not re.fullmatch(r"/[A-Za-z0-9_.+@/-]+", value)
            or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value):
        raise SafetyError("Use the exact absolute NFS export path, without spaces, backslashes, .. or shell punctuation.")
    return value


def _fstab_fields(text):
    def decode(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)

    rows = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) < 4:
            raise SafetyError("The existing fstab has an incomplete entry. Fix it manually; setup will not rewrite it.")
        rows.append([decode(value) for value in fields])
    return rows


def _read_fstab(path):
    path = no_symlinks(path)
    if not path.exists():
        return "", None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SafetyError("fstab must be a regular file without hard links. It was not changed.")
    return path.read_text(), info


def _check_fstab(text, target, source):
    matches = [fields for fields in _fstab_fields(text) if fields[1] == str(target)]
    if len(matches) > 1:
        raise SafetyError("More than one fstab entry uses this mountpoint. Resolve that conflict manually.")
    if not matches:
        return False
    row = matches[0]
    options = set(row[3].split(","))
    if (row[0] != source or row[2] not in NFS_FILESYSTEMS
            or options.intersection({"ro", "soft", "softerr", "softreval", "noauto", "bind", "rbind"})):
        raise SafetyError("An existing fstab entry conflicts with this NAS connection. Choose another mountpoint or review it manually.")
    return True


def _available_mountpoint(target, runner):
    target = no_symlinks(target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise SafetyError("The mountpoint is occupied. Choose a new empty local directory; setup will not hide existing files.")
    for row in _safe_mount_rows(runner):
        actual = PurePosixPath(row["target"])
        if actual == PurePosixPath(target) or PurePosixPath(target) in actual.parents:
            raise SafetyError("This target already contains a mounted filesystem. Select it from the mounted NAS list or use another empty mountpoint.")
    parent = storage.mount_identity(target, runner)
    if parent["fstype"] not in LOCAL_FILESYSTEMS:
        raise SafetyError("Create the new NAS mountpoint on a local Linux filesystem, not inside another NAS or unsupported filesystem.")


def _persist_mount(fstab_path, original, original_info, row, runner):
    """Append one reviewed entry; never replace a conflicting or changed file."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise SafetyError("Saving the NAS connection for reboot requires root inside this VM. Rerun this setup with sudo or from a root shell.")
    for field in ("source", "target", "fstype"):
        if not row.get(field) or re.search(r"[\s\\#\x00-\x1f\x7f]", row[field]):
            raise SafetyError("This existing mount uses characters that require manual fstab escaping. It was retained unchanged; configure persistence manually before continuing.")
    lock_path = no_symlinks(Path(str(fstab_path) + ".samurai.lock"))
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SafetyError("Another NAS setup may be active. Inspect the private fstab.samurai.lock before retrying.") from exc
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"pid": os.getpid(), "purpose": "append reviewed NAS mount"}, stream)
        current, info = _read_fstab(fstab_path)
        if current != original or (original_info is not None and
                (info is None or (info.st_dev, info.st_ino) != (original_info.st_dev, original_info.st_ino))):
            raise SafetyError("fstab changed while the NAS was connecting. The mount is retained, but no entry was written; review it before retrying.")
        if _check_fstab(current, row["target"], row["source"]):
            return None
        backup = Path(str(fstab_path) + ".samurai-backup-" + uuid.uuid4().hex)
        atomic_write(backup, original, mode=0o600)
        content = current + ("\n" if current and not current.endswith("\n") else "")
        content += "# Samurai Stack: reviewed dedicated NAS mount\n"
        content += f"{row['source']} {row['target']} {row['fstype']} rw,hard,_netdev,nofail,x-systemd.mount-timeout=30s 0 0\n"
        atomic_write(fstab_path, content, replace=fstab_path.exists(),
                     mode=stat.S_IMODE(info.st_mode) if info else 0o644)
        if info and hasattr(os, "chown") and getattr(os, "geteuid", lambda: -1)() == 0:
            os.chown(fstab_path, info.st_uid, info.st_gid)
        try:
            runner(["systemctl", "daemon-reload"])
        except SafetyError as exc:
            raise SafetyError("The NAS is mounted and its fstab entry was saved. systemd reload failed; run systemctl daemon-reload before continuing. Backup: " + str(backup)) from exc
        return backup
    finally:
        lock_path.unlink()


def connect_nfs(*, input_fn=input, output_fn=print, runner=run, fstab_path=Path("/etc/fstab")):
    """Connect one operator-selected export; no NAS ACL or remote file changes."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise SafetyError("Connecting a NAS requires root inside the new guest. Run this setup with sudo, or from a root shell.")
    while True:
        try:
            server = _nas_server(_ask(input_fn, "NAS address (for example 192.168.1.10)"))
            export = _export_path(_ask(input_fn, "Exact NFS export (for example /volume1/media)"))
            target = no_symlinks(absolute_path(_ask(input_fn, "New empty local mountpoint", "/mnt/media"), "NAS mountpoint"))
            source = server + ":" + export
            _available_mountpoint(target, runner)
            original, original_info = _read_fstab(fstab_path)
            already_persistent = _check_fstab(original, target, source)
            break
        except SafetyError as exc:
            output_fn(str(exc))
            if not _yes(input_fn, output_fn, "Enter different NAS connection details?", True):
                raise SafetyError("NAS setup stopped. Existing files, mounts and fstab were retained.") from None
    output_fn("Connect " + source + " at " + str(target) + " using a read/write hard NFS mount.")
    if already_persistent:
        output_fn("An existing compatible fstab entry will be retained unchanged.")
    else:
        output_fn("Add one persistent fstab entry after mount verification, preserving a private backup:")
        output_fn(source + " " + str(target) + " nfs (detected version) rw,hard,_netdev,nofail,x-systemd.mount-timeout=30s 0 0")
    output_fn("This creates only the empty local mountpoint. It does not change NAS accounts, permissions or existing files.")
    if not _yes(input_fn, output_fn, "Connect this NAS export?"):
        raise SafetyError("NAS connection cancelled. No mount or fstab changes were made.")
    # Recheck after the human review, before even creating the local mountpoint.
    _available_mountpoint(target, runner)
    latest, latest_info = _read_fstab(fstab_path)
    if latest != original or (original_info is not None and
            (latest_info is None or latest_info.st_ino != original_info.st_ino)):
        raise SafetyError("fstab changed during review. Review the connection again; nothing was mounted.")
    target.mkdir(parents=True, exist_ok=True, mode=0o755)
    try:
        runner(["mount", "-t", "nfs", "-o", "rw,hard,retry=0", source, str(target)], timeout=60)
    except SafetyError as exc:
        raise SafetyError("The NAS mount did not complete. Its fstab was not changed. Check the export path, NFS service and whether this VM IP/UID is allowed by the NAS; retry setup after fixing that prerequisite.") from exc
    found = storage.mount_identity(target, runner)
    if (found["target"] != str(target) or found["source"] != source
            or found["fstype"] not in NFS_FILESYSTEMS or "rw" not in found["options"].split(",")
            or set(found["options"].split(",")).intersection({"soft", "softerr", "softreval"})):
        raise SafetyError("The mounted target/source/type did not match the reviewed NAS connection. The mount was retained for inspection; fstab was not changed.")
    backup = _persist_mount(Path(fstab_path), original, original_info, found, runner)
    output_fn("NAS connected. " + ("fstab backup: " + str(backup) if backup else "Existing fstab entry retained."))
    return found


def _choose_nfs(input_fn, output_fn, runner, fstab_path):
    rows = [row for row in _safe_mount_rows(runner) if row["fstype"] in NFS_FILESYSTEMS]
    if not rows:
        output_fn("No NFS export is mounted yet. We can connect one without changing its NAS permissions.")
        return connect_nfs(input_fn=input_fn, output_fn=output_fn, runner=runner, fstab_path=fstab_path)
    for index, row in enumerate(rows, 1):
        output_fn(f"  {index}. {row['source']} at {row['target']}")
    output_fn("  new. Connect a different NAS export")
    choices = {str(index): row for index, row in enumerate(rows, 1)}
    choices["new"] = None
    selected = _choice(input_fn, output_fn, "Which NAS mount?", choices, "1")
    if selected is None:
        return connect_nfs(input_fn=input_fn, output_fn=output_fn, runner=runner, fstab_path=fstab_path)
    found = storage.mount_identity(Path(selected["target"]), runner)
    if any(found[key] != selected[key] for key in ("target", "source", "fstype")):
        raise SafetyError("The NAS mount changed during selection. Rerun setup and review it again.")
    if "rw" not in found["options"].split(","):
        raise SafetyError("That NAS export is read-only. Select a dedicated writable export; setup will not change its permissions.")
    if set(found["options"].split(",")).intersection({"soft", "softerr", "softreval"}):
        raise SafetyError("That NAS export uses a soft mount, which is unsuitable for these application writes. Review/remount it deliberately as hard before continuing; setup did not change it.")
    original, original_info = _read_fstab(fstab_path)
    if not _check_fstab(original, found["target"], found["source"]):
        output_fn("The selected NAS is mounted but has no fstab entry. Add this connection for reboot, keeping a private backup:")
        output_fn(f"{found['source']} {found['target']} {found['fstype']} rw,hard,_netdev,nofail,x-systemd.mount-timeout=30s 0 0")
        if not _yes(input_fn, output_fn, "Keep this NAS export connected after reboot?", True):
            raise SafetyError("The existing NAS mount was retained unchanged. Persistent NAS setup is incomplete; configure it before continuing, then rerun setup.")
        current = storage.mount_identity(Path(found["target"]), runner)
        if any(current[key] != found[key] for key in ("target", "source", "fstype")):
            raise SafetyError("The NAS mount changed during review. No fstab entry was written; select it again.")
        backup = _persist_mount(Path(fstab_path), original, original_info, current, runner)
        output_fn("NAS connection saved for reboot. Private fstab backup: " + str(backup))
    else:
        output_fn("Using the existing mounted NAS and compatible persistent fstab entry unchanged.")
    return found


def ensure_site_config(config_path="site.json", *, input_fn=input, output_fn=print,
                       runner=run, fstab_path=Path("/etc/fstab")):
    """Reuse saved choices or save a reviewed compact-VM configuration at mode 600."""
    config_path = no_symlinks(Path(config_path))
    if config_path.exists():
        config = load_config(config_path)
        output_fn("Using saved setup from " + str(config_path) + ". No choices were overwritten.")
        return config
    output_fn("First setup: a private media server in this VM. Existing media is never moved or adopted.")
    addresses = discover_lan_addresses(runner)
    if not addresses:
        raise SafetyError("This VM has no usable private LAN IPv4 address. Fix its network/DHCP connection, then rerun setup.")
    for index, (address, interface) in enumerate(addresses, 1):
        output_fn(f"  {index}. {address} ({interface})")
    if len(addresses) == 1:
        address = addresses[0][0]
        output_fn("Using this VM's LAN address: " + address)
    else:
        address = _choice(input_fn, output_fn, "Which LAN address should the apps use?",
                          {str(i): pair[0] for i, pair in enumerate(addresses, 1)}, "1")
    from .cli import default_config
    config = default_config(address)
    while True:
        name = _ask(input_fn, "Short name for this installation", "homelab")
        if re.fullmatch(r"[a-z][a-z0-9-]{1,39}", name):
            config["deployment_name"] = name
            break
        output_fn("Use 2-40 lowercase letters, digits or hyphens, starting with a letter.")
    config["install_root"] = str(_new_directory(input_fn, output_fn, "Local app/config directory", "/opt/samurai", installation=True))
    while True:
        timezone = _ask(input_fn, "Timezone", detected_timezone())
        try:
            ZoneInfo(timezone)
            config["timezone"] = timezone
            break
        except (ZoneInfoNotFoundError, ValueError):
            output_fn("Use an installed timezone such as America/Denver or Etc/UTC.")
    output_fn("Download method: 1. Torrents and VPN  2. Usenet  3. Both")
    method = _choice(input_fn, output_fn, "Choose download method", {"1": "torrents", "2": "usenet", "3": "both", "torrents": "torrents", "usenet": "usenet", "both": "both"}, "1")
    config["features"]["usenet"] = method in {"usenet", "both"}
    config["features"]["torrents"] = method in {"torrents", "both"}
    config["downloads"]["prefer"] = "torrents" if method != "usenet" else "usenet"
    if method == "both":
        config["downloads"]["prefer"] = _choice(input_fn, output_fn, "Preferred download method", {"torrents": "torrents", "usenet": "usenet"}, "torrents")
    output_fn("Storage: 1. NAS using NFS  2. Local Linux disk (including a temporary smoke-test tree)")
    mode = _choice(input_fn, output_fn, "Where will downloads and media live?", {"1": "nfs", "2": "local", "nfs": "nfs", "local": "local"}, "1")
    if mode == "nfs":
        found = _choose_nfs(input_fn, output_fn, runner, fstab_path)
        default_data = str(PurePosixPath(found["target"]) / "samurai")
        while True:
            data = _new_directory(input_fn, output_fn, "New dedicated downloads/library directory", default_data)
            actual = storage.mount_identity(data, runner)
            if str(data) == found["target"] or any(actual[key] != found[key] for key in ("target", "source", "fstype")):
                output_fn("That directory is outside the selected NAS mount. Choose an empty child of " + found["target"] + ".")
                continue
            if "rw" not in actual["options"].split(","):
                output_fn("That storage is read-only. Select writable dedicated storage.")
                continue
            break
    else:
        from .setup_local_storage import choose_local_directory
        data, actual = choose_local_directory(input_fn=input_fn, output_fn=output_fn, runner=runner)
    config["storage"] = {"mode": mode, "data_root": str(data), "allow_existing_contents": False,
                         "expected_mount": {key: actual[key] for key in ("target", "source", "fstype")}}
    output_fn("Media files use numeric Linux IDs. Default 1000:1000 usually matches the first normal user.")
    output_fn("For NFS, the NAS must allow these IDs on this dedicated export. Setup never changes NAS ACLs, enables root access or recursively changes existing file permissions.")
    config["puid"] = _media_id(input_fn, output_fn, "Media user ID (UID)")
    config["pgid"] = _media_id(input_fn, output_fn, "Media group ID (GID)")
    config["features"].update({"photos": False, "music": False, "dashboard": False, "music_assistant": False})
    config["hardware"]["acceleration"] = "cpu"
    _compose_names(config, input_fn, output_fn)
    validate_config(config)
    # Confirm that application state itself is local and not another shared mount.
    app_mount = storage.mount_identity(config["install_root"], runner)
    if app_mount["fstype"] not in LOCAL_FILESYSTEMS:
        raise SafetyError("The app/config directory must be on local ext4/XFS/Btrfs/ZFS storage. NAS databases are not supported.")
    output_fn("Review: " + config["deployment_name"] + " at " + address + ", " + method + ", CPU playback.")
    output_fn("Apps: " + config["install_root"] + "; media: " + config["storage"]["data_root"])
    output_fn('Compose stacks: ' + ', '.join(config['compose_projects'].values()))
    mount = config['storage']['expected_mount']
    output_fn("Backing storage: " + mount['source'] + " mounted at " + mount['target'] + " (" + mount['fstype'] + ")")
    output_fn(f"Media identity: {config['puid']}:{config['pgid']}. Photos/music remain off until their later setup parts.")
    if not _yes(input_fn, output_fn, "Save these choices and continue?", True):
        raise SafetyError("Setup stopped without saving a new site.json. Any separately confirmed NAS connection was retained.")
    # No-clobber publication also protects against a config created during review.
    write_json(config_path, config)
    output_fn("Saved private setup choices in " + str(config_path) + ". Future parts reuse this file.")
    return config
