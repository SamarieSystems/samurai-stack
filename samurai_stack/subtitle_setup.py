"""Reviewed Bazarr English subtitles for future additions, without backlog jobs.

Contract: Bazarr v1.6.2 system/settings, system/languages/profiles and
system/languages. Profile writes replace the collection, so every existing
profile is retained and the exact collection is rechecked before the write.
Provider presence is established inside the owned pinned image. Registration
and throttle readback are not proof that a subtitle was found or downloaded.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil

from . import catalog, engine, naming, onboarding, storage
from .safety import SafetyError, digest, no_symlinks, write_json

PROFILE_NAME = "Samurai English"  # Exact historical receipt alias, not the new display default.
DEFAULT_PROVIDERS = ("gestdown",)
PROVIDERS = {"gestdown": {"scope": "series", "credentials": ()},
             "yifysubtitles": {"scope": "movies", "credentials": ()},
             "opensubtitlescom": {"scope": "series_and_movies", "credentials": ("username", "password")}}
GIB = 1024 ** 3
GENERAL = {"serie_default_enabled": True, "movie_default_enabled": True,
           "use_embedded_subs": True, "ignore_pgs_subs": False, "ignore_vobsub_subs": False,
           "ignore_ass_subs": False, "multithreading": False, "concurrent_jobs": 1,
           "upgrade_subs": False, "upgrade_manual": False, "use_whisper_fallback": False,
           "use_whisper_fallback_series": False, "use_postprocessing": False,
           "wanted_search_frequency": 876000, "wanted_search_frequency_movie": 876000,
           "subzero_mods": [], "disable_all_providers_ssl_verify": False}
PROVIDER_SCRIPT = r'''
import hashlib,json,pathlib,sys
root=pathlib.Path('/app/bazarr/bin/custom_libs/subliminal_patch/providers')
names=json.load(sys.stdin)
result={}
for name in names:
 p=root/(name+'.py')
 if p.is_symlink() or not p.is_file() or p.stat().st_size>2*1024*1024: raise SystemExit(2)
 result[name]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(result))
'''


def _binding(c):
    return {"deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
            "config_digest": digest(c)}


def _paths(c):
    onboarding._guard(c)
    root, _ = onboarding._paths(c)
    return root, root / "state/subtitle-setup.json"


def _space(c, runner):
    storage.verify(c, runner)
    root = no_symlinks(Path(c["install_root"]))
    data = no_symlinks(Path(c["storage"]["data_root"]))
    if shutil.disk_usage(data).free < 50 * GIB or shutil.disk_usage(root).free < 20 * GIB:
        raise SafetyError("Subtitle setup needs at least 50 GiB free on media storage and 20 GiB on application storage")


def _target(c, providers, runner):
    engine.check_container_ownership(c, 2, runner)
    onboarding.check_targets(c, ["bazarr"], runner)
    ids = engine.docker(runner, "ps", "--filter", "status=running", "--filter",
                        "label=com.docker.compose.project=" + naming.project_name(c, 2), "--filter",
                        "label=com.docker.compose.service=bazarr", "--format", "{{.ID}}").stdout.split()
    if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", ids[0]):
        raise SafetyError("Bazarr must be the unique owned running container")
    cid = ids[0]
    expected = engine.recipe(c, 2)["services"]["bazarr"]["image"]
    image = engine.docker(runner, "image", "inspect", expected, "--format", "{{.Id}}").stdout.strip()
    image_config = json.loads(engine.docker(runner, "image", "inspect", expected, "--format", "{{json .Config}}").stdout)
    actual = json.loads(engine.docker(runner, "inspect", cid, "--format", "{{json .Image}}").stdout)
    config = json.loads(engine.docker(runner, "inspect", cid, "--format", "{{json .Config}}").stdout)
    mounts = json.loads(engine.docker(runner, "inspect", cid, "--format", "{{json .Mounts}}").stdout)
    data = [m for m in mounts if isinstance(m, dict) and m.get("Destination") == "/data"] if isinstance(mounts, list) else []
    configs = [m for m in mounts if isinstance(m, dict) and m.get("Destination") == "/config"] if isinstance(mounts, list) else []
    if (actual != image or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            or not isinstance(config, dict) or not isinstance(image_config, dict)
            or any(config.get(key) != image_config.get(key) for key in ("Cmd", "Entrypoint", "WorkingDir"))
            or len(data) != 1 or data[0].get("Type") != "bind" or data[0].get("RW") is not True
            or data[0].get("Source") != c["storage"]["data_root"]
            or len(configs) != 1 or configs[0].get("RW") is not True
            or any(not isinstance(m, dict) or not isinstance(m.get("Destination"), str)
                   or m["Destination"].startswith(("/data/", "/config/", "/app/", "/lsiopy/"))
                   or m["Destination"] in {"/app", "/lsiopy"} for m in mounts)):
        raise SafetyError("Bazarr image or media/source mounts do not match the owned pinned deployment")
    try:
        output = runner(["docker", "--context", "default", "exec", "-i", cid,
                         "/lsiopy/bin/python3", "-c", PROVIDER_SCRIPT], input=json.dumps(providers), timeout=20)
        hashes = json.loads(output.stdout)
        if (not isinstance(hashes, dict) or set(hashes) != set(providers)
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in hashes.values())):
            raise ValueError()
    except Exception:
        raise SafetyError("Selected subtitle providers are not available in this pinned Bazarr image") from None
    return {"container": cid, "image": image, "providers": hashes,
            "directory": onboarding._identity(Path(c["install_root"]) / "appdata/bazarr")}


def _providers(value):
    if (not isinstance(value, (list, tuple)) or not value or len(value) > len(PROVIDERS)
            or any(not isinstance(item, str) or item not in PROVIDERS for item in value)
            or len(set(value)) != len(value)):
        raise SafetyError("Choose supported subtitle providers explicitly; Whisper and extraction providers are excluded")
    return sorted(value)


def _same_persistent_target(saved, current):
    # Compose can recreate a container after shutdown. _target has already
    # proved the new container's deployment, pinned image and exact mounts.
    # Container IDs remain exact during a single reviewed write transaction.
    keys = {"container", "image", "providers", "directory"}
    return (isinstance(saved, dict) and isinstance(current, dict) and set(saved) == keys and set(current) == keys
            and all(isinstance(row["container"], str) and re.fullmatch(r"[a-f0-9]{12,64}", row["container"])
                    for row in (saved, current))
            and saved["image"] == current["image"] and saved["providers"] == current["providers"]
            and onboarding.same_identity(saved["directory"], current["directory"]))


def save_provider_credentials(c, provider, supplied):
    """Store optional provider login privately; no login, search or download occurs."""
    root, _ = _paths(c)
    if provider not in PROVIDERS or not PROVIDERS[provider]["credentials"]:
        raise SafetyError("This provider does not accept a stored login")
    if (not isinstance(supplied, dict) or set(supplied) != set(PROVIDERS[provider]["credentials"])
            or any(not isinstance(v, str) or not 1 <= len(v) <= 256
                   or any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in supplied.values())):
        raise SafetyError("Provide the subtitle provider username and password through hidden private input")
    _account_username(provider, supplied)
    path = root / "secrets" / ("subtitle-provider-" + provider + ".json")
    value = {"schema_version": 1, **_binding(c), "credentials": supplied}
    if path.exists() and onboarding._private_json(path) != value:
        raise SafetyError("Saved subtitle provider credentials differ; no credentials were replaced")
    if not path.exists():
        write_json(path, value)
    return provider


def _account_username(provider, credentials):
    # The .com API rejects email logins even though the website accepts them.
    # Fail before persisting input or causing a native authentication throttle.
    if provider == "opensubtitlescom" and "@" in credentials["username"]:
        raise SafetyError("OpenSubtitles.com requires the account username, not an email address")


def _credentials(c, providers):
    root, _ = _paths(c)
    result = {}
    for name in providers:
        if PROVIDERS[name]["credentials"]:
            value = onboarding._private_json(root / "secrets" / ("subtitle-provider-" + name + ".json"))
            if any(value.get(k) != v for k, v in _binding(c).items()):
                raise SafetyError("Subtitle provider input belongs to another installation or configuration")
            creds = value.get("credentials")
            if (not isinstance(creds, dict) or set(creds) != set(PROVIDERS[name]["credentials"])
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 256
                           or any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in creds.values())):
                raise SafetyError("Subtitle provider private input is invalid")
            _account_username(name, creds)
            result[name] = creds
    return result


class _Api:
    def __init__(self, c, request, resolver):
        self.base = catalog.stage_endpoints(c, 2)["bazarr"] + "/api"
        self.request, self.key = request, resolver("bazarr_api_key")
        if not isinstance(self.key, str) or not self.key.strip():
            raise SafetyError("The owned Bazarr API key is required")

    def call(self, method, path, form=None):
        try:
            options = {"headers": {"X-API-KEY": self.key.rstrip("\r\n")}, "timeout": 30}
            if form is not None:
                options["form"] = form
            return self.request(method, self.base + path, **options)
        except Exception:
            raise SafetyError("Bazarr subtitle policy request failed; private retry state was retained") from None


def profile_name(c):
    return "English | " + naming.project_name(c, 2)


def _profile(profile_id, name=PROFILE_NAME):
    return {"profileId": profile_id, "name": name, "cutoff": 65535,
            "items": [{"id": 1, "language": "en", "audio_exclude": "False", "audio_only_include": "False",
                       "forced": "False", "hi": "False"}],
            "mustContain": [], "mustNotContain": [], "originalFormat": 1, "tag": None}


def _legacy_profile(profile_id):
    """Exact previous installer defect, never a generic profile conversion."""
    result = _profile(profile_id)
    item = result["items"][0]
    item["audioExclude"] = item.pop("audio_exclude")
    item["audioOnlyInclude"] = item.pop("audio_only_include")
    return result


def _snapshot(api):
    status = api.call("GET", "/system/status")
    version = status.get("data", {}).get("bazarr_version") if isinstance(status, dict) and isinstance(status.get("data"), dict) else None
    if not isinstance(version, str) or version.lstrip("v") != "1.6.2":
        raise SafetyError("Subtitle automation supports the reviewed Bazarr 1.6.2 API contract")
    settings = api.call("GET", "/system/settings")
    profiles = api.call("GET", "/system/languages/profiles")
    languages = api.call("GET", "/system/languages")
    if (not isinstance(settings, dict) or not isinstance(settings.get("general"), dict)
            or any(key not in settings["general"] for key in GENERAL)
            or not isinstance(settings["general"].get("enabled_providers"), list)
            or not isinstance(settings["general"].get("subzero_mods"), list)
            or len(settings["general"]["subzero_mods"]) > 64
            or any(not isinstance(mod, str) or not re.fullmatch(r"[A-Za-z0-9_()= .:#+]{1,256}", mod)
                   for mod in settings["general"]["subzero_mods"])
            or any(key not in settings["general"] for key in ("serie_default_profile", "movie_default_profile"))
            or not isinstance(profiles, list) or len(profiles) > 128
            or any(not isinstance(row, dict) or type(row.get("profileId")) is not int or row["profileId"] < 1 for row in profiles)
            or len({p["profileId"] for p in profiles}) != len(profiles)
            or not isinstance(languages, list) or len(languages) > 512
            or any(not isinstance(row, dict) or not isinstance(row.get("code2"), str)
                   or type(row.get("enabled")) is not bool for row in languages)
            or len({row["code2"] for row in languages}) != len(languages)
            or not any(row["code2"] == "en" for row in languages)):
        raise SafetyError("Bazarr returned an unsupported subtitle settings, language or profile schema")
    return {"settings": settings, "profiles": sorted(profiles, key=lambda row: row["profileId"]),
            "languages": sorted(languages, key=lambda row: row["code2"])}


def _receipt(c, path):
    if not path.exists():
        return None
    value = onboarding._private_json(path)
    if value.get("schema_version") != 1 or any(value.get(k) != v for k, v in _binding(c).items()):
        raise SafetyError("Subtitle policy receipt belongs to another host or configuration")
    if not isinstance(value.get("profile"), dict) or value.get("state") not in {"intent", "verified"}:
        raise SafetyError("Subtitle policy receipt has an unsupported structure")
    return value


def _plan(c, providers, runner, request, resolver):
    root, path = _paths(c)
    receipt = _receipt(c, path)
    if providers is None:
        providers = receipt["providers"] if receipt else DEFAULT_PROVIDERS
    providers = _providers(providers)
    _space(c, runner)
    target = _target(c, providers, runner)
    creds = _credentials(c, providers)
    api = _Api(c, request, resolver or engine.secret_resolver(c))
    before = _snapshot(api)
    matches = [row for row in before["profiles"] if row.get("name") in {PROFILE_NAME, profile_name(c)}]
    accepted = [receipt["profile"]] if receipt else []
    if receipt:
        profile_id = receipt["profile"].get("profileId")
        if (type(profile_id) is not int or profile_id < 1
                or receipt["profile"] not in (_profile(profile_id), _profile(profile_id, profile_name(c)), _legacy_profile(profile_id))):
            raise SafetyError("Saved English subtitle profile does not match a supported installer policy")
        previous = receipt.get("previous_profile")
        if previous is not None:
            if previous not in (_legacy_profile(profile_id), _profile(profile_id)):
                raise SafetyError("Saved subtitle repair intent does not match the exact earlier installer defect")
            if receipt["state"] == "intent":
                accepted.append(previous)
    if matches and (not receipt or len(matches) != 1 or matches[0] not in accepted):
        raise SafetyError("An existing subtitle profile uses the managed name; no profile was replaced")
    if receipt and not matches and receipt.get("state") == "verified":
        raise SafetyError("The verified subtitle profile is missing; it will not be recreated automatically")
    desired = _profile(receipt["profile"]["profileId"], profile_name(c)) if receipt else _profile(max([p["profileId"] for p in before["profiles"]] + [0]) + 1, profile_name(c))
    repair = bool(matches) and matches[0] != desired
    relabel = bool(matches) and {**matches[0], "name": desired["name"]} == desired and repair
    general = {**GENERAL, "serie_default_profile": desired["profileId"], "movie_default_profile": desired["profileId"],
               "enabled_providers": providers}
    changes = {key: value for key, value in general.items() if before["settings"]["general"].get(key) != value}
    value = {"schema_version": 1, "kind": "subtitle-future-policy", **_binding(c), "target": target,
             "providers": providers, "input_digest": digest(creds), "snapshot_digest": digest(before),
             "provider_scopes": {name: PROVIDERS[name]["scope"] for name in providers},
             "movie_provider_configured": any(PROVIDERS[name]["scope"] in {"movies", "series_and_movies"} for name in providers),
             "receipt_digest": digest(receipt), "profile": desired, "create_profile": not bool(matches),
             "repair_profile": repair, "profile_policy_version": 3,
             "profile_action": "rename_owned_profile" if relabel else "repair_owned_legacy_profile" if repair else "create" if not matches else "verify",
             "settings": changes, "scope": "future_additions_only", "existing_media_assigned": 0,
             "minimum_free_gib": {"media": 50, "applications": 20},
             "note": "Provider registration is checked; subtitle availability and delivery remain untested"}
    value["review_hash"] = digest(value)
    return value, before, creds, api, path


def plan(c, providers=None, *, runner=None, request=None, secret_resolver=None):
    return _plan(c, providers, runner or engine.run, request or engine.request, secret_resolver)[0]


def _assert_policy(api, desired, providers):
    after = _snapshot(api)
    matches = [p for p in after["profiles"] if p.get("profileId") == desired["profileId"]]
    expected = {**GENERAL, "serie_default_profile": desired["profileId"], "movie_default_profile": desired["profileId"],
                "enabled_providers": providers}
    if matches != [desired] or any(after["settings"]["general"].get(k) != v for k, v in expected.items()):
        raise SafetyError("Bazarr did not retain the exact English future-subtitle policy")
    if not any(row["code2"] == "en" and row["enabled"] for row in after["languages"]):
        raise SafetyError("Bazarr did not retain the enabled English subtitle language")
    status = api.call("GET", "/providers")
    rows = status.get("data") if isinstance(status, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or any(len([row for row in rows if row.get("name") == name]) != 1 for name in providers)):
        raise SafetyError("Selected subtitle providers are not registered by the running Bazarr service")
    states = {name: next(row.get("status") for row in rows if row.get("name") == name) for name in providers}
    if any(not isinstance(state, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_ .:-]{0,95}", state) for state in states.values()):
        raise SafetyError("Bazarr returned an unsupported subtitle provider status")
    notifications = ["Provider is throttled or unavailable: " + name for name, state in states.items() if state != "Good"]
    movie_provider = any(PROVIDERS[name]["scope"] in {"movies", "series_and_movies"} for name in providers)
    if not movie_provider:
        notifications.append("No movie subtitle provider is configured; add an OpenSubtitles.com account for film searches")
    if "yifysubtitles" in providers:
        notifications.append("YIFY is a retained legacy provider with known connection failures; it is not recommended for new setup")
    return after, {"ok": True, "scope": "future_additions_only", "profile_id": desired["profileId"],
                   "language": "English", "existing_media_assigned": 0, "embedded_subtitles_satisfy_profile": True,
                   "provider_scopes": {name: PROVIDERS[name]["scope"] for name in providers},
                   "provider_status": states, "subtitle_delivery": "not_tested",
                   "movie_provider_configured": movie_provider, "notifications": notifications}


def _assert_credentials(after, creds):
    for name, values in creds.items():
        actual = after["settings"].get(name, {})
        if (not isinstance(actual, dict) or any(actual.get(key) != value for key, value in values.items())
                or actual.get("include_ai_translated") is not False
                or actual.get("include_machine_translated") is not False):
            raise SafetyError("Bazarr did not retain the private subtitle-provider input and disabled AI settings")


def apply(c, reviewed_plan, *, runner=None, request=None, secret_resolver=None):
    if not isinstance(reviewed_plan, dict) or reviewed_plan.get("kind") != "subtitle-future-policy":
        raise SafetyError("Review the subtitle future-additions policy before applying")
    runner, request = runner or engine.run, request or engine.request
    _paths(c)
    with engine.deployment_lock(c):
        current, before, creds, api, path = _plan(c, reviewed_plan.get("providers"), runner, request, secret_resolver)
        if current != reviewed_plan:
            raise SafetyError("Subtitle policy changed after review; no subtitle setting was written")
        receipt = {"schema_version": 1, **_binding(c), "state": "intent", "profile": current["profile"],
                   "providers": current["providers"], "target": current["target"],
                   "before_profiles": before["profiles"], "before_languages": before["languages"]}
        if current["repair_profile"]:
            receipt["previous_profile"] = next(p for p in before["profiles"] if p["profileId"] == current["profile"]["profileId"])
            backup = no_symlinks(path.parent / ("subtitle-profile-repair-" + current["review_hash"] + ".json"))
            backup_value = {"schema_version": 1, **_binding(c), "review_hash": current["review_hash"], "before": before}
            if backup.exists() and onboarding._private_json(backup) != backup_value:
                raise SafetyError("Private subtitle profile repair backup conflicts with the reviewed state")
            if not backup.exists():
                write_json(backup, backup_value)
        write_json(path, receipt, replace=path.exists())
        if _snapshot(api) != before:
            raise SafetyError("Subtitle settings changed before writing; reviewed retry state was retained")
        form = []
        if current["create_profile"] or current["repair_profile"]:
            profiles = [current["profile"] if p["profileId"] == current["profile"]["profileId"] else p for p in before["profiles"]]
            if current["create_profile"]:
                profiles.append(current["profile"])
            form.append(("languages-profiles", json.dumps(profiles)))
        if not next(row["enabled"] for row in before["languages"] if row["code2"] == "en"):
            form.extend(("languages-enabled", code) for code in sorted({row["code2"] for row in before["languages"] if row["enabled"]} | {"en"}))
        for key, value in current["settings"].items():
            field = "settings-general-" + key
            if key == "subzero_mods":
                # This UI list is backed by a string validator. Writing the
                # array directly makes save_settings fail validation. Its
                # supported toggle keys rebuild the string without modifiers.
                for mod in before["settings"]["general"]["subzero_mods"]:
                    form.append(("subzero-color", "") if mod.startswith("color") else ("subzero-" + mod, "false"))
            elif isinstance(value, list):
                form.extend((field, item) for item in value or [""])
            else:
                form.append((field, str(value).lower() if type(value) is bool else str(value)))
        for name, values in creds.items():
            actual = before["settings"].get(name, {})
            form.extend(("settings-" + name + "-" + key, value) for key, value in values.items()
                        if actual.get(key) != value)
            for flag in ("include_ai_translated", "include_machine_translated"):
                if actual.get(flag) is not False:
                    form.append(("settings-" + name + "-" + flag, "false"))
        if form:
            api.call("POST", "/system/settings", form)
        after, report = _assert_policy(api, current["profile"], current["providers"])
        _assert_credentials(after, creds)
        other_before = [p for p in before["profiles"] if p["profileId"] != current["profile"]["profileId"]]
        other_after = [p for p in after["profiles"] if p["profileId"] != current["profile"]["profileId"]]
        if other_before != other_after:
            raise SafetyError("Other subtitle profiles changed during setup; no rollback or deletion was attempted")
        if _target(c, current["providers"], runner) != current["target"]:
            raise SafetyError("Bazarr target changed during subtitle setup; private intent was retained")
        receipt["state"] = "verified"
        write_json(path, receipt, replace=True)
    return report


def verify(c, *, runner=None, request=None, secret_resolver=None):
    runner, request = runner or engine.run, request or engine.request
    _, path = _paths(c)
    receipt = _receipt(c, path)
    if not receipt or receipt.get("state") != "verified":
        raise SafetyError("A reviewed verified subtitle policy receipt is required")
    providers = _providers(receipt.get("providers"))
    _space(c, runner)
    if not _same_persistent_target(receipt.get("target"), _target(c, providers, runner)):
        raise SafetyError("Bazarr target changed since subtitle setup")
    after, report = _assert_policy(_Api(c, request, secret_resolver or engine.secret_resolver(c)), receipt["profile"], providers)
    _assert_credentials(after, _credentials(c, providers))
    return report
