"""Installed standalone capacity watcher. Never delete, resume or alter VPNs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid

MAX_BYTES = 256 * 1024
DOWNLOAD_STATES = frozenset({"downloading", "stalledDL", "queuedDL", "forcedDL", "metaDL", "forcedMetaDL", "allocating", "checkingDL"})
INSPECT = ('{"id":{{json .Id}},"labels":{{json .Config.Labels}},"network":{{json .HostConfig.NetworkMode}},'
           '"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}')


class GuardError(RuntimeError):
    pass


def clean(path):
    path = Path(path)
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise GuardError("Capacity policy paths cannot contain symbolic links")
    return path


def owner(info):
    if os.name == "posix" and info.st_uid != 0:
        raise GuardError("Capacity control files must belong to root")


def read(path, *, private=True, root_owned=True):
    path = clean(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise GuardError("Capacity control input must be a bounded regular file without hard links")
    if root_owned:
        owner(info)
    if private and os.name == "posix" and info.st_mode & 0o077:
        raise GuardError("Capacity control input must be private to root")
    if not private and root_owned and os.name == "posix" and info.st_mode & 0o022:
        raise GuardError("Owned capacity control input cannot be writable by other users")
    value = path.read_bytes()
    if len(value) > MAX_BYTES:
        raise GuardError("Capacity input exceeded its safe bound")
    return value.decode("utf-8-sig")


def host_id():
    value = Path("/etc/machine-id").read_bytes()
    if not value.strip() or len(value) > 4096:
        raise GuardError("Cannot identify the capacity policy host")
    return hashlib.sha256(value).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_policy(path):
    value = json.loads(read(path))
    c = value.get("config", {})
    identifier = c.get("deployment_id")
    try:
        valid_id = str(uuid.UUID(identifier)) == identifier
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    if (not valid_id or value.get("schema_version") != 1 or value.get("deployment_id") != identifier
            or value.get("host_id") != host_id() or value.get("config_digest") != digest(c)
            or type(value.get("qbit_guard")) is not bool):
        raise GuardError("Capacity policy identity is invalid or belongs to another host")
    for key in ("data_reserve_bytes", "app_reserve_bytes"):
        if type(value.get(key)) is not int or not 1 <= value[key] <= 2 ** 50:
            raise GuardError("Capacity reserves must be bounded positive byte counts")
    for key in ("install_root",):
        text = c.get(key)
        if not isinstance(text, str) or not text.startswith("/") or ".." in Path(text).parts or re.search(r"[\s$`\\]", text):
            raise GuardError("Capacity policy has an unsafe application path")
    if Path(path).absolute() != (Path(c["install_root"]) / "state" / "capacity-policy.json").absolute():
        raise GuardError("Capacity policy is outside its installation control directory")
    directory = clean(Path(path).parent).stat()
    owner(directory)
    if not stat.S_ISDIR(directory.st_mode) or os.name == "posix" and directory.st_mode & 0o077:
        raise GuardError("Capacity policy state directory must be private to root")
    marker = json.loads(read(Path(c["install_root"]) / ".samurai-install.json"))
    if marker.get("deployment_id") != identifier:
        raise GuardError("Capacity policy does not own this application directory")
    return value


def call(runner, argv):
    try:
        result = runner(argv, timeout=20)
        if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError()
        return result.stdout.strip()
    except Exception:
        raise GuardError("Capacity guard could not verify a local prerequisite; no unproved endpoint was changed") from None


def local_run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, **kwargs)


def stable_mount(row):
    """Canonical local block filesystems by UUID; keep subvolume/export identity."""
    result = {key: row[key] for key in ('target', 'source', 'fstype')}
    if result['fstype'] in {'ext4', 'xfs', 'btrfs'} and result['source'].startswith('/dev/'):
        identifier = row.get('uuid')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', identifier):
            raise ValueError('local block filesystem UUID unavailable')
        suffix = result['source'][result['source'].find('['):] if '[' in result['source'] else ''
        result['source'] = 'UUID=' + identifier + suffix
    return result


def capacities(config, runner=local_run):
    result = {}
    for kind, text in (("data", config["storage"]["data_root"]), ("app", config["install_root"])):
        path = clean(text)
        rows = json.loads(call(runner, ["findmnt", "--json", "--target", str(path), "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,UUID"]))["filesystems"]
        if len(rows) != 1 or "rw" not in rows[0]["options"].split(","):
            raise GuardError("Capacity guard cannot prove a unique writable backing filesystem")
        mount = {k: rows[0][k] for k in ("target", "source", "fstype")}
        if (kind == "data" and config["storage"]["expected_mount"]["source"].startswith("UUID=")
                or kind == "app" and rows[0].get("uuid")):
            try:
                mount = stable_mount(rows[0])
            except (KeyError, TypeError, ValueError):
                raise GuardError("Capacity filesystem UUID cannot be verified; no local fallback") from None
        if kind == "data":
            if mount != config["storage"]["expected_mount"]:
                raise GuardError("Capacity data mount differs; no local fallback is permitted")
            marker = json.loads(read(path / ".samurai-storage.json", private=False, root_owned=False))
            if marker.get("deployment_id") != config["deployment_id"] or marker.get("expected_mount") != mount:
                raise GuardError("Capacity data mount ownership differs")
        elif mount["fstype"] not in {"ext4", "xfs", "btrfs", "zfs"}:
            raise GuardError("Capacity application files must remain on local Linux storage")
        space = os.statvfs(path)
        result[kind] = {"mount": mount, "available_bytes": space.f_bavail * space.f_frsize,
                        "size_bytes": space.f_blocks * space.f_frsize}
    return result


def prove_service(config, service, stage, port, runner=local_run):
    docker = ["docker", "--context", "default"]
    if not call(runner, [*docker, "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"]).startswith("unix://"):
        raise GuardError("Capacity control requires the local default Docker engine")
    project = config.get("compose_projects", {}).get(str(stage), config["deployment_name"] + "-" + str(stage))
    ids = call(runner, [*docker, "ps", "--filter", "status=running", "--filter", "label=com.docker.compose.project=" + project,
                       "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"]).split()
    if len(ids) != 1 or not re.fullmatch(r"[a-fA-F0-9]{12,64}", ids[0]):
        raise GuardError("Capacity endpoint is not one unique running owned service")
    row = json.loads(call(runner, [*docker, "inspect", ids[0], "--format", INSPECT]))
    labels = row.get("labels", {})
    if (not isinstance(row.get("id"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", row["id"])
            or not row["id"].startswith(ids[0]) or labels.get("systems.samarie.deployment") != config["deployment_id"]
            or labels.get("systems.samarie.stage") != str(stage) or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != service):
        raise GuardError("Capacity endpoint container ownership differs")
    destination = "/gluetun" if service == "gluetun" else "/config"
    mounts = row.get("mounts")
    chosen = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == destination] if isinstance(mounts, list) else []
    if (len(chosen) != 1 or chosen[0].get("Type") != "bind"
            or chosen[0].get("Source") != str(Path(config["install_root"]) / "appdata" / service)):
        raise GuardError("Capacity target does not use its owned persistent application directory")
    if port is not None:
        address = config["hosts"]["torrent" if stage == 3 else "media"]
        if address != config["bind_address"] or not ipaddress.ip_address(address).is_private:
            raise GuardError("Capacity control is restricted to services on this private guest")
        interfaces = json.loads(call(runner, ["ip", "-j", "address", "show"]))
        if address not in {entry.get("local") for row in interfaces for entry in row.get("addr_info", [])}:
            raise GuardError("Capacity service address is not on this guest")
        ports = row.get("ports", {}).get(str(port) + "/tcp", [])
        if not ports or any(item.get("HostIp") != address or item.get("HostPort") != str(port) for item in ports):
            raise GuardError("Capacity API binding differs from the owned private endpoint")
    return row


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GuardError("Capacity API redirects are refused")


class Transport:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                 urllib.request.HTTPCookieProcessor(self.cookies))

    def __call__(self, method, url, *, headers=None, form=None, timeout=15):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
                text = raw.decode("utf-8")
            try:
                return json.loads(text)
            except ValueError:
                return text.strip()
        except Exception:
            raise GuardError("Capacity API request failed; inspect the service locally") from None


def qbit_backend(config, runner=local_run, request=None, credentials=None):
    qbit = prove_service(config, "qbittorrent", 3, None, runner)
    vpn = prove_service(config, "gluetun", 3, 8081, runner)
    if qbit.get("network") != "container:" + vpn["id"]:
        raise GuardError("Capacity qBittorrent target does not share its exact owned VPN namespace")
    call(runner, ["docker", "--context", "default", "exec", vpn["id"], "ip", "address", "show", "dev", "tun0"])
    resolve = credentials or (lambda name: read(Path(config["install_root"]) / "secrets" / name).rstrip("\r\n"))
    fresh_session = request is None
    request = request or Transport()
    base = "http://" + config["hosts"]["torrent"] + ":8081"
    headers = {"Referer": base + "/", "Origin": base}
    login = request("POST", base + "/api/v2/auth/login", headers=headers,
                    form={"username": resolve("qbit_username"), "password": resolve("qbit_password")})
    # Older qBit returns 'Ok.'; current releases return HTTP 204 with an SID
    # cookie and an empty body. Neither body alone proves authentication.
    if (login not in ("Ok.", "") or fresh_session and
            not any(cookie.name in {"SID", "QBT_SID_8081"} and cookie.value for cookie in request.cookies)):
        raise GuardError("Capacity qBittorrent authentication was not confirmed")
    def api(path, method="GET", form=None):
        return request(method, base + "/api/v2/" + path, headers=headers, form=form)
    prefs = api("app/preferences")
    if not isinstance(prefs, dict) or prefs.get("current_network_interface") != "tun0":
        raise GuardError("Capacity control refuses a qBittorrent instance not bound to tun0")
    if (prefs.get("bypass_local_auth") is not False or
            prefs.get("bypass_auth_subnet_whitelist_enabled") is not False):
        raise GuardError("Capacity control requires qBittorrent authentication bypasses to remain disabled")
    if config["vpn"].get("port_forwarding"):
        forwarded = call(runner, ["docker", "--context", "default", "exec", vpn["id"], "cat", "/gluetun/forwarded_port"])
        if not forwarded.isdigit() or not 1 <= int(forwarded) <= 65535 or prefs.get("listen_port") != int(forwarded):
            raise GuardError("Capacity target has an unverified forwarded listening port")
    return api


def incomplete_hashes(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise GuardError("Capacity torrent inventory is invalid or too large")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise GuardError("Capacity torrent inventory is invalid")
        if row.get("state") not in DOWNLOAD_STATES:
            continue
        progress, left, identifier = row.get("progress"), row.get("amount_left"), row.get("hash")
        if (type(progress) not in {float, int} or not 0 <= progress <= 1 or type(left) is not int or left < 0
                or not isinstance(identifier, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", identifier)):
            raise GuardError("Capacity download state is incomplete; no hashes were stopped")
        if progress < 1 or left > 0:
            result.append(identifier.lower())
    if len(set(result)) != len(result):
        raise GuardError("Capacity torrent inventory contains duplicate hashes")
    return result


def save_status(path, value):
    path = clean(path)
    if path.exists():
        read(path)
    fd, temporary = tempfile.mkstemp(prefix=".capacity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # Only this invocation's private unpublished status file.


def run_policy(path, *, runner=local_run, request=None, measure=None):
    policy = load_policy(path)
    c = policy["config"]
    status_path = Path(c["install_root"]) / "state" / "capacity-guard-status.json"
    previous = json.loads(read(status_path)) if status_path.exists() else {}
    if previous and previous.get("deployment_id") != c["deployment_id"]:
        raise GuardError("Capacity status belongs to another installation")
    tracked = previous.get("paused_hashes", [])
    if (not isinstance(tracked, list) or len(tracked) > 1024
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value) for value in tracked)):
        raise GuardError("Capacity status has an invalid bounded diagnostic hash list")
    if not policy["qbit_guard"]:
        return {"status": "disabled", "torrents_stopped": 0}
    space = (measure or capacities)(c, runner)
    if space["app"]["mount"] != policy["app_mount"]:
        raise GuardError("Capacity application mount identity changed")
    low = [kind for kind in ("data", "app") if space[kind]["available_bytes"] < policy[kind + "_reserve_bytes"]]
    record = {"schema_version": 1, "deployment_id": c["deployment_id"], "checked": datetime.now(timezone.utc).isoformat(),
              "low_filesystems": low, "capacity": space, "paused_hashes": tracked,
              "status": "low_space" if low else "space_available", "automatic_resume": False}
    if low:
        api = qbit_backend(c, runner, request)
        hashes = incomplete_hashes(api("torrents/info"))
        version = api("app/version")
        match = re.match(r"v?(\d+)\.", version) if isinstance(version, str) else None
        if not match:
            raise GuardError("Capacity guard cannot select a supported qBittorrent stop API")
        # Do not stop a torrent that completed or was manually paused while
        # checking the client version. The API has no atomic conditional stop;
        # this immediate readback keeps the control set as narrow as possible.
        eligible = set(incomplete_hashes(api("torrents/info")))
        hashes = [value for value in hashes if value in eligible]
        # Write intent before stopping. The journal is diagnostic only and is
        # never authority for automatic resume or for deleting torrent data.
        record["paused_hashes"] = sorted(set(record["paused_hashes"]) | set(hashes))[:1024]
        save_status(status_path, record)
        if hashes:
            api("torrents/stop" if int(match[1]) >= 5 else "torrents/pause", "POST", {"hashes": "|".join(hashes)})
            remaining = {row.get("hash", "").lower() for row in api("torrents/info") if row.get("state") in DOWNLOAD_STATES}
            if remaining.intersection(hashes):
                raise GuardError("Capacity guard stop readback did not confirm all selected downloads stopped")
    else:
        save_status(status_path, record)
    return {"status": record["status"], "low_filesystems": low,
            "torrents_stopped": len(hashes) if low else 0, "automatic_resume": False,
            "note": "Capacity reserves are pause thresholds, not filesystem quotas. Resume deliberately after freeing space."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy")
    args = parser.parse_args(argv)
    try:
        if not sys_linux_root():
            raise GuardError("Run the capacity watcher as root inside the Linux guest")
        result = run_policy(args.policy)
        print(json.dumps(result, sort_keys=True))
        return 0
    except GuardError as exc:
        print("Capacity guard blocked: " + str(exc) + ". No automatic resume or deletion was attempted.", file=__import__("sys").stderr)
        return 1
    except Exception:
        print("Capacity guard blocked: ownership, credentials, storage or API state could not be verified. No automatic resume or deletion was attempted.", file=__import__("sys").stderr)
        return 1


def sys_linux_root():
    return __import__("sys").platform.startswith("linux") and os.geteuid() == 0 and not Path("/etc/pve").exists()


if __name__ == "__main__":
    raise SystemExit(main())
