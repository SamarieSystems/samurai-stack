"""Reviewed, additive provisioning on an already installed Proxmox VE host.

The coordinator obtains acceptance of review_hash; apply also serializes itself.
This module never removes a VM, changes host networking, or configures drivers.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import unquote
import uuid


class ProxmoxError(RuntimeError):
    """An actionable prerequisite, ownership, or provisioning failure."""


def default_runner(argv):
    return subprocess.run(argv, text=True, capture_output=True, timeout=900, check=False)


def _call(run, argv, *, json_result=False):
    try:
        result = run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxmoxError(f"Cannot run {argv[0]}; run this stage on the selected Proxmox host: {type(exc).__name__}") from exc
    if result.returncode:
        raise ProxmoxError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed with exit {result.returncode}; inspect its local logs. No destructive rollback was attempted.")
    try:
        return json.loads(result.stdout) if json_result else result.stdout.strip()
    except (ValueError, TypeError) as exc:
        raise ProxmoxError(f"Invalid JSON returned by {argv[0]}") from exc


def _api(run, endpoint, *options):
    return _call(run, ["pvesh", "get", endpoint, *options, "--output-format", "json"], json_result=True)


def inventory(run=default_runner):
    """Inspect only; return a sanitized local-node inventory without credentials."""
    nodes = _api(run, "/cluster/status")
    local = [n for n in nodes if n.get("type") == "node" and n.get("local") == 1]
    if len(local) != 1:
        raise ProxmoxError("Cannot identify exactly one local Proxmox node")
    node = local[0]["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", node):
        raise ProxmoxError("Unexpected local node name")
    machine = _call(run, ["cat", "/etc/machine-id"])
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
        raise ProxmoxError("Host machine-id is missing or invalid")
    status = _api(run, f"/nodes/{node}/status")
    return {
        "node": node, "machine_id": machine.lower(),
        "cpu": status.get("cpuinfo", {}), "memory": status.get("memory", {}),
        "storage": _api(run, f"/nodes/{node}/storage"),
        "network": _api(run, f"/nodes/{node}/network"),
        "vms": _api(run, "/cluster/resources", "--type", "vm"),
        "pci": _api(run, f"/nodes/{node}/hardware/pci", "--pci-class-blacklist", ""),
    }


def _hash(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file_hash(path, algorithm="sha512"):
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path(value, label):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\n\r\x00,"):
        raise ProxmoxError(f"{label} must be an absolute path without traversal, commas or control characters")
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ProxmoxError(f"{label} must not traverse symlinks")
    return path


def _validate(config):
    c = dict(config)
    try:
        c["deployment_id"] = str(uuid.UUID(c["deployment_id"]))
        for key in ("node", "storage", "bridge"):
            if not isinstance(c[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", c[key]):
                raise ValueError(key)
        c["image_sha512"] = c["image_sha512"].lower()
        if not re.fullmatch(r"[0-9a-f]{128}", c["image_sha512"]):
            raise ValueError("image_sha512")
        for key in ("image_path", "ssh_public_key", "state_dir"):
            c[key] = str(_path(c[key], key))
        if len(Path(c["state_dir"]).parts) < 3:
            raise ValueError("state_dir must be a dedicated directory")
        c.setdefault("admin_user", "stackadmin")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", c["admin_user"]) or c["admin_user"] == "root":
            raise ValueError("admin_user")
        vms = []
        for source in c["vms"]:
            v = dict(source)
            for key, low, high in [("id", 100, 999999999), ("cores", 1, 256), ("memory_mib", 512, 1048576), ("disk_gib", 8, 65536)]:
                if type(v[key]) is not int or not low <= v[key] <= high:
                    raise ValueError(key)
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", v["name"]):
                raise ValueError("VM name")
            v.setdefault("ipconfig", "ip=dhcp")
            if v["ipconfig"] != "ip=dhcp":
                fields = dict(item.split("=", 1) for item in v["ipconfig"].split(","))
                if set(fields) != {"ip", "gw"}:
                    raise ValueError("ipconfig requires ip and gw, or ip=dhcp")
                interface = ipaddress.IPv4Interface(fields["ip"])
                gateway = ipaddress.IPv4Address(fields["gw"])
                if gateway not in interface.network:
                    raise ValueError("gateway is outside the VM subnet")
                v["ipconfig"] = f"ip={interface},gw={gateway}"
            v.setdefault("start", True)
            v.setdefault("onboot", True)
            if type(v["start"]) is not bool or type(v["onboot"]) is not bool:
                raise ValueError("start and onboot must be booleans")
            if v.get("gpu_pci"):
                raise ValueError("GPU passthrough is a manual reviewed prerequisite in this release; omit gpu_pci and see docs/PROXMOX.md")
            vms.append(v)
        if not vms or len({v["id"] for v in vms}) != len(vms) or len({v["name"] for v in vms}) != len(vms):
            raise ValueError("vms must contain distinct VM IDs and names")
        c["vms"] = vms
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProxmoxError(f"Invalid Proxmox configuration: {exc}") from exc
    return c


def _key_text(text):
    text = text.strip()
    fields = text.split()
    if len(fields) < 2 or fields[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"} or "\n" in text or "\r" in text:
        raise ProxmoxError("ssh_public_key must contain one plain OpenSSH public key, not a private key or authorized_keys options")
    try:
        raw = base64.b64decode(fields[1], validate=True)
        length = int.from_bytes(raw[:4], "big")
        if raw[4:4 + length].decode() != fields[0] or len(raw) < length + 12:
            raise ValueError("key encoding")
    except (ValueError, UnicodeError) as exc:
        raise ProxmoxError("Invalid SSH public key encoding") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _key(path):
    if not Path(path).is_file():
        raise ProxmoxError("ssh_public_key must be a regular file")
    return _key_text(Path(path).read_text())


def _tags(c, v, key_hash):
    owner = "samurai-" + uuid.UUID(c["deployment_id"]).hex
    spec = {k: c[k] for k in ["node", "storage", "bridge", "image_sha512", "admin_user"]}
    return owner, "sspec-" + _hash({"vm": v, "base": spec, "ssh_key": key_hash})[:20]


def _vm_config(c, v, run):
    pending = _api(run, f"/nodes/{c['node']}/qemu/{v['id']}/pending")
    if any("pending" in item or item.get("delete") for item in pending):
        raise ProxmoxError(f"VM {v['id']} has pending Proxmox changes; review them manually before proceeding")
    return _api(run, f"/nodes/{c['node']}/qemu/{v['id']}/config", "--current", "1")


def _owned(c, v, current, key_hash):
    tags = set(current.get("tags", "").split(";"))
    if not set(_tags(c, v, key_hash)).issubset(tags):
        raise ProxmoxError(f"VM {v['id']} exists without this deployment's matching ownership and specification tags; refusing adoption")
    for field, expected in {"name": v["name"], "cores": v["cores"], "memory": v["memory_mib"], "sockets": 1, "ciuser": c["admin_user"], "ciupgrade": 0, "scsihw": "virtio-scsi-pci", "cpu": "host", "serial0": "socket", "vga": "serial0", "onboot": int(v["onboot"])}.items():
        if str(current.get(field)) != str(expected):
            raise ProxmoxError(f"VM {v['id']} has configuration drift in {field}; inspect it instead of overwriting")
    net = current.get("net0", "")
    if not net.startswith("virtio=") or f"bridge={c['bridge']}" not in net.split(",") or "firewall=1" not in net.split(","):
        raise ProxmoxError(f"VM {v['id']} network differs from the reviewed plan")
    ipfields = lambda text: dict(item.split("=", 1) for item in text.split(",") if "=" in item)
    if ipfields(current.get("ipconfig0", "")) != ipfields(v["ipconfig"]):
        raise ProxmoxError(f"VM {v['id']} cloud-init networking differs from the reviewed plan")
    if _key_text(unquote(current.get("sshkeys", ""))) != key_hash:
        raise ProxmoxError(f"VM {v['id']} SSH public key differs from the reviewed plan")
    for field in ("scsi0", "ide2"):
        if field in current:
            volume = current[field].split(",", 1)[0]
            if not re.fullmatch(re.escape(c["storage"]) + rf":(?:{v['id']}/)?vm-{v['id']}-[A-Za-z0-9_.-]+", volume):
                raise ProxmoxError(f"VM {v['id']} {field} is not an owned volume on the selected storage")
    if any(key.startswith("unused") for key in current):
        raise ProxmoxError(f"VM {v['id']} has unused disks from an incomplete import; review them manually before retrying")
    receipt = Path(c["state_dir"]) / c["deployment_id"] / f"vm-{v['id']}.json"
    _path(str(receipt), "receipt path")
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous.get("state") == "defined" and current.get("boot") != "order=scsi0":
            raise ProxmoxError(f"VM {v['id']} boot order changed after provisioning; refusing to overwrite it")


def _commands(c, v, key_hash):
    owner, spec = _tags(c, v, key_hash)
    return {
        "create": ["qm", "create", str(v["id"]), "--name", v["name"], "--memory", str(v["memory_mib"]), "--cores", str(v["cores"]), "--sockets", "1", "--cpu", "host", "--machine", "q35", "--onboot", str(int(v["onboot"])), "--net0", f"virtio,bridge={c['bridge']},firewall=1", "--scsihw", "virtio-scsi-pci", "--serial0", "socket", "--vga", "serial0", "--ciuser", c["admin_user"], "--ciupgrade", "0", "--sshkeys", c["ssh_public_key"], "--ipconfig0", v["ipconfig"], "--agent", "enabled=1", "--tags", owner + ";" + spec],
        "root_disk": ["qm", "set", str(v["id"]), "--scsi0", f"{c['storage']}:0,import-from={c['image_path']},discard=on"],
        "cloud_init": ["qm", "set", str(v["id"]), "--ide2", f"{c['storage']}:cloudinit"],
        "resize": ["qm", "resize", str(v["id"]), "scsi0", f"{v['disk_gib']}G"],
        "boot": ["qm", "set", str(v["id"]), "--boot", "order=scsi0"],
        "start": ["qm", "start", str(v["id"])],
    }


def plan(config, run=default_runner):
    c = _validate(config)
    if not Path(c["image_path"]).is_file() or _file_hash(c["image_path"]) != c["image_sha512"]:
        raise ProxmoxError("Local image missing or SHA512 differs from the pinned value")
    image = _call(run, ["qemu-img", "info", "--output=json", c["image_path"]], json_result=True)
    def external(data):
        if isinstance(data, dict):
            return any((key in {"backing-filename", "full-backing-filename", "data-file", "encrypted", "encrypt"} and bool(value)) or external(value) for key, value in data.items())
        return isinstance(data, list) and any(external(item) for item in data)
    if image.get("format") != "qcow2" or external(image):
        raise ProxmoxError("Use a standalone, unencrypted qcow2 cloud image with no backing file or external data file")
    if not isinstance(image.get("virtual-size"), int) or image["virtual-size"] <= 0:
        raise ProxmoxError("Invalid image virtual size")
    key_hash = _key(c["ssh_public_key"])
    host = inventory(run)
    if host["node"] != c["node"]:
        raise ProxmoxError("Selected node is not the local node; run this stage on the intended host")
    storage = next((s for s in host["storage"] if s.get("storage") == c["storage"]), None)
    if not storage or not storage.get("active") or "images" not in storage.get("content", "").split(","):
        raise ProxmoxError("Select existing active Proxmox storage supporting images")
    if not any(n.get("iface") == c["bridge"] and n.get("type") == "bridge" and n.get("active") for n in host["network"]):
        raise ProxmoxError("Select an existing active Linux bridge; no bridge changes are automated")
    by_id = {int(v["vmid"]): v for v in host["vms"]}
    needed = 0
    for v in c["vms"]:
        if v["disk_gib"] * 2**30 < image["virtual-size"]:
            raise ProxmoxError(f"VM {v['id']} disk would shrink the source image")
        if v["id"] in by_id:
            existing = by_id[v["id"]]
            if existing.get("node") != c["node"] or existing.get("type") != "qemu":
                raise ProxmoxError(f"VM ID {v['id']} is already used elsewhere or by a container")
            _owned(c, v, _vm_config(c, v, run), key_hash)
        else:
            needed += v["disk_gib"] * 2**30
    if int(storage.get("avail", 0)) < needed:
        raise ProxmoxError("Not enough free storage for the requested new VM disk capacities")
    total = int(host["memory"].get("total", 0))
    reserve = max(2 * 2**30, total // 10)
    ids = {v["id"] for v in c["vms"]}
    allocated = sum(int(v.get("maxmem", 0)) for v in host["vms"] if v.get("node") == c["node"] and int(v["vmid"]) not in ids)
    if allocated + sum(v["memory_mib"] * 2**20 for v in c["vms"]) + reserve > total:
        raise ProxmoxError("Requested VM RAM plus existing allocations leaves insufficient host reserve")
    document = {
        "schema": 1, "kind": "proxmox", "config_digest": _hash(c),
        "host": {"node": host["node"], "machine_id": host["machine_id"]},
        "image_sha512": c["image_sha512"], "ssh_key_fingerprint": key_hash,
        "vms": [{"id": v["id"], "name": v["name"], "commands": _commands(c, v, key_hash), "start": v["start"]} for v in c["vms"]],
        "limits": ["No Proxmox installation, host network/boot changes, storage formatting, VM deletion or host GPU binding.", "Guest-agent installation and guest cloud-init completion must be verified after guest bootstrap."]
    }
    document["review_hash"] = _hash(document)
    return document


def _save(path, value):
    _path(str(path), "receipt path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not path.is_file():
        raise ProxmoxError("Receipt target is not a regular file")
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _disk_size(config):
    match = re.search(r"(?:^|,)size=([0-9.]+)([KMGT]?)B?(?:,|$)", config)
    if not match:
        raise ProxmoxError("Cannot determine the existing root disk size; refusing to resize blindly")
    return int(float(match[1]) * 1024 ** {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[match[2]])


def _apply_locked(config, plan_document, run=default_runner):
    """Apply only an accepted unchanged plan; preserve evidence on every failure."""
    supplied = dict(plan_document)
    digest = supplied.pop("review_hash", None)
    if digest != _hash(supplied):
        raise ProxmoxError("Reviewed plan integrity check failed")
    fresh = plan(config, run)
    if fresh != plan_document:
        raise ProxmoxError("Host, image, SSH key or configuration differs from the reviewed plan; generate a new plan")
    c = _validate(config)
    key_hash = fresh["ssh_key_fingerprint"]
    records = []
    for v, vm_plan in zip(c["vms"], fresh["vms"]):
        receipt_path = Path(c["state_dir"]) / c["deployment_id"] / f"vm-{v['id']}.json"
        record = {"vm_id": v["id"], "review_hash": digest, "state": "applying", "steps": []}
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            if previous.get("vm_id") != v["id"]:
                raise ProxmoxError("Receipt identity mismatch")
            record["previous_attempt"] = {k: previous.get(k) for k in ["state", "steps", "failed_step"]}
        _save(receipt_path, record)
        step = "preflight"
        try:
            vms = _api(run, "/cluster/resources", "--type", "vm")
            exists = any(int(x["vmid"]) == v["id"] for x in vms)
            if not exists:
                step = "create"
                _call(run, vm_plan["commands"][step])
            current = _vm_config(c, v, run)
            _owned(c, v, current, key_hash)
            for step, key in [("root_disk", "scsi0"), ("cloud_init", "ide2")]:
                if key not in current:
                    if any(k.startswith("unused") for k in current):
                        raise ProxmoxError("Incomplete disk import requires manual inspection; no disks removed")
                    record["attempting"] = step
                    _save(receipt_path, record)
                    _call(run, vm_plan["commands"][step])
                    current = _vm_config(c, v, run)
                    _owned(c, v, current, key_hash)
                    if key not in current:
                        raise ProxmoxError(f"VM {v['id']} did not retain {key}")
                record["steps"].append(step)
                _save(receipt_path, record)
            step = "resize"
            size = _disk_size(current["scsi0"])
            wanted = v["disk_gib"] * 2**30
            if size > wanted:
                raise ProxmoxError("Existing disk is larger than requested; shrinking is never performed")
            if size < wanted:
                _call(run, vm_plan["commands"][step])
            record["steps"].append(step)
            step = "boot"
            if current.get("boot") != "order=scsi0":
                _call(run, vm_plan["commands"][step])
            record["steps"].append(step)
            step = "start"
            status = _api(run, f"/nodes/{c['node']}/qemu/{v['id']}/status/current")
            if v["start"] and status.get("status") == "stopped":
                _call(run, vm_plan["commands"][step])
                record["steps"].append(step)
            elif status.get("status") not in {"stopped", "running"}:
                raise ProxmoxError("Unexpected guest state; no forced power action attempted")
            current = _vm_config(c, v, run)
            _owned(c, v, current, key_hash)
            if _disk_size(current["scsi0"]) != wanted or current.get("boot") != "order=scsi0":
                raise ProxmoxError("Final disk/boot readback differs from plan")
            record.pop("attempting", None)
            record["state"] = "defined"
            _save(receipt_path, record)
            records.append(record)
        except Exception as exc:
            record.update(state="failed", failed_step=step, error=type(exc).__name__)
            _save(receipt_path, record)
            raise ProxmoxError(f"VM {v['id']} failed at {step}. Partial VM/disks were preserved; receipt: {receipt_path}. {exc}") from exc
    return {"state": "defined", "vms": records, "next": "Run guest bootstrap, then proxmox verify. Definition success is not application readiness."}


@contextmanager
def _apply_lock(c):
    path = Path(c["state_dir"]) / c["deployment_id"] / "apply.lock"
    _path(str(path), "apply lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ProxmoxError(f"Another apply is active or interrupted. Inspect {path} and its PID; stale locks are never removed automatically") from exc
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "deployment_id": c["deployment_id"], "token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        # Delete only this invocation's verified transient lock, never VM data.
        if path.is_file() and not path.is_symlink():
            try:
                if json.loads(path.read_text()).get("token") == token:
                    path.unlink()
            except (OSError, ValueError):
                pass  # Keep ambiguous state for deliberate operator recovery.


def apply(config, plan_document, run=default_runner):
    """Serialize apply internally; an interrupted owner requires explicit recovery."""
    c = _validate(config)
    supplied = dict(plan_document)
    accepted = supplied.pop("review_hash", None)
    if accepted != _hash(supplied):
        raise ProxmoxError("Reviewed plan integrity check failed")
    with _apply_lock(c):
        return _apply_locked(c, plan_document, run)


def verify(config, run=default_runner):
    c = _validate(config)
    key_hash = _key(c["ssh_public_key"])
    host = inventory(run)
    if host["node"] != c["node"]:
        raise ProxmoxError("Wrong local node")
    rows = []
    for v in c["vms"]:
        current = _vm_config(c, v, run)
        _owned(c, v, current, key_hash)
        if not all(k in current for k in ["scsi0", "ide2"]) or current.get("boot") != "order=scsi0" or _disk_size(current["scsi0"]) != v["disk_gib"] * 2**30:
            raise ProxmoxError(f"VM {v['id']} disks or boot order differ from the plan")
        status = _api(run, f"/nodes/{c['node']}/qemu/{v['id']}/status/current")
        ready = status.get("status") == "running"
        agent = False
        if ready:
            try:
                _call(run, ["qm", "agent", str(v["id"]), "ping"])
                agent = True
            except ProxmoxError:
                pass
        rows.append({"id": v["id"], "definition_ok": True, "running": ready, "guest_agent_reachable": agent, "guest_bootstrap_verified": False})
    return {"state": "definitions_verified", "vms": rows, "ready": False, "next": "SSH into each guest and verify cloud-init status (exit 0 and no errors), storage identity and software prerequisites. Agent reachability alone is insufficient."}
