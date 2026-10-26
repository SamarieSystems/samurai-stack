"""One-host stage coordinator. No partition, format, migration or delete command."""
from __future__ import annotations

from contextlib import contextmanager
import copy
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
import uuid
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar
import xml.etree.ElementTree as ET

from . import __version__, storage, naming
from .safety import SafetyError, atomic_write, contained, digest, no_symlinks, private_read, run, validate_config, write_json

ROLES = {2: "media", 3: "torrent", 4: "management", 5: "photos", 6: "music"}
STAGES = {0: "storage", 2: "media", 3: "torrent", 4: "management", 5: "photos", 6: "music"}


class ProjectCollision(SafetyError):
    """Read-only evidence of an existing Docker project owned elsewhere."""
    def __init__(self, details):
        self.details = copy.deepcopy(details)
        super().__init__(format_project_collision(self.details))


def format_project_collision(details):
    """Readable selected inventory, with terminal control characters escaped."""
    quoted = lambda value: json.dumps(value, ensure_ascii=True)
    lines = ["Compose project " + quoted(details["project"]) + " already contains containers with different deployment or stage ownership.",
             "Requested deployment UUID: " + details["requested_deployment_id"]]
    for row in details["containers"]:
        lines.append("  " + quoted(row["name"]) + ": " + row["status"]
                     + ", project " + quoted(row["project"]) + ", stage " + row["stage"]
                     + ", deployment UUID " + row["deployment_id"])
        for mount in row["mounts"]:
            kind = "Appdata" if mount["appdata"] else "Mount"
            existence = "present" if mount["source_exists"] is True else "missing" if mount["source_exists"] is False else "unknown"
            lines.append("    " + kind + " " + quoted(mount["source"]) + " -> "
                         + quoted(mount["destination"]) + ": host source " + existence)
    lines.extend(["Existing containers were not changed or adopted.",
        "Locate the original site configuration and appdata backups for the existing deployment. If the installation folder is missing, run ./setup --recover for read-only recovery inventory before continuing.",
        "Choosing a different project name does not recover missing appdata or release the ports these containers already use."])
    if any(mount["appdata"] and mount["source_exists"] is False
           for row in details["containers"] for mount in row["mounts"]):
        lines.append("Some host appdata sources are missing. Leave running containers in place; review recovery before stopping or recreating them.")
    return "\n".join(lines)


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
    return Path(c["install_root"]) / "stages" / naming.project_name(c, stage)


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
    document["name"] = naming.project_name(c, stage)
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
    naming.check_installed_names(c)
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
    naming.check_installed_names(c)
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
        if name == "seerr_api_key":
            from .enroll_seerr import resolve_api_key
            return resolve_api_key(c)
        if name == "jellyfin_api_key":
            # An already-wired Seerr contains its authenticated Jellyfin token.
            # Reuse it for readback rather than asking for a browser key paste.
            from .enroll_seerr import _settings
            try:
                value = _settings(c).get("jellyfin", {}).get("apiKey")
            except SafetyError:
                value = None
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._~-]{16,4096}", value):
                return value
            raise SafetyError("Jellyfin needs its existing administrator login in guided setup to connect its API")
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
    return ["compose", "--project-name", naming.project_name(c, stage), "--file", str(compose_path(c, stage))]


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


_PROJECT_INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"status":{{json .State.Status}},'
    '"pid":{{json .State.Pid}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"deployment_id":{{json (index .Config.Labels "systems.samarie.deployment")}},'
    '"stage":{{json (index .Config.Labels "systems.samarie.stage")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"mounts":[{{range $i, $m := .Mounts}}{{if $i}},{{end}}'
    '{"type":{{json $m.Type}},"source":{{json $m.Source}},"destination":{{json $m.Destination}}}{{end}}]}')


def _collision_name(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value.lstrip("/")):
        return value.lstrip("/")
    return "[unknown]"


def _collision_uuid(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else "[unlabelled]"
    except ValueError:
        return "[unrecognized]"


def _collision_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or re.search(r"[a-z][a-z0-9+.-]*://", value, re.I)):
        return None
    return value


def _collision_choice(value, choices):
    return value if isinstance(value, str) and value in choices else "unknown"


def _collision_container(row):
    mounts = row.get("mounts")
    if not isinstance(mounts, list) or len(mounts) > 256 or any(not isinstance(item, dict) for item in mounts):
        raise SafetyError("Existing Docker project has an unsupported mount inventory; no container was changed")
    selected = []
    for mount in mounts:
        source, destination = _collision_path(mount.get("source")), _collision_path(mount.get("destination"))
        exists = None
        if source is not None:
            try:
                exists = Path(source).exists()
            except OSError:
                pass
        selected.append({"type": _collision_choice(mount.get("type"), {"bind", "volume", "tmpfs"}),
                         "source": source or "[unavailable]", "destination": destination or "[unavailable]",
                         "source_exists": exists, "appdata": destination in {"/config", "/app/config"}})
    return {"id": row["id"], "name": _collision_name(row.get("name")),
            "status": _collision_choice(row.get("status"), {"created", "restarting", "running", "removing", "paused", "exited", "dead"}),
            "project": _collision_name(row.get("project")), "deployment_id": _collision_uuid(row.get("deployment_id")),
            "stage": _collision_choice(row.get("stage"), {str(s) for s in STAGES}),
            "service": _collision_name(row.get("service")), "mounts": selected}


