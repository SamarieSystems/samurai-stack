"""Reviewed, opt-in Starr profiles. No searches, imports or title reassignment.

Profiles and custom formats are created from live API schemas. Existing policy
is verified rather than overwritten; reviewed legacy-label updates retain the
same IDs and policy. File-size definitions are global Starr settings, so changing
them requires a separate reviewed plan.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from . import engine, hardlinks, integrations, naming
from .safety import SafetyError, digest, no_symlinks, write_json


SERVICES = ("sonarr", "radarr")
QUALITIES = frozenset({"HDTV-720p", "WEBDL-720p", "WEBRip-720p", "Bluray-720p",
                      "HDTV-1080p", "WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p"})
PREFERENCE = ("Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p", "HDTV-1080p",
              "Bluray-720p", "WEBDL-720p", "WEBRip-720p", "HDTV-720p")
HDR_PATTERN = r"(?i)\b(?:HDR(?:10(?:\+|Plus)?)?|HLG|Dolby[ ._-]*Vision|DoVi|DV)\b"
LIMITS = {
    "sonarr": {720: (5, 20, 40), 1080: (10, 45, 75)},
    "radarr": {720: (8, 30, 60), 1080: (15, 60, 90)},
}
LIMIT_KEYS = ("minSize", "preferredSize", "maxSize")
LIMITATIONS = [
    "Original means English for English-origin titles and the original language for foreign titles; it is not English-only.",
    "Language and HDR checks depend on provider metadata and parsed release names; they cannot guarantee the audio tracks or video inside a mislabeled file.",
    "720p/1080p SDR is a selection policy, not a guarantee that every codec or client can direct-play without CPU transcoding.",
    "Existing titles, their profiles, files and download queues are not changed. Select the new profile for future additions or explicitly choose it in the requesting app.",
]


def profile_name(config):
    return "1080p Original | " + naming.project_name(config, 2)


def legacy_profile_name(config):
    return "Samurai " + config["deployment_name"] + " 1080p Original"


def _name(config, suffix):
    return suffix + " | " + naming.project_name(config, 2)


def _legacy_format_name(config, desired):
    return "Samurai " + config["deployment_name"] + ": " + desired["name"].rsplit(" | ", 1)[0]


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SafetyError("Starr returned an unsupported resource schema; no settings were accepted")
    return value


def _unique(rows, name):
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) > 1:
        raise SafetyError("Duplicate Samurai profile or custom format names; review them in Starr before retrying")
    if matches and (not _integer(matches[0].get("id")) or matches[0]["id"] < 1):
        raise SafetyError("Starr returned an invalid resource ID")
    return matches[0] if matches else None


def _alias(rows, name, legacy):
    """Only the exact friendly/legacy pair can identify one managed resource."""
    matches = [row for row in rows if row.get("name") in {name, legacy}]
    if len(matches) > 1:
        raise SafetyError("Ambiguous friendly and legacy profile/custom format names; existing resources were kept")
    if matches and (not _integer(matches[0].get("id")) or matches[0]["id"] < 1):
        raise SafetyError("Starr returned an invalid resource ID")
    return matches[0] if matches else None


def _matching_policy(policy, existing, desired):
    """Display names may differ; the complete selection policy may not."""
    current = policy(existing)
    current["name"] = desired["name"]
    return current == policy(desired)


def _action(existing, name):
    return "create" if existing is None else "verify" if existing["name"] == name else "rename"


def _specification(schema, implementation, *, name, value, negate=False):
    matches = [row for row in _rows(schema) if row.get("implementation") == implementation]
    if len(matches) != 1:
        raise SafetyError("This Starr version lacks a required custom-format condition")
    fields = _rows(matches[0].get("fields"))
    if len([field for field in fields if field.get("name") == "value"]) != 1:
        raise SafetyError("This Starr version has an unsupported custom-format field schema")
    result = {"name": name, "implementation": implementation, "negate": negate, "required": False,
              "fields": [{"name": field["name"], "value": deepcopy(field.get("value"))} for field in fields]}
    for field in result["fields"]:
        if field["name"] == "value":
            field["value"] = value
        elif field["name"] == "exceptLanguage":
            field["value"] = False
    return result


def _formats(config, service, schema, original):
    result = [{"name": _name(config, "HDR or Dolby Vision"), "includeCustomFormatWhenRenaming": False,
               "specifications": [_specification(schema, "ReleaseTitleSpecification",
                                    name="HDR or Dolby Vision release name", value=HDR_PATTERN)]}]
    if service == "sonarr":
        result.append({"name": _name(config, "Missing original audio"), "includeCustomFormatWhenRenaming": False,
                       "specifications": [_specification(schema, "LanguageSpecification",
                                            name="Does not include original language", value=original["id"], negate=True)]})
    return result


def _cf_policy(row):
    """Ignore UI metadata but require the complete condition policy to match."""
    specs = []
    for spec in _rows(row.get("specifications")):
        fields = _rows(spec.get("fields"))
        if any(not isinstance(field.get("name"), str) for field in fields):
            raise SafetyError("Starr returned an unsupported custom-format schema")
        if len({field["name"] for field in fields}) != len(fields):
            raise SafetyError("Starr returned duplicate custom-format fields")
        specs.append({"name": spec.get("name"), "implementation": spec.get("implementation"),
                      "negate": spec.get("negate"), "required": spec.get("required"),
                      "fields": {field["name"]: field.get("value") for field in fields}})
    return {"name": row.get("name"), "includeCustomFormatWhenRenaming": row.get("includeCustomFormatWhenRenaming"),
            "specifications": specs}


def _quality_items(items):
    items = deepcopy(_rows(items))
    available, cutoffs = set(), {}

    def visit(row, parent_id=None):
        children = _rows(row.get("items", []))
        quality = row.get("quality")
        if quality is not None:
            if not isinstance(quality, dict) or not _integer(quality.get("id")) or children:
                raise SafetyError("Starr returned an unsupported quality-profile item")
            name = quality.get("name")
            row["allowed"] = name in QUALITIES
            if row["allowed"]:
                available.add(name)
                cutoffs[name] = parent_id if parent_id is not None else quality["id"]
        else:
            if not children or not _integer(row.get("id")):
                raise SafetyError("Starr returned an empty quality group")
            for child in children:
                visit(child, row["id"] if parent_id is None else parent_id)
            row["allowed"] = any(child["allowed"] for child in children)

    for item in items:
        visit(item)
    if not QUALITIES <= available:
        raise SafetyError("This Starr version lacks one or more required 720p/1080p qualities")
    return items, cutoffs[next(name for name in PREFERENCE if name in available)]


def _profile(config, service, schema, formats, managed_names, original):
    if not isinstance(schema, dict):
        raise SafetyError("Starr returned an unsupported quality-profile template")
    required = {"upgradeAllowed", "cutoff", "items", "minFormatScore", "cutoffFormatScore", "formatItems"}
    if not required <= set(schema):
        raise SafetyError("This Starr version lacks required quality-profile policy fields")
    result = deepcopy(schema)
    result.pop("id", None)
    result["name"] = profile_name(config)
    result["items"], result["cutoff"] = _quality_items(schema["items"])
    result.update(upgradeAllowed=False, minFormatScore=0, cutoffFormatScore=0)
    if "minUpgradeFormatScore" in result:
        # Both live pinned apps validate >= 1 even when upgrades are disabled.
        result["minUpgradeFormatScore"] = 1
    if service == "radarr":
        if "language" not in result:
            raise SafetyError("This Radarr version lacks native profile language selection")
        result["language"] = {"id": original["id"], "name": original["name"]}
    result["formatItems"] = [{"format": row["id"], "name": row["name"],
                               "score": -10000 if row["name"] in managed_names else 0} for row in formats]
    return result


def _quality_policy(items):
    result = []
    for item in _rows(items):
        quality = item.get("quality")
        result.append({"id": item.get("id"), "quality": quality.get("id") if isinstance(quality, dict) else None,
                       "allowed": item.get("allowed"), "items": _quality_policy(item.get("items", []))})
    return result


def _profile_policy(row):
    formats = _rows(row.get("formatItems"))
    if (any(not _integer(item.get("format")) or not _integer(item.get("score")) for item in formats)
            or len({item["format"] for item in formats}) != len(formats)):
        raise SafetyError("Starr returned unsupported custom-format scores")
    result = {key: deepcopy(row.get(key)) for key in ("name", "upgradeAllowed", "cutoff", "minFormatScore", "cutoffFormatScore")}
    if "minUpgradeFormatScore" in row:
        result["minUpgradeFormatScore"] = row["minUpgradeFormatScore"]
    if "language" in row:
        language = row["language"]
        if not isinstance(language, dict):
            raise SafetyError("Radarr returned an unsupported language selection")
        result["language"] = language.get("id")
    result["items"] = _quality_policy(row.get("items"))
    result["formatItems"] = sorted((item["format"], item["score"]) for item in formats)
    return result


def _sizes(service, definitions):
    changes, found = [], set()
    for row in _rows(definitions):
        quality = row.get("quality")
        if not isinstance(quality, dict) or quality.get("name") not in QUALITIES:
            continue
        name = quality["name"]
        # Starr omits nullable unlimited max/preferred limits from JSON.
        if name in found or not _integer(row.get("id")) or "minSize" not in row:
            raise SafetyError("Starr returned unsupported global quality definitions")
        found.add(name)
        values = dict(zip(LIMIT_KEYS, LIMITS[service][1080 if name.endswith("1080p") else 720]))
        if any(row.get(key) != value for key, value in values.items()):
            after = deepcopy(row)
            after.update(values)
            changes.append({"quality": name, "id": row["id"],
                            "before": {key: row.get(key) for key in LIMIT_KEYS}, "after": values, "resource": after})
    if found != QUALITIES:
        raise SafetyError("Starr did not return all required global quality definitions")
    return changes


class _Inventory:
    def __init__(self, config, request, resolver, include_global_sizes):
        self.api = integrations._Run(config, 2, request, resolver, False).api
        self.services = {}
        for service in SERVICES:
            call = lambda path: self.api(service, "/api/v3/" + path)
            profiles, formats = _rows(call("qualityprofile")), _rows(call("customformat"))
            profile_schema, format_schema = call("qualityprofile/schema"), _rows(call("customformat/schema"))
            languages = _rows(call("language"))
            originals = [row for row in languages if row.get("name") == "Original" and _integer(row.get("id"))]
            if len(originals) != 1 or originals[0]["id"] >= 0:
                raise SafetyError("Starr did not expose an unambiguous Original language")
            original = originals[0]
            desired_formats = _formats(config, service, format_schema, original)
            managed_names = {row["name"] for row in desired_formats}
            existing_formats = {}
            for desired in desired_formats:
                existing = _alias(formats, desired["name"], _legacy_format_name(config, desired))
                existing_formats[desired["name"]] = existing
                if existing and not _matching_policy(_cf_policy, existing, desired):
                    raise SafetyError("An existing Samurai custom format differs; it was kept. Review it in Starr before retrying")
            # Missing formats use temporary negative IDs only in the reviewed
            # projection. Apply rebuilds the body with IDs read back from Starr.
            projected_formats = deepcopy(formats)
            for number, desired in enumerate(desired_formats, 1):
                existing = existing_formats[desired["name"]]
                if existing:
                    projected = next(row for row in projected_formats if row["id"] == existing["id"])
                    projected["name"] = desired["name"]
                else:
                    projected_formats.append({**deepcopy(desired), "id": -100000 - number})
            desired_profile = _profile(config, service, profile_schema, projected_formats, managed_names, original)
            existing_profile = _alias(profiles, profile_name(config), legacy_profile_name(config))
            if existing_profile and not _matching_policy(_profile_policy, existing_profile, desired_profile):
                raise SafetyError("An existing Samurai quality profile differs; it was kept. Review it in Starr before retrying")
            definitions = _rows(call("qualitydefinition")) if include_global_sizes else []
            self.services[service] = {"profiles": profiles, "formats": formats, "profile_schema": profile_schema,
                                      "original": original, "desired_formats": desired_formats,
                                      "existing_formats": existing_formats,
                                      "desired_profile": desired_profile, "existing_profile": existing_profile,
                                      "definitions": definitions,
                                      "size_changes": _sizes(service, definitions) if include_global_sizes else []}

    def reviewed(self, config, include_global_sizes):
        services = {}
        for service, state in self.services.items():
            services[service] = {
                "profile": profile_name(config),
                "action": _action(state["existing_profile"], profile_name(config)),
                "profile_id": state["existing_profile"]["id"] if state["existing_profile"] else None,
                "previous_profile_name": state["existing_profile"]["name"] if state["existing_profile"] else None,
                "custom_formats": [{"name": row["name"], "score": -10000,
                                     "action": _action(state["existing_formats"][row["name"]], row["name"]),
                                     "id": state["existing_formats"][row["name"]]["id"] if state["existing_formats"][row["name"]] else None,
                                     "previous_name": state["existing_formats"][row["name"]]["name"] if state["existing_formats"][row["name"]] else None}
                                    for row in state["desired_formats"]],
                "size_changes": [{key: row[key] for key in ("id", "quality", "before", "after")} for row in state["size_changes"]],
                "snapshot_hash": digest({key: state[key] for key in ("profiles", "formats", "profile_schema", "definitions", "original")}),
            }
        result = {"schema_version": 1, "policy_version": 2, "deployment_id": config["deployment_id"],
                  "deployment_name": config["deployment_name"], "bind_address": config["bind_address"],
                  "host_id": engine.host_identity(), "config_digest": digest(config),
                  "include_global_sizes": include_global_sizes, "services": services,
                  "policy": {"qualities": sorted(QUALITIES), "language": "Original", "hdr": "reject parsed HDR/DV names",
                             "upgrades": False, "minimum_custom_format_score": 0,
                             "positive_custom_format_scores": 0, "size_unit": "MB/minute",
                             "global_sizes": "reviewed updates to the eight allowed qualities only" if include_global_sizes else "unchanged"},
                  "limitations": list(LIMITATIONS)}
        result["review_hash"] = digest(result)
        return result


def _inventory(config, runner, request, secret_resolver, include_global_sizes):
    if not isinstance(include_global_sizes, bool):
        raise SafetyError("Global size policy must be an explicit true/false choice")
    hardlinks.check_targets(config, runner)
    return _Inventory(config, request, secret_resolver, include_global_sizes)


def plan(config, *, include_global_sizes=False, runner=None, request=None, secret_resolver=None):
    """Read-only preview. Size limits remain unchanged unless explicitly selected."""
    inventory = _inventory(config, runner or engine.run, request or engine.request,
                           secret_resolver or engine.secret_resolver(config), include_global_sizes)
    return inventory.reviewed(config, include_global_sizes)


def _backup(config, reviewed, inventory):
    root = no_symlinks(Path(config["install_root"]) / "backups")
    root.mkdir(mode=0o700, exist_ok=True)
    path = no_symlinks(root / ("media-profiles-" + reviewed["review_hash"] + ".json"))
    if not path.exists():
        write_json(path, {"deployment_id": config["deployment_id"], "review_hash": reviewed["review_hash"],
                          "services": {service: {key: state[key] for key in ("profiles", "formats", "definitions")}
                                       for service, state in inventory.services.items()}})


def apply(config, reviewed_plan, *, runner=None, request=None, secret_resolver=None):
    """Apply exactly the reviewed live state, then verify every created resource.

    Existing matching resources make retries safe. A conflict or stale preview
    is refused before any API write. Partial API failures leave an explicit,
    inspectable partial setup, never a destructive rollback.
    """
    if not isinstance(reviewed_plan, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(reviewed_plan.get("review_hash", ""))):
        raise SafetyError("Review the profile plan before applying it")
    runner, request = runner or engine.run, request or engine.request
    resolver = secret_resolver or engine.secret_resolver(config)
    include_sizes = reviewed_plan.get("include_global_sizes")
    hardlinks.check_targets(config, runner)
    with engine.deployment_lock(config):
        inventory = _inventory(config, runner, request, resolver, include_sizes)
        current = inventory.reviewed(config, include_sizes)
        if current != reviewed_plan:
            raise SafetyError("Starr settings changed after the preview; review a fresh profile plan before applying")
        _backup(config, reviewed_plan, inventory)
        profile_ids = {}
        for service, state in inventory.services.items():
            api = lambda path, method="GET", body=None: inventory.api(service, "/api/v3/" + path, method, body)
            for desired in state["desired_formats"]:
                existing = _alias(_rows(api("customformat")), desired["name"], _legacy_format_name(config, desired))
                reviewed = state["existing_formats"][desired["name"]]
                if reviewed is not None and existing != reviewed:
                    raise SafetyError("A custom format changed during setup; its settings were kept")
                if existing and not _matching_policy(_cf_policy, existing, desired):
                    raise SafetyError("An existing Samurai custom format differs; its settings were kept")
                if not existing:
                    api("customformat", "POST", desired)
                elif existing["name"] != desired["name"]:
                    renamed = deepcopy(existing)
                    renamed["name"] = desired["name"]
                    api("customformat/" + str(existing["id"]), "PUT", renamed)
                    saved = _unique(_rows(api("customformat")), desired["name"])
                    if saved != renamed:
                        raise SafetyError("Starr did not retain the label-only custom-format update")
                existing = _unique(_rows(api("customformat")), desired["name"])
                if not existing or _cf_policy(existing) != _cf_policy(desired):
                    raise SafetyError("Starr did not retain a Samurai custom format; no profile was accepted")
            formats = _rows(api("customformat"))
            desired_profile = _profile(config, service, state["profile_schema"], formats,
                                       {row["name"] for row in state["desired_formats"]}, state["original"])
            # Recheck immediately before the create, including outside changes
            # made while another app or custom format was being configured.
            existing = _alias(_rows(api("qualityprofile")), desired_profile["name"], legacy_profile_name(config))
            reviewed = state["existing_profile"]
            if reviewed and (not existing or existing["id"] != reviewed["id"]):
                raise SafetyError("A quality profile changed identity during setup; its settings were kept")
            if existing and not _matching_policy(_profile_policy, existing, desired_profile):
                raise SafetyError("A Samurai profile changed during setup; its settings were kept")
            if not existing:
                api("qualityprofile", "POST", desired_profile)
            elif existing["name"] != desired_profile["name"]:
                renamed = deepcopy(existing)
                renamed["name"] = desired_profile["name"]
                api("qualityprofile/" + str(existing["id"]), "PUT", renamed)
                saved = _unique(_rows(api("qualityprofile")), desired_profile["name"])
                if saved != renamed:
                    raise SafetyError("Starr did not retain the label-only quality-profile update")
            existing = _unique(_rows(api("qualityprofile")), desired_profile["name"])
            if not existing or _profile_policy(existing) != _profile_policy(desired_profile):
                raise SafetyError("Starr did not retain the reviewed Samurai profile")
            profile_ids[service] = existing["id"]
            for change in state["size_changes"]:
                latest = _rows(api("qualitydefinition"))
                rows = [row for row in latest if row.get("id") == change["id"]]
                original = next(row for row in state["definitions"] if row["id"] == change["id"])
                if len(rows) != 1 or rows[0] != original:
                    raise SafetyError("A global size definition changed during setup; review a fresh size plan")
                api("qualitydefinition/" + str(change["id"]), "PUT", change["resource"])
                latest = _rows(api("qualitydefinition"))
                rows = [row for row in latest if row.get("id") == change["id"]]
                if len(rows) != 1 or any(rows[0].get(key) != value for key, value in change["after"].items()):
                    raise SafetyError("Starr did not retain the reviewed global size limits")
        result = verify(config, include_global_sizes=include_sizes, runner=runner, request=request, secret_resolver=resolver)
        if not result["ok"]:
            raise SafetyError("Final Starr profile readback failed; inspect the saved private profile backup")
        return result


def verify(config, *, include_global_sizes=False, runner=None, request=None, secret_resolver=None):
    """Read-only policy proof; no title, download or import APIs are called."""
    inventory = _inventory(config, runner or engine.run, request or engine.request,
                           secret_resolver or engine.secret_resolver(config), include_global_sizes)
    profiles = {service: state["existing_profile"]["id"] if state["existing_profile"] else None
                for service, state in inventory.services.items()}
    missing = [service for service, identifier in profiles.items() if identifier is None]
    sizes_ok = not include_global_sizes or not any(state["size_changes"] for state in inventory.services.values())
    return {"ok": not missing and sizes_ok, "status": "verified" if not missing and sizes_ok else "pending",
            "profile_name": profile_name(config), "profile_ids": profiles, "missing": missing,
            "current_profile_names": {service: state["existing_profile"]["name"] if state["existing_profile"] else None
                                      for service, state in inventory.services.items()},
            "label_updates_pending": sum(
                int(state["existing_profile"] is not None and state["existing_profile"]["name"] != profile_name(config))
                + sum(existing is not None and existing["name"] != name
                      for name, existing in state["existing_formats"].items())
                for state in inventory.services.values()),
            "global_sizes": "verified" if include_global_sizes and sizes_ok else "pending" if include_global_sizes else "unchanged",
            "limitations": list(LIMITATIONS)}
