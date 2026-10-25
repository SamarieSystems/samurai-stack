"""One-host stage coordinator. No partition, format, migration or delete command."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar
import xml.etree.ElementTree as ET

from . import __version__, storage
from .safety import SafetyError, atomic_write, contained, digest, no_symlinks, private_read, run, validate_config, write_json

ROLES = {2: "media", 3: "torrent", 4: "management", 5: "photos", 6: "music"}
STAGES = {0: "storage", 2: "media", 3: "torrent", 4: "management", 5: "photos", 6: "music"}


def now():
    return datetime.now(timezone.utc).isoformat()


def linux_only():
    if not sys.platform.startswith("linux"):
        raise SafetyError("Apply/verify run inside the target Linux VM. Windows can create and inspect configuration only")
    if Path("/etc/pve").is_dir():
        raise SafetyError("Application stages must run inside a guest, not on the Proxmox hypervisor")


def host_identity():
    try:
        return hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
    except OSError as exc:
        raise SafetyError("Cannot identify this Linux host") from exc


def stage_path(c, stage):
    return Path(c["install_root"]) / "stages" / f"{c['deployment_name']}-{stage}"


def compose_path(c, stage):
    return stage_path(c, stage) / "compose.yaml"


def receipt_path(c, stage):
    return Path(c["install_root"]) / "state" / f"stage-{stage}.json"


def docker(runner, *args, timeout=120):
    # Explicit local default context prevents a user's remote Docker context from
    # silently deploying onto a different person's server.
    return runner(["docker", "--context", "default", *map(str, args)], timeout=timeout)


def check_docker(runner=run):
    endpoint = docker(runner, "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}").stdout.strip()
    if not endpoint.startswith("unix://"):
        raise SafetyError("Only a local Unix-socket Docker engine is supported for application stages")
    version = docker(runner, "compose", "version", "--short").stdout.strip().lstrip("v")
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 24):
        raise SafetyError("Docker Compose v2.24 or newer is required")
    docker(runner, "info", "--format", "{{.OSType}}")


def stage_allowed(c, stage):
    if stage not in STAGES:
        raise SafetyError("Unknown application stage; stage 1 uses the separate proxmox command")
    role = c.get("role", "all")
    if stage and stage != 4 and role != "all" and ROLES[stage] != role:
        raise SafetyError("This stage belongs to a different configured VM role")
    if stage and stage != 4 and c["hosts"][ROLES[stage]] != c["bind_address"]:
        raise SafetyError("Stage host address differs from this configuration's bind_address")
    feature = {3: "torrents", 5: "photos", 6: "music"}.get(stage)
    if feature and not c["features"].get(feature):
        raise SafetyError(f"Stage {stage} is disabled; review its feature switch first")


def preflight(c, stage, runner=run):
    linux_only()
    validate_config(c)
    stage_allowed(c, stage)
    identity = storage.check_mount(c, runner) if stage != 4 else None
    if stage:
        if stage != 4:
            storage.verify(c, runner)
        check_docker(runner)
    # Local databases/configuration must never follow media onto NFS/SMB.
    app_mount = storage.mount_identity(c["install_root"], runner)
    if app_mount["fstype"] not in {"ext4", "xfs", "btrfs", "zfs"}:
        raise SafetyError("install_root must use local Linux application storage, not a NAS share")
    ip_result = runner(["ip", "-j", "address", "show"])
    addresses = {a.get("local") for iface in json.loads(ip_result.stdout) for a in iface.get("addr_info", [])}
    if c["bind_address"] not in addresses:
        raise SafetyError("bind_address is not present on this host")
    return {"host_id": host_identity(), "mount": {k: identity[k] for k in ["target", "source", "fstype"]} if identity else None}


def read_lock(c, stage):
    path = stage_path(c, stage) / "images.lock.json"
    if not path.exists():
        return {}
    no_symlinks(path)
    lock = json.loads(path.read_text())
    if not isinstance(lock, dict) or lock.get("deployment_id") != c["deployment_id"]:
        raise SafetyError("Image lock belongs to another deployment")
    images = lock.get("images", {})
    if not isinstance(images, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.search(r"@sha256:[0-9a-f]{64}$", v) or image_repository(k) != image_repository(v) for k, v in images.items()):
        raise SafetyError("Image lock is malformed or points to a different repository")
    return images


def image_repository(value):
    repo = value.split("@")[0]
    if ":" in repo.rsplit("/", 1)[-1]:
        repo = repo.rsplit(":", 1)[0]
    return repo.removeprefix("docker.io/").removeprefix("library/")


def recipe(c, stage, *, use_lock=True):
    from . import catalog
    document = catalog.render_stage(c, stage)
    for name, service in document.get("services", {}).items():
        service["restart"] = "no"
        service.setdefault("labels", {})["systems.samarie.deployment"] = c["deployment_id"]
        service["labels"]["systems.samarie.stage"] = str(stage)
    document["name"] = f"{c['deployment_name']}-{stage}"
    if use_lock:
        locks = read_lock(c, stage)
        for service in document.get("services", {}).values():
            image = service.get("image")
            if image in locks:
                service["image"] = locks[image]
    return document


def plan(c, stage, *, offline=False, runner=run):
    validate_config(c)
    stage_allowed(c, stage)
    host = {"offline": True} if offline else preflight(c, stage, runner)
    path = compose_path(c, stage)
    no_symlinks(path)
    payload = {
        "schema_version": 1, "toolkit_version": __version__, "kind": "application-stage",
        "deployment_id": c["deployment_id"], "stage": stage,
        "config_digest": digest(c), "host": host,
        "recipe_digest": digest(recipe(c, stage)) if stage else digest(storage.DIRECTORIES),
        "compose_preview": recipe(c, stage) if stage else None,
        "existing_compose_digest": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
        "actions": ["verify exact storage mount", "create missing dedicated directories without modifying existing files"] if stage == 0 else ["verify storage marker", "lock image digests", "generate private configuration", "start this stage only", "verify service health"],
        "existing_media_migration": False,
    }
    payload["review_hash"] = digest(payload)
    return payload


def verify_plan(c, stage, document, accepted, runner=run):
    signed = dict(document)
    review_hash = signed.pop("review_hash", None)
    if not accepted or accepted != review_hash or digest(signed) != review_hash:
        raise SafetyError("Reviewed plan hash does not match --accept-plan")
    if document.get("host", {}).get("offline"):
        raise SafetyError("Offline plans are for review only; make a live plan on the target VM")
    current = plan(c, stage, runner=runner)
    if current["review_hash"] != review_hash:
        raise SafetyError("Plan is stale or belongs to another host/configuration; inspect and generate a new plan")


def _new_directory(path, *, mode=0o700):
    path = no_symlinks(path)
    if path.exists() and not path.is_dir():
        raise SafetyError("A required managed directory is occupied by a file")
    path.mkdir(parents=True, exist_ok=True, mode=mode)


def initialize_install(c):
    root = no_symlinks(c["install_root"])
    marker = root / ".samurai-install.json"
    if marker.exists():
        if json.loads(private_read(marker)).get("deployment_id") != c["deployment_id"]:
            raise SafetyError("install_root belongs to another deployment")
    elif root.exists() and any(root.iterdir()):
        raise SafetyError("Refusing nonempty unmanaged install_root; choose a new dedicated directory")
    _new_directory(root)
    if not marker.exists():
        write_json(marker, {"deployment_id": c["deployment_id"], "version": __version__})
    for relative in ["state", "secrets", "stages", "appdata", "backups"]:
        _new_directory(contained(root / relative, root))


@contextmanager
def deployment_lock(c):
    path = Path(c["install_root"]) / "state" / "apply.lock"
    no_symlinks(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SafetyError("Another apply is active or interrupted. Inspect state/apply.lock and its PID before manual recovery") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"pid": os.getpid(), "host": host_identity(), "started": now()}, stream)
        yield
    finally:
        path.unlink()  # Only the invocation's exclusive lock, never application data.


def secret_path(c, name):
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name):
        raise SafetyError("Invalid secret name")
    return contained(Path(c["install_root"]) / "secrets" / name, Path(c["install_root"]) / "secrets")


def secret_resolver(c):
    def resolve(name):
        path = secret_path(c, name)
        if path.exists():
            value = read_secret(c, name).rstrip('\r\n')
            if not value.strip():
                raise SafetyError(f"Secret {name} is empty")
            return value
        if name == "bazarr_api_key":
            config = no_symlinks(Path(c["install_root"]) / "appdata" / "bazarr" / "config" / "config.yaml")
            # Bazarr's generated YAML uses a top-level auth map and a plain
            # 32-hex key. Extract only that known scalar shape; complex YAML,
            # duplicate entries and aliases require an explicit secret import.
            if config.is_file() and config.stat().st_size <= 2 * 1024 * 1024:
                lines = config.read_text(encoding="utf-8").splitlines()
                starts = [i for i, line in enumerate(lines) if re.match(r"^auth\s*:", line)]
                if len(starts) == 1 and lines[starts[0]] == "auth:":
                    section = []
                    for line in lines[starts[0] + 1:]:
                        if line and not line.startswith((" ", "#")):
                            break
                        section.append(line)
                    rows = [line for line in section if re.match(r"^\s+apikey\s*:", line)]
                    if len(rows) == 1 and not any(re.match(r"^\s+<<\s*:", line) for line in section):
                        match = re.fullmatch(r'''  apikey: (?P<quote>['"]?)(?P<key>[a-fA-F0-9]{32})(?P=quote)\s*''', rows[0])
                        if match:
                            return match.group("key")
            raise SafetyError("Bazarr API key was not found in its generated config/config.yaml; use secret bazarr_api_key to import it privately")
        if name in {"sonarr_api_key", "radarr_api_key", "prowlarr_api_key"}:
            app = name.removesuffix("_api_key")
            config = no_symlinks(Path(c["install_root"]) / "appdata" / app / "config.xml")
            if config.exists():
                value = ET.parse(config).findtext("ApiKey")
                if value:
                    return value
        if name == "sab_api_key":
            path = no_symlinks(Path(c["install_root"]) / "appdata" / "sabnzbd" / "sabnzbd.ini")
            # SAB uses ConfigObj, including assignments before any section and
            # nested [[sections]]. Do not feed that grammar to ConfigParser.
            if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                lines = path.read_text(encoding="utf-8").splitlines()
                starts = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[misc\]\s*", line)]
                if len(starts) == 1:
                    section = []
                    for line in lines[starts[0] + 1:]:
                        if line.lstrip().startswith("["):
                            break
                        section.append(line)
                    rows = [line for line in section if re.match(r"\s*api_key\s*=", line)]
                    if len(rows) == 1:
                        match = re.fullmatch(r'''\s*api_key\s*=\s*(?P<quote>['"]?)(?P<key>[a-fA-F0-9]{32})(?P=quote)\s*''', rows[0])
                        if match:
                            return match.group("key")
            raise SafetyError("SABnzbd API key was not found in its generated [misc] settings; use secret sab_api_key to import it privately")
        raise SafetyError(f"Missing {name}; use the secret command or finish the relevant application enrollment")
    return resolve


def read_secret(c, name):
    path = secret_path(c, name)
    if name == "immich_db_password" and os.name == "posix":
        info = path.stat()
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o440 and info.st_gid == c["pgid"] and info.st_uid == 0:
            return path.read_text()
    return private_read(path)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(method, url, headers=None, json=None, form=None, timeout=20):
    import json as codec
    data = codec.dumps(json).encode() if json is not None else None
    values = dict(headers or {})
    if data is not None:
        values["Content-Type"] = "application/json"
    if form is not None:
        if json is not None:
            raise SafetyError("An HTTP request cannot contain both form and JSON bodies")
        data = urllib.parse.urlencode(form).encode()
        values["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=values, method=method)
    try:
        with _HTTP.open(req, timeout=timeout) as response:
            payload = response.read(8 * 1024 * 1024)
            if not payload:
                return None
            try:
                return codec.loads(payload)
            except ValueError:
                return payload.decode("utf-8")
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SafetyError(f"Application HTTP request returned {code}; inspect service enrollment/authentication") from None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SafetyError("Application API unavailable or returned invalid JSON") from None


def prepare_secrets(c, stage):
    from . import catalog
    required = catalog.expected_secret_names(c, stage) if hasattr(catalog, "expected_secret_names") else []
    generated = {"qbit_password", "immich_db_password", "homarr_encryption_key", "slskd_api_key", "slskd_password"}
    for name in required:
        path = secret_path(c, name)
        if not path.exists() and name in generated:
            value = secrets.token_hex(32) if name in {"homarr_encryption_key", "immich_db_password"} else secrets.token_urlsafe(36)
            atomic_write(path, value + "\n")
        elif not path.exists() and name in {"qbit_username", "slskd_username"}:
            atomic_write(path, "stackadmin\n")
        if not path.exists():
            raise SafetyError(f"Missing {name}; provide it privately with the secret command before apply")
        read_secret(c, name)
        if name == "immich_db_password" and os.name == "posix":
            os.chown(path, 0, c["pgid"])
            os.chmod(path, 0o440)


def seed_qbittorrent(c):
    """Set authenticated, tun0-bound startup config before the first process runs."""
    path = Path(c["install_root"]) / "appdata" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"
    no_symlinks(path)
    if path.exists():
        return  # Never overwrite application-managed configuration.
    resolve = secret_resolver(c)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", resolve("qbit_username")):
        raise SafetyError("qBit username must use 1-64 letters, digits, dots, underscores or hyphens")
    import base64
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha512", resolve("qbit_password").encode(), salt, 100000)
    password_hash = base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode()
    _new_directory(path.parent, mode=0o775)
    text = "[Preferences]\nWebUI\\Username=" + resolve("qbit_username") + "\nWebUI\\Password_PBKDF2=\"@ByteArray(" + password_hash + ")\"\nWebUI\\LocalHostAuth=true\nWebUI\\AuthSubnetWhitelistEnabled=false\nWebUI\\Address=*\nWebUI\\Port=8081\nWebUI\\UseUPnP=false\nWebUI\\CSRFProtection=true\nWebUI\\ClickjackingProtection=true\nWebUI\\HostHeaderValidation=true\nConnection\\UPnP=false\n\n[BitTorrent]\nSession\\Interface=tun0\nSession\\InterfaceName=tun0\nSession\\DefaultSavePath=/data/downloads/torrents/manual\nSession\\TempPath=/data/downloads/torrents/incomplete\nSession\\TempPathEnabled=true\nSession\\GlobalUPLimit=0\nSession\\GlobalDLSpeedLimit=0\nSession\\MaxUploads=-1\nSession\\MaxUploadsPerTorrent=-1\n"
    atomic_write(path, text)
    if os.name == "posix":
        os.chown(path.parent, c["puid"], c["pgid"])
        os.chown(path, c["puid"], c["pgid"])


def env_files(c, stage):
    from . import catalog
    mapping = catalog.environment_secrets(c, stage) if hasattr(catalog, "environment_secrets") else {}
    resolve = secret_resolver(c)
    for service, variables in mapping.items():
        lines = []
        for env_name, secret_name in variables.items():
            value = resolve(secret_name)
            if any(ch in value for ch in ["\n", "\r", "\x00"]):
                raise SafetyError("Multiline credentials cannot be written to an env file")
            encoded = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
            lines.append(env_name + '="' + encoded + '"')
        path = contained(stage_path(c, stage) / (service + ".env"), stage_path(c, stage))
        atomic_write(path, "\n".join(lines) + "\n", replace=path.exists())


def pin_images(c, stage, runner=run):
    document = recipe(c, stage, use_lock=False)
    locks = read_lock(c, stage)
    for service in document["services"].values():
        image = service["image"]
        if image in locks:
            try:
                docker(runner, "image", "inspect", locks[image], "--format", "{{.Id}}")
            except SafetyError:
                docker(runner, "pull", locks[image], timeout=900)
            continue
        docker(runner, "pull", image, timeout=900)
        refs = json.loads(docker(runner, "image", "inspect", image, "--format", "{{json .RepoDigests}}").stdout)
        if not refs:
            raise SafetyError("Image registry did not provide an immutable digest")
        candidates = [r for r in refs if image_repository(r) == image_repository(image)]
        if not candidates or not re.search(r"@sha256:[0-9a-f]{64}$", candidates[0]):
            raise SafetyError("Image digest does not match the requested image repository")
        locks[image] = candidates[0]
    path = stage_path(c, stage) / "images.lock.json"
    write_json(path, {"deployment_id": c["deployment_id"], "images": locks}, replace=path.exists())
    return recipe(c, stage)


def compose_args(c, stage):
    return ["compose", "--project-name", f"{c['deployment_name']}-{stage}", "--file", str(compose_path(c, stage))]


def service_health(c, stage, runner=run):
    raw = docker(runner, *compose_args(c, stage), "ps", "--all", "--format", "json").stdout.strip()
    try:
        rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line]
    except ValueError as exc:
        raise SafetyError("Could not parse Compose service health") from exc
    expected = set(recipe(c, stage)["services"])
    found = {row.get("Service"): row for row in rows}
    if expected - found.keys():
        raise SafetyError("Stage is missing one or more containers")
    problems = [name for name in expected if found[name].get("State") != "running" or found[name].get("Health") not in {None, "", "healthy"}]
    if problems:
        raise SafetyError("Services not ready: " + ", ".join(sorted(problems)))
    return {name: {k: found[name].get(k) for k in ["State", "Health"]} for name in sorted(expected)}


def check_container_ownership(c, stage, runner=run):
    ids = docker(runner, "ps", "--all", "--filter", f"label=com.docker.compose.project={c['deployment_name']}-{stage}", "--format", "{{.ID}}").stdout.split()
    if not ids:
        return
    raw = docker(runner, "inspect", *ids, "--format", "{{json .Config.Labels}}").stdout
    for line in raw.splitlines():
        labels = json.loads(line)
        if labels.get("systems.samarie.deployment") != c["deployment_id"] or labels.get("systems.samarie.stage") != str(stage):
            raise SafetyError("Compose project collides with containers owned by another deployment; choose a unique deployment_name")


def torrent_network_proof(c, runner=run):
    """Read-only namespace/tunnel/forwarded-port checks; no tunnel toggling."""
    args = compose_args(c, 3)
    gluetun = docker(runner, *args, "ps", "--quiet", "gluetun").stdout.strip()
    qbit = docker(runner, *args, "ps", "--quiet", "qbittorrent").stdout.strip()
    if not gluetun or not qbit:
        raise SafetyError("VPN/qBittorrent containers are missing")
    gluetun = docker(runner, "inspect", gluetun, "--format", "{{.Id}}").stdout.strip()
    network = docker(runner, "inspect", qbit, "--format", "{{.HostConfig.NetworkMode}}").stdout.strip()
    if network != "container:" + gluetun:
        raise SafetyError("qBittorrent does not share the exact VPN network namespace")
    docker(runner, *args, "exec", "-T", "gluetun", "ip", "address", "show", "dev", "tun0")
    proof = {"namespace": "shared_with_gluetun", "tun0": "present", "active_kill_switch_test": "not_run; complete commissioning checklist"}
    if c["vpn"].get("port_forwarding"):
        raw = docker(runner, *args, "exec", "-T", "gluetun", "cat", "/gluetun/forwarded_port").stdout.strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 65535:
            raise SafetyError("VPN forwarded port is not ready")
        base = f"http://{c['hosts']['torrent']}:8081"
        resolve = secret_resolver(c)
        request("POST", base + "/api/v2/auth/login", headers={"Referer": base + "/"}, form={"username": resolve("qbit_username"), "password": resolve("qbit_password")})
        prefs = request("GET", base + "/api/v2/app/preferences", headers={"Referer": base + "/"})
        if prefs.get("listen_port") != int(raw):
            raise SafetyError("qBittorrent listen port differs from the VPN-forwarded port")
        proof["forwarded_port_matches"] = True
    return proof


def runtime_files(c, stage):
    root = Path(c["install_root"])
    # Standalone guard runs before each systemd start, independently of the checkout.
    guard_source = Path(__file__).with_name("runtime_guard.py").read_text()
    guard_path = root / "runtime_guard.py"
    atomic_write(guard_path, guard_source, replace=guard_path.exists())
    # Each unit must validate the storage described by its own Compose recipe.
    # A later management-only apply may use different storage settings and must
    # not redirect an existing media unit's startup guard to that other tree.
    # Leave the legacy root/site.json intact for units not yet reapplied.
    saved_config = stage_path(c, stage) / "site.json"
    write_json(saved_config, c, replace=saved_config.exists())
    if c["vpn"].get("port_forwarding"):
        source = Path(__file__).resolve().parent.parent / "scripts" / "port_sync.py"
        target = root / "port_sync.py"
        atomic_write(target, source.read_text(), replace=target.exists(), mode=0o444)
    return root, guard_path, saved_config


def install_unit(c, stage, runner=run, *, changed=False):
    root, guard, config = runtime_files(c, stage)
    unit_name = f"samurai-{c['deployment_name']}-{stage}.service"
    path = Path("/etc/systemd/system") / unit_name
    no_symlinks(path)
    if path.exists() and "# Managed by Samurai Stack: " + c["deployment_id"] not in path.read_text():
        raise SafetyError("A systemd unit with this name is not owned by this deployment")
    command = " ".join(["/usr/bin/docker", "--context", "default", *compose_args(c, stage)])
    required_mounts = str(root) if stage == 4 else f"{c['storage']['data_root']} {root}"
    text = f"# Managed by Samurai Stack: {c['deployment_id']}\n[Unit]\nDescription=Samurai Stack stage {stage}\nRequires=docker.service\nAfter=docker.service network-online.target\nWants=network-online.target\nRequiresMountsFor={required_mounts}\nStartLimitIntervalSec=300\nStartLimitBurst=3\n\n[Service]\nType=simple\nUMask=0077\nExecStartPre=/usr/bin/python3 {guard} {config} {stage}\nExecStart={command} up --no-color --abort-on-container-failure --pull never\nExecStop={command} stop --timeout 45\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=75\n\n[Install]\nWantedBy=multi-user.target\n"
    changed = changed or not path.exists() or path.read_text() != text
    atomic_write(path, text, replace=path.exists(), mode=0o644)
    runner(["systemctl", "daemon-reload"])
    runner(["systemctl", "enable", unit_name])
    runner(["systemctl", "restart" if changed else "start", unit_name])
    return unit_name


def apply(c, stage, document, accepted, *, runner=run, ready_timeout=180):
    verify_plan(c, stage, document, accepted, runner)
    if os.name == "posix" and os.geteuid() != 0 and (stage != 0 or os.geteuid() != c["puid"] or os.getegid() != c["pgid"]):
        raise SafetyError("Apply requires sudo on the target VM; filesystem and service ownership must be controlled")
    initialize_install(c)
    with deployment_lock(c):
        # Another accepted apply may have finished between the early review
        # check and this lock acquisition. Revalidate before changing receipts
        # or deployment files, while this invocation owns the lock.
        verify_plan(c, stage, document, accepted, runner)
        path = receipt_path(c, stage)
        receipt = {"deployment_id": c["deployment_id"], "stage": stage, "config_digest": digest(c), "started": now(), "status": "applying"}
        write_json(path, receipt, replace=path.exists())
        try:
            if stage == 0:
                result = storage.prepare(c, runner)
            else:
                from . import catalog
                _new_directory(stage_path(c, stage))
                for entry in catalog.stage_directories(c, stage):
                    entry = contained(entry, c["install_root"])
                    existed = entry.exists()
                    _new_directory(entry, mode=0o775)
                    if not existed and os.name == "posix":
                        os.chown(entry, c["puid"], c["pgid"])
                prepare_secrets(c, stage)
                if stage == 2:
                    # Establish freshness before any application can create an
                    # owner or database. Legacy installs are never enrolled by
                    # guessing from an unauthenticated first-run screen.
                    from .onboarding import reserve_fresh_apps
                    reserve_fresh_apps(c, stage=stage, runner=runner)
                if stage == 3:
                    seed_qbittorrent(c)
                env_files(c, stage)
                compiled = pin_images(c, stage, runner)
                target = compose_path(c, stage)
                changed = not target.exists() or json.loads(target.read_text()) != compiled
                # Inspect project labels before Compose can recreate any container.
                check_container_ownership(c, stage, runner)
                if target.exists():
                    backup = Path(c["install_root"]) / "backups" / (f"stage-{stage}-" + str(time.time_ns()) + ".compose.json")
                    atomic_write(backup, target.read_text())
                write_json(target, compiled, replace=target.exists())
                if stage != 4:
                    storage.verify(c, runner)
                docker(runner, *compose_args(c, stage), "config", "--quiet")
                unit = install_unit(c, stage, runner, changed=changed)
                deadline = time.monotonic() + ready_timeout
                while True:
                    try:
                        health = service_health(c, stage, runner)
                        break
                    except SafetyError:
                        if time.monotonic() >= deadline:
                            raise SafetyError("Stage readiness timed out; containers/configuration preserved. Inspect journalctl for this stage") from None
                        time.sleep(3)
                result = {"unit": unit, "containers": health, "configuration": "pending; run configure then verify"}
            receipt.update(status="prepared" if stage == 0 else "started", finished=now(), result=result)
            write_json(path, receipt, replace=True)
            return receipt
        except Exception as exc:
            receipt.update(status="failed", finished=now(), reason="Stage failed; inspect private application logs. No data or volumes were removed")
            write_json(path, receipt, replace=True)
            raise


def configure(c, stage):
    from . import integrations
    linux_only()
    preflight(c, stage)
    with deployment_lock(c):
        service_health(c, stage)
        check_container_ownership(c, stage)
        if stage == 3:
            torrent_network_proof(c)
        result = integrations.configure_stage(c, stage, request=request, secret_resolver=secret_resolver(c))
        write_json(stage_path(c, stage) / "configuration-report.json", result, replace=(stage_path(c, stage) / "configuration-report.json").exists())
    return result


def verify(c, stage, runner=run):
    preflight(c, stage, runner)
    if stage == 0:
        result = {"deployment_id": c["deployment_id"], "stage": 0, "checked": now(), "status": "verified", **storage.verify(c, runner)}
        write_json(receipt_path(c, stage), result, replace=receipt_path(c, stage).exists())
        return result
    from . import integrations
    health = service_health(c, stage, runner)
    check_container_ownership(c, stage, runner)
    wiring = integrations.verify_stage(c, stage, request=request, secret_resolver=secret_resolver(c))
    if not isinstance(wiring, dict) or not isinstance(wiring.get("status"), str):
        raise SafetyError("Application verifier returned a malformed report; refusing verification")
    report = {"deployment_id": c["deployment_id"], "stage": stage, "checked": now(), "containers": health, "wiring": wiring}
    if stage == 3:
        report["network"] = torrent_network_proof(c, runner)
    manual = manual_enrollment(c, stage, wiring)
    if isinstance(wiring, dict) and not manual and (wiring.get("status") != "verified" or wiring.get("errors") or wiring.get("pending")):
        raise SafetyError("Service processes are running but application enrollment or wiring remains incomplete")
    if manual:
        report["manual_enrollment"] = manual
    report["status"] = "verified_with_manual_checks" if manual else "verified"
    write_json(receipt_path(c, stage), report, replace=True)
    return report


MANUAL_CHECKS = {
    4: ["Created Dockge administrator and tested login", "Created Uptime Kuma administrator and tested login",
        "Added intended service monitors", "Sent and received a test notification using chosen provider",
        "If Homarr is enabled, created its administrator and tested login",
        "Kept management interfaces private; no router port forwarding"],
    6: ["Created Navidrome administrator and tested login", "Played an actual library song",
        "Signed into slskd and confirmed Soulseek connectivity", "Reviewed downloads and library paths",
        "If Music Assistant is enabled, tested its administrator/provider/player enrollment"],
}


def attest(c, stage, confirmation):
    if stage not in MANUAL_CHECKS or confirmation != "ENROLLMENT COMPLETE":
        raise SafetyError("Manual enrollment requires the displayed checklist and exact ENROLLMENT COMPLETE confirmation")
    preflight(c, stage)
    service_health(c, stage)
    check_container_ownership(c, stage)
    path = stage_path(c, stage) / "manual-enrollment.json"
    value = {"deployment_id": c["deployment_id"], "config_digest": digest(c), "stage": stage,
             "checked": now(), "checks": MANUAL_CHECKS[stage], "method": "operator_attestation; not automated API verification"}
    write_json(path, value, replace=path.exists())
    return value


def manual_enrollment(c, stage, wiring):
    allowed = {4: {"management:enrollment"}, 6: {"music:enrollment"}}
    if not isinstance(wiring, dict) or wiring.get("status") != "pending" or stage not in allowed or wiring.get("errors"):
        return None
    pending = wiring.get("pending", [])
    if not isinstance(pending, list) or any(not isinstance(x, dict) for x in pending) or {x.get("name") for x in pending} != allowed[stage]:
        return None
    path = stage_path(c, stage) / "manual-enrollment.json"
    if not path.exists():
        return None
    value = json.loads(private_read(path))
    if value.get("deployment_id") == c["deployment_id"] and value.get("config_digest") == digest(c) and value.get("stage") == stage and value.get("checks") == MANUAL_CHECKS[stage]:
        return value
    return None
