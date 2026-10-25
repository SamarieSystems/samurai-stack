"""Reviewed naming settings for future Starr imports, never mass renaming.

Only config/naming is written. Existing files, title paths, imports, searches,
queues and quality policy are outside this module. The live examples endpoint
must see id=1 or Starr substitutes its saved settings for the proposed formats.
"""
from copy import deepcopy
import re
from urllib.parse import urlencode

from . import catalog, engine, onboarding
from .safety import SafetyError, digest, no_symlinks, write_json

SERVICES = ("sonarr", "radarr")
DETAILS = ("{[Custom Formats]}{[Quality Full]}{[Mediainfo AudioCodec}{ Mediainfo AudioChannels]}"
           "{[MediaInfo VideoDynamicRangeType]}{[Mediainfo VideoCodec]}{-Release Group}")
TITLE = "{Series CleanTitleWithoutYear} {(Series Year)}"
POLICY = {
    "sonarr": {
        "renameEpisodes": True, "replaceIllegalCharacters": True,
        "standardEpisodeFormat": TITLE + " - S{season:00}E{episode:00} - {Episode CleanTitle:90} " + DETAILS,
        "dailyEpisodeFormat": TITLE + " - {Air-Date} - {Episode CleanTitle:90} " + DETAILS,
        "animeEpisodeFormat": (TITLE + " - S{season:00}E{episode:00} - {absolute:000} - {Episode CleanTitle:90} "
            + "{[Custom Formats]}{[Quality Full]}{[Mediainfo AudioCodec}{ Mediainfo AudioChannels]}"
            + "{MediaInfo AudioLanguages}{[MediaInfo VideoDynamicRangeType]}"
            + "[{Mediainfo VideoCodec }{MediaInfo VideoBitDepth}bit]{-Release Group}"),
        "seriesFolderFormat": TITLE + " [tvdbid-{TvdbId}]", "seasonFolderFormat": "Season {season:00}"},
    "radarr": {
        "renameMovies": True, "replaceIllegalCharacters": True,
        "standardMovieFormat": ("{Movie CleanTitle} {(Release Year)} [tmdbid-{TmdbId}] - {{Edition Tags}} "
            + "{[MediaInfo 3D]}" + DETAILS),
        "movieFolderFormat": "{Movie CleanTitle} ({Release Year}) [tmdbid-{TmdbId}]"}}
EXAMPLES = {
    "sonarr": ("singleEpisodeExample", "multiEpisodeExample", "dailyEpisodeExample", "animeEpisodeExample",
               "animeMultiEpisodeExample", "seriesFolderExample", "seasonFolderExample"),
    "radarr": ("movieExample", "movieFolderExample")}
QUERY_FIELDS = {
    "sonarr": ("id", "renameEpisodes", "replaceIllegalCharacters", "colonReplacementFormat",
               "customColonReplacementFormat", "multiEpisodeStyle", "standardEpisodeFormat", "dailyEpisodeFormat",
               "animeEpisodeFormat", "seriesFolderFormat", "seasonFolderFormat", "specialsFolderFormat"),
    "radarr": ("id", "renameMovies", "replaceIllegalCharacters", "colonReplacementFormat",
               "standardMovieFormat", "movieFolderFormat")}
SCOPE = "Future imports and new title folders only; no existing-file rename, move, import, scan or search was requested"


def _guard(c, runner):
    onboarding._guard(c)
    root, _ = onboarding._paths(c)
    engine.check_container_ownership(c, 2, runner)
    onboarding.check_targets(c, SERVICES, runner)
    return root


