"""One Compose identity for recipes, runtime paths and ownership checks.

Older sites deliberately retain their numbered project names. New sites may
choose role names before deployment; changing an installed name is a migration,
not a cosmetic Dockge edit.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .safety import SafetyError, no_symlinks, private_read

PROJECT_ROLES = {2: "media", 3: "torrents", 4: "management", 5: "photos", 6: "music"}
UNIT_ROOT = Path("/etc/systemd/system")


def default_projects(deployment_name):
    """Meaningful suggestions for a new installation, keyed by engine stage."""
    return {str(stage): deployment_name + "-" + role for stage, role in PROJECT_ROLES.items()}


def project_name(config, stage):
    """Return the exact historic name unless this stage was explicitly named."""
    return config.get("compose_projects", {}).get(str(stage), config["deployment_name"] + "-" + str(stage))


def validate_projects(config):
    projects = config.get("compose_projects", {})
    if not isinstance(projects, dict) or any(key not in {str(stage) for stage in PROJECT_ROLES} for key in projects):
        raise SafetyError("compose_projects must be an object keyed by application stages 2 through 6")
    for value in projects.values():
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value):
            raise SafetyError("Compose project names must be 1-63 lowercase letters, digits, hyphens or underscores, starting with a letter or digit")
    names = [project_name(config, stage) for stage in PROJECT_ROLES]
    if len(set(names)) != len(names):
        raise SafetyError("Every Compose set needs a different project name, including stages with numbered defaults")
    return projects


def _registered_names(config, registry_root):
    from . import installation_registry as registry
    root = no_symlinks(Path(registry_root))
    directory = no_symlinks(root / config["deployment_id"])
    if not directory.exists():
        return
    registry._directory(root)
    registry._directory(directory)
    # Verify the host-bound immutable identity even when its runtime snapshots
    # disappeared. An incomplete or unreadable reservation is never a rename.
    registry._metadata(directory / "identity.json", config)


def _unit_names(config, unit_root):
    from . import retired_units
    for stage in PROJECT_ROLES:
        unit = no_symlinks(Path(unit_root) / ("samurai-" + config["deployment_name"] + "-" + str(stage) + ".service"))
        if not unit.exists():
            continue
        _, text, _ = retired_units._unit(unit)
        markers = re.findall(r"^# Managed by Samurai Stack: ([^\r\n]+)$", text, re.M)
        if config["deployment_id"] not in markers:
            continue  # Foreign UUIDs still require the separate retirement proof.
        if markers != [config["deployment_id"]]:
            raise SafetyError("The existing owned service has ambiguous deployment markers; its files were retained")
        # _generated validates every directive, but intentionally returns early
        # for the current UUID. Substitute only the comparison UUID, never the
        # on-disk unit, to use its complete read-only generated-unit proof.
        proof = dict(config, deployment_id="00000000-0000-0000-0000-000000000000")
        if proof["deployment_id"] == config["deployment_id"]:
            proof["deployment_id"] = "11111111-1111-1111-1111-111111111111"
        # First releases used a common guard site. Accept that exact historical
        # reference while checking the original project/Compose path unchanged.
        old_guard = "/usr/bin/python3 " + config["install_root"] + "/runtime_guard.py " + config["install_root"] + "/site.json " + str(stage)
        new_guard = "/usr/bin/python3 " + config["install_root"] + "/runtime_guard.py " + config["install_root"] + "/stages/" + project_name(config, stage) + "/site.json " + str(stage)
        checked = text.replace("ExecStartPre=" + old_guard + "\n", "ExecStartPre=" + new_guard + "\n")
        try:
            retired_units._generated(proof, stage, checked)
        except SafetyError:
            raise SafetyError("The owned service for installed stage " + str(stage)
                              + " does not match its saved Compose project and generated paths. Restore its original compose_projects names/configuration; renaming needs a reviewed migration. Existing services and files were retained.") from None


def check_installed_names(config, *, registry_root=None, unit_root=None):
    """Refuse renaming an existing stage, even before looking up its new name.

    Canonical identity, exact owned service directives and private runtime
    snapshots are independent proofs. Losing one never authorizes a rename.
    """
    try:
        return _check_installed_names(config, registry_root=registry_root, unit_root=unit_root)
    except (PermissionError, SafetyError) as exc:
        cause = exc
        while cause is not None:
            if isinstance(cause, PermissionError):
                raise SafetyError("Existing installation identity cannot be read with this account. Run sudo ./setup or sudo ./stack with the original configuration. Existing services and files were retained; unreadable identity was not treated as a fresh installation.") from None
            cause = cause.__cause__
        raise


def _check_installed_names(config, *, registry_root=None, unit_root=None):
    if registry_root is None:
        from .installation_registry import REGISTRY_ROOT
        registry_root = REGISTRY_ROOT
    if unit_root is None:
        unit_root = UNIT_ROOT
    _registered_names(config, registry_root)
    _unit_names(config, unit_root)
    root = no_symlinks(Path(config["install_root"]) / "stages")
    if not root.exists():
        return
    paths = sorted(root.glob("*/site.json"))
    if len(paths) > 64:
        raise SafetyError("Too many saved stage configurations; review installation identity before continuing")
    for path in paths:
        no_symlinks(path)
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError()
            saved = json.loads(private_read(path))
            if not isinstance(saved, dict) or saved.get("deployment_id") != config["deployment_id"]:
                raise ValueError()
            if saved.get("install_root") != config["install_root"]:
                raise ValueError()
            validate_projects(saved)
            stages = [stage for stage in PROJECT_ROLES if project_name(saved, stage) == path.parent.name]
            if len(stages) != 1:
                raise ValueError()
        except PermissionError:
            raise
        except (OSError, ValueError, KeyError, TypeError, SafetyError):
            raise SafetyError("Saved stage identity is invalid or conflicts with this installation; existing services and files were retained") from None
        stage = stages[0]
        if project_name(config, stage) != path.parent.name:
            raise SafetyError("Compose project name for installed stage " + str(stage) + " is fixed as "
                              + path.parent.name + ". Restore its saved compose_projects name. Renaming needs a reviewed migration; editing only compose.yaml name does not rename containers.")
