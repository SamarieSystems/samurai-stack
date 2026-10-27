"""Interactive configuration, explicit stage commands, and private credentials."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import uuid
import warnings

from . import __version__, engine, storage
from .safety import SafetyError, atomic_write, load_config, no_symlinks, private_read, validate_config, write_json
from .proxmox import ProxmoxError


def ask(label, default=None):
    result = input(label + (f" [{default}]" if default is not None else "") + ": ").strip()
    return result or default


def ask_bool(label, default=False):
    while True:
        value = ask(label + "? yes/no", "yes" if default else "no").lower()
        if value in {"yes", "y"}:
            return True
        if value in {"no", "n"}:
            return False
        print("Enter yes or no.")


def default_config(address="192.168.1.50"):
    return {
        "schema_version": 1, "deployment_id": str(uuid.uuid4()), "deployment_name": "homelab",
        "role": "all", "install_root": "/opt/samurai", "bind_address": address,
        "timezone": "Etc/UTC", "puid": 1000, "pgid": 1000,
        "storage": {"mode": "nfs", "data_root": "/mnt/media/samurai", "allow_existing_contents": False,
                    "expected_mount": {"target": "/mnt/media", "source": "192.168.1.10:/export/media", "fstype": "nfs4"}},
        "hosts": {role: address for role in ["media", "torrent", "management", "photos", "music"]},
        "hardware": {"acceleration": "cpu"},
        "downloads": {"prefer": "usenet"},
        "vpn": {"provider": "protonvpn", "type": "wireguard", "port_forwarding": False},
        "features": {"usenet": True, "torrents": True, "photos": False, "music": False, "dashboard": False, "music_assistant": False},
    }


def initialize(args):
    path = no_symlinks(args.config)
    if path.exists():
        raise SafetyError("Configuration already exists; edit it deliberately rather than rerunning init")
    if args.example:
        write_json(path, default_config())
        return {"created": str(path), "next": "Replace example LAN/mount values before planning"}
    print("Samurai Stack, configuration only. No disk, NAS, VM or service changes occur here.")
    c = default_config()
    c["deployment_name"] = ask("Deployment name", "homelab")
    c["role"] = ask("This VM role: all/media/torrent/management/photos/music", "all")
    c["bind_address"] = ask("This VM's LAN IPv4 address", c["bind_address"])
    c["install_root"] = ask("New local application directory", "/opt/samurai")
    c["timezone"] = ask("IANA timezone", "Etc/UTC")
    c["puid"], c["pgid"] = int(ask("Media UID", "1000")), int(ask("Media GID", "1000"))
    for role in c["hosts"]:
        c["hosts"][role] = c["bind_address"] if c["role"] == "all" else ask(role + " VM address", c["bind_address"])
    c["storage"]["mode"] = ask("Storage: nfs/local", "nfs")
    c["storage"]["data_root"] = ask("Dedicated data subdirectory on the mounted storage", "/mnt/media/samurai")
    identity = None
    if sys.platform.startswith("linux"):
        try:
            identity = storage.mount_identity(c["storage"]["data_root"])
            if c["storage"]["mode"] == "local":
                identity = storage.persistent_identity(identity)
            print("Observed backing mount: " + json.dumps(identity))
        except SafetyError:
            pass
    c["storage"]["expected_mount"] = {k: ask("Expected mount " + k, (identity or c["storage"]["expected_mount"])[k]) for k in ["target", "source", "fstype"]}
    for feature in ["usenet", "torrents", "photos", "music", "dashboard", "music_assistant"]:
        c["features"][feature] = ask_bool("Enable " + feature, c["features"][feature])
    c["downloads"]["prefer"] = ask("Preferred download protocol: usenet/torrents", "usenet" if c["features"]["usenet"] else "torrents")
    c["hardware"]["acceleration"] = ask("Jellyfin acceleration: cpu/intel/amd/nvidia", "cpu")
    if c["features"]["torrents"]:
        c["vpn"]["provider"] = ask("VPN provider: protonvpn/mullvad/custom", "protonvpn")
        c["vpn"]["type"] = ask("VPN protocol: wireguard/openvpn", "wireguard")
        if c["vpn"]["provider"] == "protonvpn":
            c["vpn"]["port_forwarding"] = ask_bool("Enable provider port forwarding")
        elif c["vpn"]["provider"] == "mullvad":
            c["vpn"]["wireguard_addresses"] = ask("Provider-issued WireGuard address including /prefix")
    validate_config(c)
    write_json(path, c)
    return {"created": str(path), "deployment_id": c["deployment_id"], "next": "Read README and plan stage 0 on this VM"}


def parser():
    result = argparse.ArgumentParser(description="Non-destructive staged homelab deployment")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", "-c", default="site.json")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create private site configuration interactively")
    init.add_argument("--example", action="store_true")
    commands.add_parser("validate", help="Validate configuration without writes")
    commands.add_parser("inspect", help="Read-only local mount/hardware/runtime inventory")
    photos = commands.add_parser("photos", help="Review native Immich owner/resource setup; never reset accounts or import photos")
    photos.add_argument("action", choices=["plan", "apply", "verify"])
    photos.add_argument("--out", default="photos-plan.json")
    photos.add_argument("--plan")
    photos.add_argument("--accept-plan")
    hardlinks = commands.add_parser("hardlinks", help="Verify only Sonarr/Radarr hardlinks in an existing installation")
    hardlinks.add_argument("--apply", action="store_true", help="Enable hardlinks and verify readback; no scans, imports or redeploy")
    monitor = commands.add_parser("monitor-search", help="Install or verify the owned Sonarr monitoring-change search companion")
    monitor.add_argument("action", choices=["install", "verify"])
    profiles = commands.add_parser("profiles", help="Review bounded media profiles; explicit request choices can classify metadata-confirmed anime")
    profiles.add_argument("action", choices=["plan", "apply", "verify", "binding-plan", "binding-apply"])
    profiles.add_argument("--global-sizes", action="store_true", help="Explicitly include shared quality size limits in plan/verify")
    profiles.add_argument("--request-choices", action="store_true", help="Plan-only opt-in: four HD/anime/encoded-UHD request choices, no searches or automatic upgrades")
    profiles.add_argument("--out", default="profiles-plan.json")
    profiles.add_argument("--plan")
    profiles.add_argument("--accept-plan")
    profiles.add_argument("--previous-config", help="Exact root-private predecessor capture for metadata-only receipt binding migration")
    naming = commands.add_parser("naming", help="Review future-import filenames and Jellyfin folder IDs; never rename existing files")
    naming.add_argument("action", choices=["plan", "apply", "verify"])
    naming.add_argument("--out", default="naming-plan.json")
    naming.add_argument("--plan")
    naming.add_argument("--accept-plan")
    restored = commands.add_parser("restore-accounts", help="Reauthorize verified restored account directories; never create accounts")
    restored.add_argument("action", choices=["plan", "apply", "verify"])
    restored.add_argument("--out", default="restore-accounts-plan.json")
    restored.add_argument("--plan")
    restored.add_argument("--accept-plan")
    indexers = commands.add_parser("indexers", help="Test source readiness or apply one reviewed private Newznab/Torznab intake")
    indexers.add_argument("action", choices=["plan", "apply", "verify"])
    indexers.add_argument("--input-id", help="Opaque saved private input ID from guided step 3; never an API key")
    indexers.add_argument("--out", default="indexers-plan.json")
    indexers.add_argument("--plan")
    indexers.add_argument("--accept-plan")
    subtitles = commands.add_parser("subtitles", help="Review future English subtitle defaults; no existing-library backfill")
    subtitles.add_argument("action", choices=["plan", "apply", "verify", "binding-plan", "binding-apply"])
    subtitles.add_argument("--providers", help="Plan-only comma-separated supported providers; keeps saved choices, new default gestdown (TV only)")
    subtitles.add_argument("--previous-config", help="Binding migration only: exact captured configuration used by legacy subtitle inputs")
    subtitles.add_argument("--out", default="subtitles-plan.json")
    subtitles.add_argument("--plan")
    subtitles.add_argument("--accept-plan")
    capacity = commands.add_parser("capacity", help="Review free-space thresholds and an optional owned qBittorrent pause watcher")
    capacity.add_argument("action", choices=["plan", "apply", "verify"])
    capacity.add_argument("--data-reserve-gib", type=int, help="Whole GiB reserve on media/download storage; plan only, default 50")
    capacity.add_argument("--app-reserve-gib", type=int, help="Whole GiB reserve on application storage; plan only, default 20")
    watcher = capacity.add_mutually_exclusive_group()
    watcher.add_argument("--qbit-guard", dest="qbit_guard", action="store_true", default=None, help="Review the optional owned qBittorrent pause timer")
    watcher.add_argument("--no-qbit-guard", dest="qbit_guard", action="store_false", help="Review disabling this installation's existing capacity timer")
    capacity.add_argument("--out", default="capacity-plan.json")
    capacity.add_argument("--plan")
    capacity.add_argument("--accept-plan")
    upgrade = commands.add_parser("storage-identity", help="Review a known local filesystem UUID upgrade after block-device renumbering")
    upgrade.add_argument("action", choices=["plan", "apply"])
    upgrade.add_argument("--data-uuid", help="Plan only: independently confirmed original media filesystem UUID")
    upgrade.add_argument("--app-uuid", help="Plan only: independently confirmed original application filesystem UUID")
    upgrade.add_argument("--out", default="storage-identity-plan.json")
    upgrade.add_argument("--plan")
    upgrade.add_argument("--accept-plan")
    for name in ["plan", "apply", "configure", "verify", "status", "access"]:
        cmd = commands.add_parser(name)
        cmd.add_argument("stage", type=int, choices=sorted(engine.STAGES))
        if name == "plan":
            cmd.add_argument("--out", default="plan.json")
            cmd.add_argument("--offline", action="store_true", help="Review only; cannot be applied")
        if name == "apply":
            cmd.add_argument("--plan", required=True)
            cmd.add_argument("--accept-plan", required=True)
    secret = commands.add_parser("secret", help="Store a credential without command-line values")
    secret.add_argument("name")
    secret.add_argument("--from-file", help="Import a private VPN config or application credential file")
    attest = commands.add_parser("attest", help="Record manual GUI enrollment after actually testing it")
    attest.add_argument("stage", type=int, choices=[4, 6])
    foundation = commands.add_parser("proxmox", help="Stage 1, run on already installed Proxmox")
    foundation.add_argument("action", choices=["init", "inspect", "plan", "apply", "verify"])
    foundation.add_argument("--out", default="proxmox-plan.json")
    foundation.add_argument("--plan")
    foundation.add_argument("--accept-plan")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args)
        else:
            c = load_config(args.config)
            if args.command == "validate":
                result = {"configuration": "valid", "version": __version__}
            elif args.command == "photos":
                from . import photos_setup
                if args.action == "plan":
                    result = photos_setup.plan(c)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = photos_setup.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Photo apply requires its saved plan and exact --accept-plan hash")
                    result = photos_setup.apply(c, json.loads(private_read(args.plan)), args.accept_plan)
            elif args.command == "hardlinks":
                from . import hardlinks
                result = hardlinks.run(c, apply=args.apply)
            elif args.command == "monitor-search":
                from . import monitor_setup
                result = (monitor_setup.install if args.action == "install" else monitor_setup.verify)(c)
            elif args.command == "profiles":
                from . import media_profiles
                if args.previous_config is not None and args.action not in {"binding-plan", "binding-apply"}:
                    raise SafetyError("A previous request configuration belongs only to binding migration")
                if args.action in {"binding-plan", "binding-apply"}:
                    from . import onboarding, request_profiles
                    if not args.previous_config or args.global_sizes or args.request_choices:
                        raise SafetyError("Request binding migration needs only its exact --previous-config capture; it cannot change policy choices")
                    previous = onboarding._private_json(Path(args.previous_config))
                    if args.action == "binding-plan":
                        result = request_profiles.binding_migration_plan(c, previous)
                        write_json(args.out, result)
                    else:
                        if not args.plan or not args.accept_plan:
                            raise SafetyError("Request binding apply requires --plan and --accept-plan")
                        document = json.loads(private_read(args.plan))
                        if document.get("review_hash") != args.accept_plan:
                            raise SafetyError("Request binding reviewed plan hash does not match")
                        result = request_profiles.apply_binding_migration(c, previous, document)
                elif args.action == "plan":
                    if args.request_choices:
                        from . import request_profiles
                        result = request_profiles.plan(c, include_global_sizes=args.global_sizes)
                    else:
                        result = media_profiles.plan(c, include_global_sizes=args.global_sizes)
                    write_json(args.out, result)
                elif args.action == "verify":
                    if args.request_choices:
                        raise SafetyError("Request choices are selected in the reviewed plan; ordinary verify recognizes the saved policy")
                    result = media_profiles.verify(c, include_global_sizes=args.global_sizes)
                else:
                    if args.global_sizes or args.request_choices or not args.plan or not args.accept_plan:
                        raise SafetyError("Profile apply requires --plan and --accept-plan; global size selection belongs in the reviewed plan")
                    document = json.loads(private_read(args.plan))
                    if document.get("review_hash") != args.accept_plan:
                        raise SafetyError("Profile reviewed plan hash does not match")
                    result = media_profiles.apply(c, document)
            elif args.command == "naming":
                from . import media_naming
                if args.action == "plan":
                    result = media_naming.plan(c)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = media_naming.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Naming apply requires --plan and --accept-plan")
                    document = json.loads(private_read(args.plan))
                    if document.get("review_hash") != args.accept_plan:
                        raise SafetyError("Naming reviewed plan hash does not match")
                    result = media_naming.apply(c, document)
            elif args.command == "restore-accounts":
                from . import restore_accounts
                if args.action == "plan":
                    result = restore_accounts.plan(c)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = restore_accounts.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Account restore requires --plan and --accept-plan")
                    document = json.loads(private_read(args.plan))
                    if document.get('review_hash') != args.accept_plan:
                        raise SafetyError("Account restore reviewed plan hash does not match")
                    result = restore_accounts.apply(c, document)
            elif args.command == "indexers":
                from . import indexer_setup
                if args.action != "plan" and args.input_id is not None:
                    raise SafetyError("--input-id belongs only to an indexer plan; credentials are entered in ./setup 3")
                if args.action == "plan":
                    result = indexer_setup.plan(c, args.input_id)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = indexer_setup.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Indexer apply requires --plan and --accept-plan")
                    document = json.loads(private_read(args.plan))
                    if document.get('review_hash') != args.accept_plan:
                        raise SafetyError("Indexer reviewed plan hash does not match")
                    result = indexer_setup.apply(c, document)
            elif args.command == "subtitles":
                from . import subtitle_setup
                if args.action != "plan" and args.providers is not None:
                    raise SafetyError("Subtitle provider choices belong only in the reviewed plan")
                if args.previous_config is not None and args.action not in {"binding-plan", "binding-apply"}:
                    raise SafetyError("A previous subtitle configuration belongs only to binding migration")
                if args.action in {"binding-plan", "binding-apply"}:
                    from . import onboarding
                    if not args.previous_config:
                        raise SafetyError("Subtitle binding migration requires the exact --previous-config capture")
                    previous = onboarding._private_json(Path(args.previous_config))
                    if args.action == "binding-plan":
                        result = subtitle_setup.binding_migration_plan(c, previous)
                        write_json(args.out, result)
                    else:
                        if not args.plan or not args.accept_plan:
                            raise SafetyError("Subtitle binding apply requires --plan and --accept-plan")
                        document = json.loads(private_read(args.plan))
                        if document.get("review_hash") != args.accept_plan:
                            raise SafetyError("Subtitle binding reviewed plan hash does not match")
                        result = subtitle_setup.apply_binding_migration(c, previous, document)
                elif args.action == "plan":
                    providers = args.providers.split(',') if args.providers is not None else None
                    result = subtitle_setup.plan(c, providers)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = subtitle_setup.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Subtitle apply requires --plan and --accept-plan")
                    document = json.loads(private_read(args.plan))
                    if document.get("review_hash") != args.accept_plan:
                        raise SafetyError("Subtitle reviewed plan hash does not match")
                    result = subtitle_setup.apply(c, document)
            elif args.command == "capacity":
                from . import capacity
                if args.action != "plan" and any(value is not None for value in
                        (args.data_reserve_gib, args.app_reserve_gib, args.qbit_guard)):
                    raise SafetyError("Capacity reserve and watcher choices belong only in the reviewed plan")
                if args.action == "plan":
                    result = capacity.plan(c, data_reserve_gib=50 if args.data_reserve_gib is None else args.data_reserve_gib,
                        app_reserve_gib=20 if args.app_reserve_gib is None else args.app_reserve_gib, qbit_guard=args.qbit_guard)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = capacity.verify(c)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Capacity apply requires --plan and --accept-plan")
                    document = json.loads(private_read(args.plan))
                    if document.get("review_hash") != args.accept_plan:
                        raise SafetyError("Capacity reviewed plan hash does not match")
                    result = capacity.apply(c, document)
            elif args.command == "storage-identity":
                from . import storage_upgrade
                if args.action == "plan":
                    if not args.data_uuid or not args.app_uuid:
                        raise SafetyError("Storage identity review requires the original --data-uuid and --app-uuid")
                    result = storage_upgrade.plan(c, args.config, expected_data_uuid=args.data_uuid, expected_app_uuid=args.app_uuid)
                    write_json(args.out, result)
                else:
                    if args.data_uuid or args.app_uuid or not args.plan or not args.accept_plan:
                        raise SafetyError("Storage identity apply requires --plan and --accept-plan; UUID choices belong in the reviewed plan")
                    document = json.loads(private_read(args.plan))
                    if document.get('review_hash') != args.accept_plan:
                        raise SafetyError("Storage identity reviewed plan hash does not match")
                    result = storage_upgrade.apply(c, document)
            elif args.command == "inspect":
                result = {"platform": sys.platform, "cpus": os.cpu_count(), "role": c.get("role", "all"),
                          "storage": storage.check_mount(c), "gpu_mode": c["hardware"]["acceleration"]}
            elif args.command == "plan":
                result = engine.plan(c, args.stage, offline=args.offline)
                write_json(args.out, result)
            elif args.command == "apply":
                result = engine.apply(c, args.stage, json.loads(Path(args.plan).read_text()), args.accept_plan)
            elif args.command == "configure":
                if args.stage == 0:
                    raise SafetyError("Storage has no separate configure action")
                result = engine.configure(c, args.stage)
            elif args.command == "verify":
                result = engine.verify(c, args.stage)
            elif args.command == "status":
                path = engine.receipt_path(c, args.stage)
                no_symlinks(path)
                result = json.loads(path.read_text()) if path.exists() else {"status": "not started"}
            elif args.command == "access":
                from . import catalog
                result = {"endpoints": catalog.stage_endpoints(c, args.stage), "credential_directory": str(Path(c["install_root"]) / "secrets"), "note": "Complete each application's account enrollment. No secret values are displayed"}
            elif args.command == "secret":
                engine.linux_only()
                engine.initialize_install(c)
                path = engine.secret_path(c, args.name)
                if path.exists():
                    raise SafetyError("Secret already exists; rotations require deliberate application-side changes")
                if args.from_file:
                    value = private_read(args.from_file)
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        try:
                            value = getpass.getpass("Secret value (hidden): ")
                        except getpass.GetPassWarning:
                            raise SafetyError("Hidden credential input is unavailable; use a real terminal or --from-file with a mode-600 file") from None
                if not value.strip():
                    raise SafetyError("Empty secret refused")
                atomic_write(path, value.rstrip("\n") + "\n")
                result = {"stored": args.name, "values_displayed": False}
            elif args.command == "attest":
                print("Complete these manual checks before confirming:", file=sys.stderr)
                for check in engine.MANUAL_CHECKS[args.stage]:
                    print("  - " + check, file=sys.stderr)
                result = engine.attest(c, args.stage, input("Type ENROLLMENT COMPLETE only after testing: "))
            elif args.command == "proxmox":
                from . import proxmox
                p = c.get("proxmox", {})
                if args.action == "init":
                    if p:
                        raise SafetyError("Proxmox configuration already exists; edit it deliberately")
                    info = proxmox.inventory()
                    print("Existing bridge choices: " + ", ".join(n.get("iface", "") for n in info["network"] if n.get("type") == "bridge"))
                    print("Existing storage choices: " + ", ".join(s.get("storage", "") for s in info["storage"] if s.get("active")))
                    total_mib = int(info.get("memory", {}).get("total", 0)) // 1048576
                    cpu_count = int(info.get("cpu", {}).get("cpus", 2))
                    print(f"Detected {cpu_count} logical CPUs and {total_mib} MiB RAM. No hardware/boot changes will be made.")
                    count = int(ask("New VM count: 1 compact, 2 media+torrent, 3 adds management", "2" if total_mib >= 16384 else "1"))
                    if count not in {1, 2, 3}:
                        raise SafetyError("Choose one, two or three guests in this baseline wizard")
                    image = ask("Absolute path to the official local qcow2 cloud image")
                    p = {"deployment_id": c["deployment_id"], "node": info["node"],
                         "storage": ask("Selected existing image-capable storage ID"),
                         "bridge": ask("Selected existing active bridge", "vmbr0"),
                         "image_path": image, "image_sha512": ask("Published SHA512 for that exact image"),
                         "ssh_public_key": ask("Absolute path to your SSH public .pub key"),
                         "admin_user": ask("Guest administrator", "stackadmin"),
                         "state_dir": "/var/lib/samurai-proxmox", "vms": []}
                    roles = ["applications"] if count == 1 else ["media", "torrent", "management"][:count]
                    used_ids = {int(v["vmid"]) for v in info.get("vms", []) if "vmid" in v}
                    print("Occupied VM/container IDs: " + ", ".join(map(str, sorted(used_ids))))
                    next_id = 200
                    for index, role in enumerate(roles):
                        while next_id in used_ids:
                            next_id += 1
                        memory = min(8192 if role in {"media", "applications"} else 2048, max(1024, int(total_mib * 0.65 / count)))
                        selected_id = int(ask(role + " free VM ID", str(next_id)))
                        if selected_id in used_ids:
                            raise SafetyError("That VM/container ID is already occupied")
                        used_ids.add(selected_id)
                        p["vms"].append({"id": selected_id,
                                         "name": ask(role + " VM name", c["deployment_name"] + "-" + role),
                                         "cores": int(ask(role + " vCPU count", str(min(4, max(1, cpu_count // count))))),
                                         "memory_mib": int(ask(role + " RAM MiB", str(memory))),
                                         "disk_gib": int(ask(role + " new virtual disk GiB", "64" if role in {"media", "applications"} else "24")),
                                         "ipconfig": ask(role + " cloud-init IP configuration", "ip=dhcp"),
                                         "start": True, "onboot": True})
                    c["proxmox"] = p
                    validate_config(c)
                    write_json(args.config, c, replace=True)
                    result = {"configured": "proxmox", "vms": len(p["vms"]), "next": "Run proxmox plan and inspect its exact commands before apply"}
                elif args.action == "inspect":
                    result = proxmox.inventory()
                elif args.action == "plan":
                    result = proxmox.plan(p)
                    write_json(args.out, result)
                elif args.action == "verify":
                    result = proxmox.verify(p)
                else:
                    if not args.plan or not args.accept_plan:
                        raise SafetyError("Proxmox apply requires --plan and --accept-plan")
                    document = json.loads(Path(args.plan).read_text())
                    if document.get("review_hash") != args.accept_plan:
                        raise SafetyError("Proxmox reviewed plan hash does not match")
                    result = proxmox.apply(p, document)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if isinstance(result, dict) and result.get("status") in {"pending", "failed", "blocked"} else 0
    except engine.ProjectCollision as exc:
        print(json.dumps({"status": "blocked", "error_code": "EXISTING_INSTALLATION_IDENTITY_CONFLICT",
                          "details": exc.details}, indent=2, sort_keys=True))
        print("ERROR: " + engine.format_project_collision(exc.details), file=sys.stderr)
        return 1
    except (SafetyError, ProxmoxError, OSError, ValueError, KeyError) as exc:
        # Do not echo arbitrary vendor/server exceptions or payloads.
        message = str(exc) if isinstance(exc, (SafetyError, ProxmoxError)) else "Operation failed validation or local I/O; inspect the relevant private configuration"
        print("ERROR: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Existing data and partial provisioning are preserved; inspect before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
