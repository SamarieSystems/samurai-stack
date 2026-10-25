"""Choose existing installation state before starting the guided setup menu.

An explicit path always wins. Automatic discovery prefers the host-bound
registry and managed service references over a replaceable source checkout.
Unresolved running installations never become a new configuration wizard.
This module creates no accounts and does not adopt, restart or remove services.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

from . import installation_registry as registry, recovery, naming
from .safety import SafetyError, digest, no_symlinks, private_read, run, validate_config


_RECOVER = "Existing Samurai installation evidence needs review. Run ./setup --recover before creating another configuration. Existing services and files were retained."


def _root_owned(path):
    return os.name != "posix" or path.stat().st_uid == 0


def _saved_source(path):
    """A private root-owned old site may live in a normal source directory."""
    path = no_symlinks(Path(path).absolute())
    if path.stat().st_size > registry.MAX_BYTES or not _root_owned(path):
        raise SafetyError("The existing source configuration must be bounded, private and owned by root. It was not replaced.")
    try:
        config = json.loads(private_read(path).lstrip("\ufeff"))
        validate_config(config)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SafetyError):
        raise SafetyError("The existing source configuration could not be validated safely. Keep it and run ./setup --recover.") from None
    return {"config": config, "path": path, "source": "source", "aliases": []}


def _units_exist(unit_root):
    try:
        root = no_symlinks(Path(unit_root))
        return root.exists() and any(root.glob("samurai-*.service"))
    except OSError:
        raise SafetyError(_RECOVER) from None


def _inventory(runner, unit_root):
    try:
        result = recovery.inventory(runner=runner, systemd_dir=unit_root)
    except Exception:
        raise SafetyError(_RECOVER) from None
    if not isinstance(result, dict) or not isinstance(result.get("containers"), list) or not isinstance(result.get("systemd_units"), list):
        raise SafetyError(_RECOVER)
    return result


def _source_matches_runtime(config, report):
    """Do not register a new source-tree UUID over an old project's containers."""
    containers = report["containers"]
    if not containers:
        return
    identifier = config["deployment_id"]
    projects = {naming.project_name(config, stage) for stage in range(7)}
    matched = False
    for row in containers:
        if not isinstance(row, dict):
            raise SafetyError(_RECOVER)
        project = row.get("project")
        same_name = project in projects if isinstance(project, str) else False
        same_id = row.get("deployment_id") == identifier
        if same_name != same_id:
            raise SafetyError(_RECOVER)
        matched |= same_id and same_name
        # A different UUID occupying this application's local root is a
        # conflict even when the proposed deployment name also changed.
        for mount in row.get("mounts", []):
            source = mount.get("source") if isinstance(mount, dict) else None
            if (not same_id and isinstance(source, str)
                    and (source == config["install_root"] or source.startswith(config["install_root"].rstrip("/") + "/"))):
                raise SafetyError(_RECOVER)
    if not matched:
        # Existing unrelated installations require an explicit --config choice.
        raise SafetyError(_RECOVER)


def _choose(candidates, input_fn, output_fn):
    if len(candidates) == 1:
        return candidates[0]
    if input_fn is input and not sys.stdin.isatty():
        raise SafetyError("Several saved installations exist. Select one in an interactive terminal or pass its explicit --config path.")
    output_fn("Several saved installations exist. Choose the configuration to resume:")
    for number, row in enumerate(candidates, 1):
        c = row["config"]
        output_fn(str(number) + ". " + c["deployment_name"] + " | " + c["deployment_id"]
                  + " | apps " + c["install_root"] + " | media " + c["storage"]["data_root"])
    while True:
        value = input_fn("Configuration number, or Enter to pause: ").strip()
        if not value:
            raise SafetyError("Configuration selection paused. No installation identity was changed.")
        if value.isascii() and value.isdigit() and 1 <= int(value) <= len(candidates):
            return candidates[int(value) - 1]
        output_fn("Enter one of the configuration numbers. No configuration has been changed.")


def _remember(candidate, registry_root, runner, output_fn):
    path = no_symlinks(Path(candidate["path"]).absolute())
    c = candidate["config"]
    if candidate.get("warnings"):
        output_fn("Warning: A managed service runtime configuration is missing. The saved installation identity was kept; inspect application data before any redeployment. Run ./setup --recover for read-only evidence.")
    try:
        canonical = registry.remember_verified(c, registry_root=registry_root, runner=runner)
    except (SafetyError, OSError, RuntimeError, ValueError):
        output_fn("Using the saved configuration at " + str(path) + ". Its persistent recovery copy could not be verified or updated; the original identity and files were kept.")
        return path
    output_fn("Resuming saved installation " + c["deployment_name"] + " (" + c["deployment_id"] + ").")
    return no_symlinks(Path(canonical).absolute())


def resolve_configuration(config_path=None, *, input_fn=input, output_fn=print,
                          registry_root=registry.REGISTRY_ROOT, setup_root=registry.SETUP_ROOT,
                          unit_root=registry.UNIT_ROOT, runner=run):
    """Return an absolute existing site or a persistent new-draft location.

    A source file is never silently substituted for trusted runtime state.
    Storage verification is required before saving a canonical registry copy;
    an unavailable mount leaves the selected original path usable for repair.
    """
    if config_path is not None:
        return no_symlinks(Path(config_path).absolute())
    try:
        trusted = registry.discover(registry_root=registry_root, unit_root=unit_root)
    except (SafetyError, OSError, ValueError):
        raise SafetyError(_RECOVER) from None
    if trusted:
        return _remember(_choose(trusted, input_fn, output_fn), registry_root, runner, output_fn)

    from .setup_fresh import pending_fresh_configuration
    fresh = pending_fresh_configuration(setup_root=setup_root)
    if fresh is not None:
        output_fn('Continuing the fresh setup session. Earlier source-folder configuration is not used.')
        return fresh

    # Read a pre-registration draft without creating its directory. It remains
    # recoverable across checkout replacement even before storage is verified.
    draft = no_symlinks(Path(setup_root).absolute() / registry.host_identity() / "site.json")
    candidates = []
    if draft.exists():
        try:
            candidates.extend(registry.discover(registry_root=registry_root, unit_root=unit_root, config_paths=(draft,)))
        except (SafetyError, OSError, ValueError):
            raise SafetyError(_RECOVER) from None
    source = no_symlinks(Path.cwd() / "site.json")
    if source.exists() and all(Path(row["path"]) != source for row in candidates):
        candidates.append(_saved_source(source))
    unique = {}
    for row in candidates:
        unique.setdefault(digest(row["config"]), row)
    candidates = list(unique.values())

    report = _inventory(runner, unit_root)
    units = bool(report["systemd_units"]) or _units_exist(unit_root)
    docker_unknown = bool(report.get("warnings")) and not report["containers"] and shutil.which("docker") is not None
    if candidates:
        selected = _choose(candidates, input_fn, output_fn)
        if docker_unknown or units:
            raise SafetyError(_RECOVER)
        _source_matches_runtime(selected["config"], report)
        return _remember(selected, registry_root, runner, output_fn)
    if report["containers"] or units or docker_unknown:
        raise SafetyError(_RECOVER)
    return no_symlinks(registry.draft_directory(setup_root=setup_root) / "site.json")