def _check_existing_appdata(rows):
    """Do not recreate a container over missing or replaced persistent config."""
    message = ("Existing container appdata path is missing, replaced, or unsafe; no paths were recreated. "
               "Leave existing containers running and run ./setup --recover before any recreation.")
    for row in rows:
        mounts = row.get("mounts")
        if not isinstance(mounts, list) or len(mounts) > 256 or any(not isinstance(item, dict) for item in mounts):
            raise SafetyError("Existing Docker project has an unsupported mount inventory; no container was changed")
        for mount in mounts:
            if mount.get("type") != "bind" or mount.get("destination") not in {"/config", "/app/config"}:
                continue
            source = _collision_path(mount.get("source"))
            if source is None:
                raise SafetyError(message)
            try:
                host = no_symlinks(source).stat()
                if not stat.S_ISDIR(host.st_mode):
                    raise SafetyError(message)
                pid = row.get("pid")
                if sys.platform.startswith("linux") and type(pid) is int and 0 < pid < 2**31:
                    # A deleted bind source may have been recreated at its old
                    # name while the running container still holds the old inode.
                    live = os.stat("/proc/" + str(pid) + "/root" + mount["destination"])
                    current = no_symlinks(source).stat()
                    identity = lambda info: (info.st_dev, info.st_ino)
                    if not stat.S_ISDIR(live.st_mode) or identity(host) != identity(live) or identity(host) != identity(current):
                        raise SafetyError(message)
            except (OSError, SafetyError):
                raise SafetyError(message) from None


def project_conflicts(c, stage, runner=run):
    """Inspect only selected fields; return conflicts without reading app data."""
    naming.check_installed_names(c)
    project = naming.project_name(c, stage)
    ids = docker(runner, "ps", "--all", "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.ID}}").stdout.split()
    if not ids:
        return None
    if len(ids) > 256 or any(not re.fullmatch(r"[a-fA-F0-9]{12,64}", value) for value in ids):
        raise SafetyError("Existing Docker project returned an invalid container inventory; no container was changed")
    raw = docker(runner, "inspect", *ids, "--format", _PROJECT_INSPECT).stdout
    if len(raw) > 2 * 1024 * 1024:
        raise SafetyError("Existing Docker project inventory exceeded the safe size limit; no container was changed")
    try:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (ValueError, TypeError):
        raise SafetyError("Existing Docker project returned an unreadable inventory; no container was changed") from None
    if (len(rows) != len(ids) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{12,64}", row["id"]) for row in rows)
            or len({row["id"] for row in rows}) != len(rows)
            or any(sum(row["id"].startswith(identifier) for row in rows) != 1 for identifier in ids)):
        raise SafetyError("Existing Docker project inventory is incomplete or ambiguous; no container was changed")
    conflicts = [row for row in rows if row.get("deployment_id") != c["deployment_id"]
                 or row.get("stage") != str(stage) or row.get("project") != project]
    if not conflicts:
        _check_existing_appdata(rows)
        return None
    details = {"project": project, "requested_deployment_id": c["deployment_id"], "requested_stage": stage,
               "containers": [_collision_container(row) for row in conflicts]}
    details["inventory_digest"] = digest(details)
    return details


def check_container_ownership(c, stage, runner=run):
    details = project_conflicts(c, stage, runner)
    if details is not None:
        raise ProjectCollision(details)


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
    naming.check_installed_names(c)
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
    startup_script = root / "runtime_startup.py"
    atomic_write(startup_script, Path(__file__).with_name("runtime_startup.py").read_text(), replace=startup_script.exists())
    startup_path = stage_path(c, stage) / "startup.json"
    startup = {"schema_version": 1, "deployment_id": c["deployment_id"], "stage": stage,
               "project": naming.project_name(c, stage), "config_digest": digest(c),
               "compose_sha256": hashlib.sha256(no_symlinks(compose_path(c, stage)).read_bytes()).hexdigest(),
               "services": sorted(recipe(c, stage)["services"])}
    write_json(startup_path, startup, replace=startup_path.exists())
    if c["vpn"].get("port_forwarding"):
        source = Path(__file__).resolve().parent.parent / "scripts" / "port_sync.py"
        target = root / "port_sync.py"
        atomic_write(target, source.read_text(), replace=target.exists(), mode=0o444)
    return root, guard_path, saved_config


def _unit_has_owner(path, deployment_id):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024:
        raise SafetyError("The existing systemd unit is not a bounded regular generated file; it was retained")
    try:
        markers = re.findall(r"^# Managed by Samurai Stack: ([^\r\n]+)$", path.read_text(encoding="utf-8"), re.M)
    except (OSError, UnicodeError):
        raise SafetyError("The existing systemd unit could not be read safely; it was retained") from None
    return markers == [deployment_id]


