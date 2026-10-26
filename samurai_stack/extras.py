"""Bounded defaults for optional services on a shared, CPU-only media guest.

Photo limits and storage-floor checks remain independent of the core services.
"""
from __future__ import annotations

from .safety import SafetyError


def options(config, role):
    value = config.get("extras", {}).get(role, {})
    if not isinstance(value, dict):
        raise SafetyError("Optional service settings must be JSON objects")
    return value


def validate(config):
    value = config.get("extras", {})
    if not isinstance(value, dict) or set(value) - {"photos", "music"}:
        raise SafetyError("extras supports only photos and music settings")
    photos, music = options(config, "photos"), options(config, "music")
    if set(photos) - {"machine_learning", "data_reserve_gib", "app_reserve_gib", "user_quota_gib"}:
        raise SafetyError("Unknown photo resource setting")
    if type(photos.get("machine_learning", True)) is not bool:
        raise SafetyError("photos.machine_learning must be a JSON boolean")
    for key, default, maximum in (("data_reserve_gib", 50, 100000), ("app_reserve_gib", 20, 100000),
                                  ("user_quota_gib", 2, 100000)):
        if type(photos.get(key, default)) is not int or not 1 <= photos.get(key, default) <= maximum:
            raise SafetyError("Photo quotas and free-space floors must be positive bounded GiB counts")
    if set(music) - {"data_reserve_gib", "app_reserve_gib"}:
        raise SafetyError("Unknown music resource setting")
    for key, default in (("data_reserve_gib", 50), ("app_reserve_gib", 20)):
        if type(music.get(key, default)) is not int or not 1 <= music.get(key, default) <= 100000:
            raise SafetyError("Music free-space floors must be positive bounded GiB counts")


def limited(service, memory, cpus):
    """Use standalone Compose cgroup limits, not Swarm-only reservations."""
    service.update(mem_limit=memory, memswap_limit=memory, cpus=str(cpus), pids_limit=256)
    return service


def photo_policy(config):
    """Supported native Immich system-config fields; merge with native defaults."""
    enabled = options(config, "photos").get("machine_learning", True)
    return {"job": {name: {"concurrency": 1} for name in (
        "backgroundTask", "faceDetection", "library", "metadataExtraction", "migration",
        "notifications", "ocr", "search", "sidecar", "smartSearch", "thumbnailGeneration", "videoConversion",
        "workflow", "editor", "integrityCheck")},
        "ffmpeg": {"threads": 1},
        "machineLearning": {"enabled": enabled, "clip": {"enabled": enabled},
                            "facialRecognition": {"enabled": enabled}, "ocr": {"enabled": enabled}}}


def free_space(config, role, *, usage=None):
    """Read-only floor proof shared by enrollment and maintenance tooling."""
    if usage is None:
        from shutil import disk_usage
        usage = disk_usage
    settings = options(config, role)
    result = {}
    for key, path, default in (("data", config["storage"]["data_root"], 50),
                               ("app", config["install_root"], 20)):
        floor = settings.get(key + "_reserve_gib", default) * 1024 ** 3
        available = usage(path).free
        result[key] = {"free_bytes": available, "reserve_bytes": floor}
        if available <= floor:
            raise SafetyError("Optional service free-space floor reached; existing media is retained")
    return result


def install_capacity_guard(config, stage, *, runner=None, unit_root=None):
    """Add only an owned extras stop timer, retaining existing core policies."""
    import hashlib
    import json
    from pathlib import Path
    from .safety import run, atomic_write, write_json, no_symlinks, private_read
    runner = runner or run
    unit_root = Path(unit_root or "/etc/systemd/system")
    root = Path(config["install_root"])
    script = no_symlinks(root / "extras_capacity_guard.py")
    source = Path(__file__).with_name("extras_capacity_guard.py").read_text()
    if script.exists() and script.read_text() != source:
        raise SafetyError("Existing optional capacity source differs; review its owned upgrade before replacing it")
    atomic_write(script, source, replace=script.exists())
    policy_path = root / "state" / "extras-capacity-policy.json"
    previous = json.loads(private_read(policy_path)) if policy_path.exists() else {}
    if previous and previous.get("config", {}).get("deployment_id") != config["deployment_id"]:
        raise SafetyError("Optional capacity policy belongs to another installation")
    stages = sorted(set(previous.get("stages", [])) | {stage})
    write_json(policy_path, {"schema_version": 1, "config": config, "stages": stages,
                            "host_id": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()},
               replace=policy_path.exists())
    name = "samurai-" + config["deployment_name"] + "-extras-capacity-guard"
    marker = "# Managed by Samurai Stack: " + config["deployment_id"] + "\n"
    documents = {
        "service": marker + "[Unit]\nDescription=Optional media/photo capacity stop guard\nAfter=docker.service\n\n[Service]\nType=oneshot\nUMask=0077\nExecStart=/usr/bin/python3 " + str(script) + " " + str(policy_path) + "\nTimeoutStartSec=150\n",
        "timer": marker + "[Unit]\nDescription=Check optional media/photo free-space floors\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=1min\nAccuracySec=10s\n\n[Install]\nWantedBy=timers.target\n"}
    for suffix, content in documents.items():
        target = no_symlinks(unit_root / (name + "." + suffix))
        if target.exists() and target.read_text() != content:
            raise SafetyError("Existing optional capacity unit differs; it was retained")
        atomic_write(target, content, replace=target.exists(), mode=0o644)
    runner(["systemctl", "daemon-reload"])
    runner(["systemctl", "enable", "--now", name + ".timer"])
    return name + ".timer"


def prepare_fresh(config, stage):
    """Record empty photo database roots before first boot, never reset them."""
    import json
    from pathlib import Path
    from .safety import no_symlinks, private_read, write_json
    if stage != 5:
        raise SafetyError("Fresh optional enrollment supports photos only")
    root = Path(config["install_root"])
    state = root / "state" / "extras-enrollment.json"
    saved = json.loads(private_read(state)) if state.exists() else {"deployment_id": config["deployment_id"], "apps": {}}
    if saved.get("deployment_id") != config["deployment_id"]:
        raise SafetyError("Extra account reservation belongs to another installation")
    if "immich" not in saved["apps"]:
        path = no_symlinks(root / "appdata" / "immich-postgres")
        saved["apps"]["immich"] = {"fresh": path.is_dir() and not any(path.iterdir()),
                                    "paths": [{"path": str(path), "inode": path.stat().st_ino}]}
    write_json(state, saved, replace=state.exists())
    return saved