class _Api:
    def __init__(self, c, request, resolver):
        self.endpoints = catalog.stage_endpoints(c, 2)
        self.request, self.resolver = request, resolver

    def call(self, service, path, body=None):
        try:
            key = self.resolver(service + "_api_key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError()
            kwargs = {"headers": {"X-Api-Key": key.rstrip("\r\n")}, "timeout": 30}
            if body is not None:
                kwargs["json"] = body
            return self.request("PUT" if body is not None else "GET",
                self.endpoints[service] + "/api/v3/config/naming" + path, **kwargs)
        except Exception:
            raise SafetyError("Starr naming request failed; inspect the service locally. Existing files were not renamed") from None


def _settings(service, value):
    if not isinstance(value, dict) or len(value) > 64 or type(value.get("id")) is not int or value["id"] != 1:
        raise SafetyError("Starr returned an unsupported naming resource identity")
    for name in QUERY_FIELDS[service]:
        if name not in value:
            raise SafetyError("Starr lacks a required naming setting; no naming changes were accepted")
        item = value[name]
        if name == "colonReplacementFormat" and service == "radarr":
            # Radarr's resource is an enum serialized by camel-case
            # JsonStringEnumConverter(allowIntegerValues:true): e.g. smart.
            # Sonarr exposes an int instead. Keep the live Radarr value exact,
            # including a future vendor-recognized enum, and let its examples
            # endpoint validate it rather than converting or resetting policy.
            if type(item) is not int and not (isinstance(item, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", item)):
                raise SafetyError("Radarr returned an unsupported colon-replacement enum")
            continue
        expected = bool if name.startswith("rename") or name == "replaceIllegalCharacters" else (
            int if name in {"id", "colonReplacementFormat", "multiEpisodeStyle"} else str)
        if type(item) is not expected or isinstance(item, str) and (len(item) > 4096 or any(ord(char) < 32 for char in item)):
            raise SafetyError("Starr returned an unsupported naming setting value")
    return deepcopy(value)


def _examples(api, service, desired):
    query = urlencode({name: str(desired[name]).lower() if type(desired[name]) is bool else desired[name]
                       for name in QUERY_FIELDS[service]})
    examples = api.call(service, "/examples?" + query)
    if (not isinstance(examples, dict) or any(not isinstance(examples.get(key), str)
            or not examples[key].strip() or len(examples[key]) > 4096 for key in EXAMPLES[service])):
        raise SafetyError("Starr did not produce valid examples for every proposed naming format")
    return {key: examples[key] for key in EXAMPLES[service]}


def _inventory(c, runner, request, resolver):
    _guard(c, runner)
    api = _Api(c, request, resolver)
    states = {}
    for service in SERVICES:
        before = _settings(service, api.call(service, ""))
        after = {**deepcopy(before), **deepcopy(POLICY[service])}
        states[service] = {"before": before, "after": after, "examples": _examples(api, service, after)}
    return api, states


def _review(c, states):
    result = {"schema_version": 1, "policy_version": 1, "kind": "future-import-naming",
              "deployment_id": c["deployment_id"], "deployment_name": c["deployment_name"],
              "host_id": engine.host_identity(), "config_digest": digest(c),
              "install_root": c["install_root"], "bind_address": c["bind_address"],
              "scope": SCOPE, "services": {}}
    for service, state in states.items():
        changes = {key: {"before": state["before"][key], "after": value}
                   for key, value in POLICY[service].items() if state["before"][key] != value}
        result["services"][service] = {"action": "update" if changes else "verify", "changes": changes,
            "snapshot_hash": digest(state["before"]), "examples": state["examples"]}
    result["review_hash"] = digest(result)
    return result


def plan(c, *, runner=None, request=None, secret_resolver=None):
    """Read-only proposed formats and vendor-rendered examples."""
    _, states = _inventory(c, runner or engine.run, request or engine.request,
                          secret_resolver or engine.secret_resolver(c))
    return _review(c, states)


def _backup(c, root, reviewed, states):
    directory = no_symlinks(root / "backups")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = no_symlinks(directory / ("media-naming-" + reviewed["review_hash"] + ".json"))
    value = {"schema_version": 1, "deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
             "config_digest": digest(c), "review_hash": reviewed["review_hash"],
             "services": {service: deepcopy(state["before"]) for service, state in states.items()}}
    if path.exists():
        if onboarding._private_json(path) != value:
            raise SafetyError("The private naming backup conflicts with this reviewed state")
    else:
        write_json(path, value)


def _read_all(api):
    return {service: _settings(service, api.call(service, "")) for service in SERVICES}


def apply(c, reviewed_plan, *, runner=None, request=None, secret_resolver=None):
    """Apply one exact reviewed snapshot, preserving unrelated naming settings.

    An interrupted partial apply needs a fresh review. No rollback changes files
    or settings behind the operator's back, and verified matches are not rewritten.
    """
    if (not isinstance(reviewed_plan, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", str(reviewed_plan.get("review_hash", "")))):
        raise SafetyError("Review the future-import naming plan before apply")
    runner, request = runner or engine.run, request or engine.request
    resolver = secret_resolver or engine.secret_resolver(c)
    root = _guard(c, runner)
    with engine.deployment_lock(c):
        api, states = _inventory(c, runner, request, resolver)
        if _review(c, states) != reviewed_plan:
            raise SafetyError("Naming state, host or configuration changed after the preview; review a fresh plan")
        expected = {service: deepcopy(state["before"]) for service, state in states.items()}
        if any(state["before"] != state["after"] for state in states.values()):
            _backup(c, root, reviewed_plan, states)
        for service, state in states.items():
            if state["before"] == state["after"]:
                continue
            _guard(c, runner)
            if _read_all(api) != expected:
                raise SafetyError("Naming settings changed during apply; existing changes were kept, review a fresh plan")
            api.call(service, "/" + str(state["before"]["id"]), deepcopy(state["after"]))
            expected[service] = deepcopy(state["after"])
            if _read_all(api) != expected:
                raise SafetyError("Starr did not preserve and read back the exact reviewed naming settings; inspect before retrying")
        _guard(c, runner)
        if _read_all(api) != expected:
            raise SafetyError("Naming settings changed before final verification; review the live state")
        return verify(c, runner=runner, request=request, secret_resolver=resolver)


def verify(c, *, runner=None, request=None, secret_resolver=None):
    _, states = _inventory(c, runner or engine.run, request or engine.request,
                          secret_resolver or engine.secret_resolver(c))
    services = {service: {"ok": state["before"] == state["after"],
                         "examples": state["examples"]} for service, state in states.items()}
    ok = all(item["ok"] for item in services.values())
    return {"ok": ok, "status": "verified" if ok else "pending", "services": services, "scope": SCOPE}