def retire_obsolete_unit(c, stage, runner=run, *, unit_root=Path("/etc/systemd/system"),
                         archive_root=Path("/var/lib/samurai-stack/retired-units"), actions=None):
    """Retire only an obsolete generated unit proven to have zero containers."""
    unit_name = f"samurai-{c['deployment_name']}-{stage}.service"
    path = Path(unit_root) / unit_name
    no_symlinks(path)
    if path.exists() and not _unit_has_owner(path, c["deployment_id"]):
        from .retired_units import retire_conflicting
        result = retire_conflicting(c, stage, runner=runner, unit_root=Path(unit_root), archive_root=Path(archive_root))
        if path.exists() and not _unit_has_owner(path, c["deployment_id"]):
            raise SafetyError("A systemd unit with this name is not owned by this deployment; no runtime files were replaced")
        if result is not None:
            if actions is not None:
                actions.append(result)
            return result
    return None


def install_unit(c, stage, runner=run, *, changed=False, actions=None,
                 unit_root=Path("/etc/systemd/system"), archive_root=Path("/var/lib/samurai-stack/retired-units")):
    # A foreign old unit may be in a restart loop. Stop/archive it, with the
    # dedicated zero-container proof, before overwriting its guard or site file.
    retire_obsolete_unit(c, stage, runner, unit_root=unit_root, archive_root=archive_root, actions=actions)
    root, guard, config = runtime_files(c, stage)
    unit_name = f"samurai-{c['deployment_name']}-{stage}.service"
    path = no_symlinks(Path(unit_root) / unit_name)
    command = " ".join(["/usr/bin/docker", "--context", "default", *compose_args(c, stage)])
    required_mounts = str(root) if stage == 4 else f"{c['storage']['data_root']} {root}"
    # A legitimate storage/VPN delay may last longer than three retries. Keep
    # retrying failures at a bounded interval, with the guard before every
    # attempt. An explicit systemctl stop still stays stopped.
    text = f"# Managed by Samurai Stack: {c['deployment_id']}\n[Unit]\nDescription=Samurai Stack stage {stage}\nRequires=docker.service\nPartOf=docker.service\nAfter=docker.service network-online.target\nWants=network-online.target\nRequiresMountsFor={required_mounts}\nStartLimitIntervalSec=0\n\n[Service]\nType=simple\nUMask=0077\nExecStartPre=/usr/bin/python3 {guard} {config} {stage}\nExecStart={command} up --no-color --abort-on-container-failure --pull never\nExecStartPost=/usr/bin/python3 {root / 'runtime_startup.py'} {config} {stage}\nExecStop={command} stop --timeout 45\nRestart=on-failure\nRestartSec=15\nTimeoutStartSec=180\nTimeoutStopSec=75\n\n[Install]\nWantedBy=multi-user.target\n"
    changed = changed or not path.exists() or path.read_text() != text
    atomic_write(path, text, replace=path.exists(), mode=0o644)
    runner(["systemctl", "daemon-reload"])
    runner(["systemctl", "enable", unit_name])
    try:
        # Clear the exact owned unit's earlier restart-loop rate limit, not all
        # systemd failures on this host. A newly installed unit may be not-found
        # until systemd loads it; start below remains the readiness authority.
        runner(["systemctl", "reset-failed", unit_name])
    except SafetyError:
        loaded = runner(["systemctl", "show", unit_name, "--property=LoadState", "--value"]).stdout.strip()
        if loaded != "not-found":
            raise
    runner(["systemctl", "restart" if changed else "start", unit_name], timeout=200)
    return unit_name


def apply(c, stage, document, accepted, *, runner=run, ready_timeout=180):
    verify_plan(c, stage, document, accepted, runner)
    if os.name == "posix" and os.geteuid() != 0 and (stage != 0 or os.geteuid() != c["puid"] or os.getegid() != c["pgid"]):
        raise SafetyError("Apply requires sudo on the target VM; filesystem and service ownership must be controlled")
    if stage:
        # A regenerated site UUID must not prepare or repopulate the original
        # appdata paths before discovering the still-running old project.
        check_container_ownership(c, stage, runner)
    initialize_install(c)
    with deployment_lock(c):
        # Another accepted apply may have finished between the early review
        # check and this lock acquisition. Revalidate before changing receipts
        # or deployment files, while this invocation owns the lock.
        verify_plan(c, stage, document, accepted, runner)
        if stage:
            check_container_ownership(c, stage, runner)
        recovery_actions = []
        if stage:
            retire_obsolete_unit(c, stage, runner, actions=recovery_actions)
        path = receipt_path(c, stage)
        receipt = {"deployment_id": c["deployment_id"], "stage": stage, "config_digest": digest(c), "started": now(), "status": "applying"}
        if recovery_actions:
            receipt["recovery_actions"] = recovery_actions
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
                if stage == 4:
                    from .management_setup import reserve_fresh
                    reserve_fresh(c, runner=runner)
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
                unit = install_unit(c, stage, runner, changed=changed, actions=recovery_actions)
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
                if recovery_actions:
                    result["recovery_actions"] = recovery_actions
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
