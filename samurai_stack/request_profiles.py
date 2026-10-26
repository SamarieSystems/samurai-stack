"""Explicit four-choice request policy for an existing owned media deployment.

Standard and anime default to bounded HD. UHD is a deliberate quality-profile
choice on the same backend, not a second pretend 4K server or duplicate copy.
No search, acquisition, file move or automatic quality upgrade is performed.
"""
from copy import deepcopy
from pathlib import Path

from . import catalog, engine, hardlinks, integrations, media_profiles, naming, onboarding
from .safety import SafetyError, digest, no_symlinks, write_json

VERSION = 3
VARIANTS = ("hd", "anime_hd", "uhd", "anime_uhd")
TITLES = {"hd": "1080p English", "anime_hd": "Anime 1080p Dual Audio",
          "uhd": "4K English (opt-in)", "anime_uhd": "Anime 4K Dual Audio (opt-in)"}
FACTORIES = {1: "Any", 2: "SD", 3: "HD-720p", 4: "HD-1080p", 5: "Ultra-HD", 6: "HD - 720p/1080p"}
UHD = frozenset({"WEBDL-2160p", "WEBRip-2160p", "Bluray-2160p"})
UHD_LIMITS = {"sonarr": (15, 55, 100), "radarr": (20, 90, 150)}
DUAL = r"(?i)\b(?:dual|multi)[ ._-]*audio\b|\bdual\b"
ENGLISH_SUBS = r"(?i)\b(?:eng(?:lish)?)[ ._-]*(?:sub(?:title)?s?|subbed)\b|\bsub(?:title)?s?[ ._-]*eng(?:lish)?\b"
DV = r"(?i)\b(?:Dolby[ ._-]*Vision|DoVi|DV)\b"
HDR10 = r"(?i)\bHDR10(?:Plus|\+)?\b"
LIMITATIONS = [
    "English and dual-audio/subtitle preferences use release metadata and names; actual tracks still need playback inspection.",
    "Anime prefers English and dual audio, with Japanese fallback. English subtitle-name evidence is preferred, not proof of embedded subtitle availability.",
    "The advanced request dropdown selects one stored version. Replacing existing HD with UHD is an owner operation, not a duplicate 4K request.",
    "UHD requires a compatible direct-play client on a CPU-only server. No transcoding performance is claimed.",
]


def profile_names(c):
    return {kind: text + " | " + naming.project_name(c, 2) for kind, text in TITLES.items()}


def _path(c):
    return no_symlinks(Path(c["install_root"]) / "state/request-profiles.json")


def _binding(c):
    return {"deployment_id": c["deployment_id"], "host_id": engine.host_identity(), "config_digest": digest(c)}


def receipt(c):
    path = _path(c)
    if not path.exists():
        return None
    row = onboarding._private_json(path)
    if (row.get("schema_version") != 1 or row.get("policy_version") != VERSION
            or row.get("state") not in {"intent", "verified"}
            or any(row.get(k) != v for k, v in _binding(c).items())
            or row.get("profile_names") != profile_names(c)):
        raise SafetyError("The request-profile policy receipt belongs to another installation or policy")
    return row


def has_policy(c):
    return receipt(c) is not None


def _language(rows, name):
    found = [r for r in rows if r.get("name") == name]
    if len(found) != 1 or type(found[0].get("id")) is not int:
        raise SafetyError("Starr lacks an unambiguous required request language")
    return {"id": found[0]["id"], "name": found[0]["name"]}


def _format_name(c, name):
    return name + " | " + naming.project_name(c, 2)


def build_formats(c, app, schema, languages):
    def condition(implementation, label, value, *, negate=False, required=False):
        row = media_profiles._specification(schema, implementation, name=label, value=value, negate=negate)
        row["required"] = required
        return row
    def title(label, pattern, **kwargs):
        return condition("ReleaseTitleSpecification", label, pattern, **kwargs)
    def language(label, target, **kwargs):
        return condition("LanguageSpecification", label, _language(languages, target)["id"], **kwargs)
    definitions = [
        ("HDR or Dolby Vision", [title("HDR or Dolby Vision release name", media_profiles.HDR_PATTERN)]),
        ("English audio", [language("English audio", "English")]),
        ("No English audio", [language("English is absent", "English", negate=True)]),
        ("No English or Japanese audio", [language("English is absent", "English", negate=True, required=True),
                                         language("Japanese is absent", "Japanese", negate=True, required=True)]),
        ("Dual audio release", [title("Dual audio release-name evidence", DUAL)]),
        ("English subtitle release", [title("English subtitle release-name evidence", ENGLISH_SUBS)]),
        ("Dolby Vision without HDR10", [title("Dolby Vision", DV, required=True),
                                       title("No advertised HDR10 fallback", HDR10, negate=True, required=True)]),
    ]
    return [{"name": _format_name(c, label), "includeCustomFormatWhenRenaming": False,
             "specifications": specs} for label, specs in definitions]


def _leaves(items):
    result = []
    for row in media_profiles._rows(items):
        if isinstance(row.get("quality"), dict):
            if row.get("items") or type(row["quality"].get("id")) is not int:
                raise SafetyError("Unsupported Starr quality leaf")
            result.append(deepcopy(row))
        else:
            result.extend(_leaves(row.get("items")))
    identifiers = [r["quality"]["id"] for r in result]
    if len(set(identifiers)) != len(identifiers):
        raise SafetyError("Duplicate Starr quality IDs")
    return result


def quality_items(schema, uhd):
    leaves = _leaves(schema)
    allowed = UHD if uhd else media_profiles.QUALITIES
    if not allowed <= {r["quality"].get("name") for r in leaves}:
        raise SafetyError("Starr lacks required bounded request qualities")
    items = []
    for row in leaves:
        if row["quality"]["name"] not in allowed:
            row["allowed"] = False
            items.append(row)
    resolutions = (2160,) if uhd else (720, 1080)
    for number, resolution in enumerate(resolutions, 1000):
        children = [r for r in leaves if r["quality"]["name"] in allowed
                    and r["quality"]["name"].endswith(str(resolution) + "p")]
        for child in children:
            child["allowed"] = True
        items.append({"id": number, "name": str(resolution) + "p", "items": children, "allowed": True})
    return items, 1000 + len(resolutions) - 1


def build_profile(c, app, variant, template, formats, languages):
    row = deepcopy(template)
    row.pop("id", None)
    anime, uhd = variant.startswith("anime"), "uhd" in variant
    row["name"] = profile_names(c)[variant]
    row["items"], row["cutoff"] = quality_items(template["items"], uhd)
    row.update(upgradeAllowed=False, minFormatScore=0, cutoffFormatScore=0)
    if "minUpgradeFormatScore" in row:
        row["minUpgradeFormatScore"] = 1
    if app == "radarr":
        row["language"] = _language(languages, "Any" if anime else "English")
    scores = {"HDR or Dolby Vision": 0 if uhd else -10000,
              "English audio": 700 if anime else 200,
              "No English audio": 0 if anime else -10000,
              "No English or Japanese audio": -10000 if anime else 0,
              "Dual audio release": 1500, "English subtitle release": 1000,
              "Dolby Vision without HDR10": -10000}
    row["formatItems"] = [{"format": fmt["id"], "name": fmt["name"],
                            "score": scores.get(fmt["name"].rsplit(" | ", 1)[0], 0)} for fmt in formats]
    return row


def is_anime(title):
    genres = {x.lower() for x in title.get("genres", []) if isinstance(x, str)}
    language = title.get("originalLanguage") or {}
    japanese = language.get("name") == "Japanese"
    return "anime" in genres or (japanese and ("animation" in genres or title.get("seriesType") == "anime"))


class Inventory:
    def __init__(self, c, *, runner, request, resolver, include_global_sizes):
        if type(include_global_sizes) is not bool:
            raise SafetyError("Shared HD size review requires an explicit true/false choice")
        hardlinks.check_targets(c, runner)
        self.c, self.request, self.resolver = c, request, resolver
        self.saved, self.services = receipt(c), {}
        self.names = profile_names(c)
        seerr = catalog.stage_endpoints(c, 2)["seerr"] + "/api/v1/"
        self.seerr = lambda path, method="GET", body=None: request(method, seerr + path,
            headers={"X-Api-Key": resolver("seerr_api_key")}, json=body)
        self.connections = {app: self.seerr("settings/" + app) for app in media_profiles.SERVICES}
        self.rules = self.seerr("overrideRule")
        requests = self.seerr("request?take=1000&skip=0")
        if (not isinstance(requests, dict) or not isinstance(requests.get("results"), list)
                or requests.get("pageInfo", {}).get("results") != len(requests["results"])):
            raise SafetyError("The complete Seerr request reference inventory is unavailable")
        self.references = [{k: row.get(k) for k in ("id", "type", "profileId", "serverId")}
                           for row in requests["results"]]
        for app in media_profiles.SERVICES:
            call = lambda path: self.call(app, path)
            profiles, formats = media_profiles._rows(call("qualityprofile")), media_profiles._rows(call("customformat"))
            schema, cf_schema, languages = call("qualityprofile/schema"), call("customformat/schema"), call("language")
            titles = media_profiles._rows(call("series" if app == "sonarr" else "movie"))
            import_lists = media_profiles._rows(call("importlist"))
            collections = media_profiles._rows(call("collection")) if app == "radarr" else []
            definitions = media_profiles._rows(call("qualitydefinition"))
            desired_formats = build_formats(c, app, cf_schema, languages)
            for fmt in desired_formats:
                existing = media_profiles._unique(formats, fmt["name"])
                if existing and media_profiles._cf_policy(existing) != media_profiles._cf_policy(fmt):
                    raise SafetyError("An existing request custom format differs; it was not overwritten")
            legacy = {media_profiles.baseline_profile_name(c), media_profiles.legacy_profile_name(c)}
            candidates = [p for p in profiles if p["name"] in legacy | {self.names["hd"]}]
            if len(candidates) != 1:
                raise SafetyError("Four request choices require one existing installation-managed HD profile")
            standard = candidates[0]
            if not self.saved:
                if standard["name"] == self.names["hd"] or any(p["name"] in self.names.values() for p in profiles):
                    raise SafetyError("Request-profile names already exist without an owned policy receipt")
            if standard["name"] in legacy:
                # A lost custom-format response leaves the old HD profile
                # with newly appended zero-score formats. It is still the
                # exact original policy, not permission to overwrite owner
                # edits made while the first attempt was interrupted.
                baseline_formats = media_profiles._formats(c, app, cf_schema, _language(languages, "Original"))
                desired = media_profiles._profile(c, app, schema, formats, {f["name"] for f in baseline_formats}, _language(languages, "Original"))
                if not media_profiles._matching_policy(media_profiles._profile_policy, standard, desired):
                    raise SafetyError("The baseline HD profile is customized; review it before opting into four choices")
            ids = self.saved.get("profile_ids", {}).get(app, {}) if self.saved else {}
            existing_profiles = {kind: media_profiles._unique(profiles, label) for kind, label in self.names.items()}
            existing_profiles["hd"] = standard
            for kind, identifier in ids.items():
                existing = existing_profiles.get(kind)
                if not existing or existing["id"] != identifier:
                    raise SafetyError("A request profile was replaced or removed; no duplicate was created")
            factory = [p for p in profiles if p.get("id") in FACTORIES and p.get("name") == FACTORIES[p["id"]]]
            known_ids = {p["id"] for p in factory} | {p["id"] for p in existing_profiles.values() if p}
            if any(p["id"] not in known_ids for p in profiles):
                raise SafetyError("Other quality profiles exist; they were kept instead of silently retiring them")
            retiring = {p["id"] for p in factory}
            if any(t.get("qualityProfileId") in retiring for t in titles):
                raise SafetyError("A factory profile is assigned to existing media; no profile was retired")
            if any(value in retiring for value in _profile_references(import_lists)):
                raise SafetyError("An import list references a factory profile; its policy was kept")
            collection_assignments = []
            for collection in collections:
                if collection.get("qualityProfileId") not in retiring:
                    continue
                if collection.get("monitored") is not False:
                    raise SafetyError("A monitored collection references a factory profile; its policy was kept")
                members = [t for t in titles if (t.get("collection") or {}).get("tmdbId") == collection.get("tmdbId")]
                variant = "anime_hd" if members and all(is_anime(t) for t in members) else "hd"
                collection_assignments.append({"id": collection["id"], "title": collection.get("title"),
                    "from_profile": collection["qualityProfileId"], "to_kind": variant,
                    "to_profile": (existing_profiles[variant] or {}).get("id"), "to_name": self.names[variant]})
            if any(r.get("profileId") in retiring for r in self.references):
                raise SafetyError("A Seerr request references a factory profile; no profile was retired")
            if any(value in retiring for value in _profile_references(self.rules)):
                raise SafetyError("A Seerr override rule references a factory profile; no profile was retired")
            connections = self.connections[app]
            if (not isinstance(connections, list) or len(connections) != 1 or not isinstance(connections[0], dict)
                    or connections[0].get("hostname") != catalog.role_host(c, 2)
                    or connections[0].get("port") != catalog.PORTS[app]
                    or connections[0].get("is4k") is not False or connections[0].get("isDefault") is not True
                    or connections[0].get("activeProfileId") != standard["id"]):
                raise SafetyError("The request server is not one unambiguous existing default backend")
            if any(connections[0].get(k) in retiring for k in ("activeProfileId", "activeAnimeProfileId")):
                raise SafetyError("A request-server default still references a factory profile")
            projected = deepcopy(formats)
            for number, fmt in enumerate(desired_formats, 1):
                if not media_profiles._unique(projected, fmt["name"]):
                    projected.append({**deepcopy(fmt), "id": -100000 - number})
            desired_profiles = {kind: build_profile(c, app, kind, existing_profiles[kind] or schema, projected, languages)
                                for kind in VARIANTS}
            for kind, existing in existing_profiles.items():
                if existing and existing["name"] == self.names[kind]:
                    if media_profiles._profile_policy(existing) != media_profiles._profile_policy(desired_profiles[kind]):
                        raise SafetyError("An owned request profile differs from its saved policy; review the change explicitly")
            sizes = []
            for row in definitions:
                if row.get("quality", {}).get("name") in UHD:
                    wanted = dict(zip(media_profiles.LIMIT_KEYS, UHD_LIMITS[app]))
                    if any(row.get(k) != v for k, v in wanted.items()):
                        changed = {**deepcopy(row), **wanted}
                        sizes.append({"id": row["id"], "quality": row["quality"]["name"],
                                      "before": {k: row.get(k) for k in wanted}, "after": wanted, "resource": changed})
            if {r.get("quality", {}).get("name") for r in definitions} & UHD != UHD:
                raise SafetyError("Starr lacks complete UHD size definitions")
            if include_global_sizes:
                sizes.extend(media_profiles._sizes(app, definitions))
            assignments = [{"id": t["id"], "title": t.get("title"), "path": t.get("path"),
                            "from_profile": t["qualityProfileId"], "to_profile": existing_profiles["anime_hd"]["id"] if existing_profiles["anime_hd"] else None,
                            "to_name": self.names["anime_hd"]}
                           for t in titles if is_anime(t) and t.get("qualityProfileId") == standard["id"]]
            self.services[app] = {"profiles": profiles, "formats": formats, "schema": schema, "cf_schema": cf_schema,
                "languages": languages, "definitions": definitions, "titles": titles, "existing_profiles": existing_profiles,
                "desired_formats": desired_formats, "desired_profiles": desired_profiles, "retiring": factory,
                "size_changes": sizes, "assignments": assignments, "import_lists": import_lists,
                "collections": collections, "collection_assignments": collection_assignments}

    def call(self, app, path, method="GET", body=None):
        try:
            return integrations._Run(self.c, 2, self.request, self.resolver, False).api(app, "/api/v3/" + path, method, body)
        except Exception:
            raise SafetyError("The request-profile API did not complete; private intent and backups were retained") from None

    def reviewed(self, include_global_sizes):
        services = {}
        for app, row in self.services.items():
            services[app] = {"profile": self.names["hd"], "profile_id": row["existing_profiles"]["hd"]["id"],
                "action": "verify" if not row["retiring"] and not row["assignments"] and all(row["existing_profiles"].values()) else "configure_request_choices",
                "choices": [{"kind": kind, "name": self.names[kind],
                             "id": row["existing_profiles"][kind]["id"] if row["existing_profiles"][kind] else None}
                            for kind in VARIANTS],
                "custom_formats": [{"name": f["name"], "action": "verify" if media_profiles._unique(row["formats"], f["name"]) else "create"}
                                   for f in row["desired_formats"]],
                "retire_factory_profiles": [{"id": p["id"], "name": p["name"]} for p in row["retiring"]],
                "anime_assignments": row["assignments"],
                "collection_assignments": row["collection_assignments"],
                "size_changes": [{k: r[k] for k in ("id", "quality", "before", "after")} for r in row["size_changes"]],
                "snapshot_hash": digest({k: row[k] for k in ("profiles", "formats", "schema", "cf_schema", "languages", "definitions")}),
                "title_reference_hash": digest([_title_identity(t) for t in row["titles"]]),
                "import_list_reference_hash": digest(row["import_lists"]), "collection_reference_hash": digest(row["collections"])}
        document = {"schema_version": 1, "policy_version": VERSION, **_binding(self.c),
                    "include_global_sizes": include_global_sizes, "services": services,
                    "seerr_snapshot_hash": digest(self.connections), "request_reference_hash": digest(self.references),
                    "override_rules_hash": digest(self.rules), "policy": {"choices": 4, "default": "hd",
                    "anime_default": "anime_hd", "upgrades": False, "remux": False, "one_copy": True,
                    "uhd_size_limits": "reviewed encoded UHD qualities only", "shared_hd_size_limits": include_global_sizes},
                    "limitations": LIMITATIONS}
        document["review_hash"] = digest(document)
        return document


def _profile_references(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"profileid", "qualityprofileid", "activeprofileid", "activeanimeprofileid"} and type(item) is int:
                found.append(item)
            else:
                found.extend(_profile_references(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_profile_references(item))
    return found


def _title_identity(row):
    return {k: deepcopy(row.get(k)) for k in ("id", "path", "qualityProfileId", "genres", "originalLanguage", "seriesType")}


def _same_with_appended_formats(actual, previous):
    """New custom formats are added to every Starr profile at score zero."""
    left, right = deepcopy(actual), deepcopy(previous)
    latest, original = left.pop("formatItems", []), right.pop("formatItems", [])
    if left != right:
        return False
    indexed = {row["format"]: row for row in latest}
    return (len(indexed) == len(latest) and all(indexed.get(row["format"]) == row for row in original)
            and all(row.get("score") == 0 for row in latest if row["format"] not in {r["format"] for r in original}))


def _inventory(c, include_global_sizes, runner, request, secret_resolver):
    return Inventory(c, runner=runner or engine.run, request=request or engine.request,
                     resolver=secret_resolver or engine.secret_resolver(c), include_global_sizes=include_global_sizes)


def plan(c, *, include_global_sizes=False, runner=None, request=None, secret_resolver=None):
    return _inventory(c, include_global_sizes, runner, request, secret_resolver).reviewed(include_global_sizes)


def apply(c, reviewed_plan, *, runner=None, request=None, secret_resolver=None):
    if not isinstance(reviewed_plan, dict) or reviewed_plan.get("policy_version") != VERSION:
        raise SafetyError("Review the explicit four-choice request profile policy first")
    include_sizes = reviewed_plan.get("include_global_sizes")
    runner, request, resolver = runner or engine.run, request or engine.request, secret_resolver or engine.secret_resolver(c)
    with engine.deployment_lock(c):
        inventory = _inventory(c, include_sizes, runner, request, resolver)
        if inventory.reviewed(include_sizes) != reviewed_plan:
            raise SafetyError("The request-profile preview is stale; review again before any API write")
        backup = no_symlinks(Path(c["install_root"]) / "backups" / ("request-profiles-" + reviewed_plan["review_hash"] + ".json"))
        if not backup.exists():
            write_json(backup, {"plan": reviewed_plan, "services": inventory.services, "seerr": inventory.connections,
                                "rules": inventory.rules, "references": inventory.references})
        saved = inventory.saved or {"schema_version": 1, "policy_version": VERSION, **_binding(c),
                                   "profile_names": profile_names(c),
                                   "profile_ids": {app: {"hd": row["existing_profiles"]["hd"]["id"]}
                                                   for app, row in inventory.services.items()}, "state": "intent"}
        def save():
            write_json(_path(c), saved, replace=_path(c).exists())
        save()
        for app, state in inventory.services.items():
            profiles_changed = False
            call = lambda path, method="GET", body=None: inventory.call(app, path, method, body)
            for desired in state["desired_formats"]:
                rows = media_profiles._rows(call("customformat"))
                existing = media_profiles._unique(rows, desired["name"])
                if not existing:
                    call("customformat", "POST", desired)
                saved_format = media_profiles._unique(media_profiles._rows(call("customformat")), desired["name"])
                if not saved_format or media_profiles._cf_policy(saved_format) != media_profiles._cf_policy(desired):
                    raise SafetyError("Starr did not retain the request custom format")
            formats = media_profiles._rows(call("customformat"))
            profile_ids = saved["profile_ids"].setdefault(app, {})
            for kind in VARIANTS:
                profiles = media_profiles._rows(call("qualityprofile"))
                original = state["existing_profiles"][kind]
                existing = (next((p for p in profiles if original and p["id"] == original["id"]), None)
                            if original else media_profiles._unique(profiles, inventory.names[kind]))
                desired = build_profile(c, app, kind, existing or state["schema"], formats, state["languages"])
                if existing:
                    if kind == "hd" and existing["id"] != state["existing_profiles"]["hd"]["id"]:
                        raise SafetyError("The standard profile changed identity")
                    if original and not _same_with_appended_formats(existing, original):
                        raise SafetyError("A profile changed after review; no owner policy was overwritten")
                    changed = {**deepcopy(existing), **desired}
                    if existing != changed:
                        call("qualityprofile/" + str(existing["id"]), "PUT", changed)
                        profiles_changed = True
                else:
                    call("qualityprofile", "POST", desired)
                    profiles_changed = True
                actual = media_profiles._unique(media_profiles._rows(call("qualityprofile")), desired["name"])
                if not actual or media_profiles._profile_policy(actual) != media_profiles._profile_policy(desired):
                    raise SafetyError("Starr did not retain a complete bounded request profile")
                profile_ids[kind] = actual["id"]
                save()
            for change in state["size_changes"]:
                original = next(r for r in state["definitions"] if r["id"] == change["id"])
                latest = next(r for r in media_profiles._rows(call("qualitydefinition")) if r["id"] == change["id"])
                if latest != original:
                    raise SafetyError("A global quality limit changed during request-policy setup")
                call("qualitydefinition/" + str(change["id"]), "PUT", change["resource"])
            for assignment in state["assignments"]:
                kind = "series" if app == "sonarr" else "movie"
                latest = call(kind + "/" + str(assignment["id"]))
                original = next(r for r in state["titles"] if r["id"] == assignment["id"])
                if _title_identity(latest) != _title_identity(original):
                    raise SafetyError("An anime title changed during policy review; its path and choice were kept")
                changed = {**deepcopy(latest), "qualityProfileId": profile_ids["anime_hd"]}
                call(kind + "/" + str(latest["id"]) + "?moveFiles=false", "PUT", changed)
                actual = call(kind + "/" + str(latest["id"]))
                expected = {**_title_identity(latest), "qualityProfileId": profile_ids["anime_hd"]}
                if _title_identity(actual) != expected:
                    raise SafetyError("Anime profile reassignment did not retain title identity and path")
            old_connection = inventory.connections[app][0]
            current = inventory.seerr("settings/" + app)
            if current != inventory.connections[app]:
                raise SafetyError("A Seerr connection changed during profile review")
            changed = {**deepcopy(old_connection), "activeProfileId": profile_ids["hd"],
                       "activeProfileName": inventory.names["hd"]}
            if app == "sonarr":
                changed.update(activeAnimeProfileId=profile_ids["anime_hd"], activeAnimeProfileName=inventory.names["anime_hd"])
            body = {k: v for k, v in changed.items() if k != "id"}
            if changed != old_connection:
                inventory.seerr("settings/" + app + "/" + str(old_connection["id"]), "PUT", body)
            if inventory.seerr("settings/" + app) != [changed]:
                raise SafetyError("Seerr did not preserve its other server settings during default-profile update")
            for assignment in state["collection_assignments"]:
                original = next(r for r in state["collections"] if r["id"] == assignment["id"])
                latest = call("collection/" + str(assignment["id"]))
                if latest != original:
                    raise SafetyError("A collection changed during review; its defaults were kept")
                changed = {**deepcopy(latest), "qualityProfileId": profile_ids[assignment["to_kind"]]}
                # Use per-ID PUT. Bulk PUT also queues RefreshCollections and
                # can change member monitoring; neither belongs in this task.
                call("collection/" + str(assignment["id"]), "PUT", changed)
                if call("collection/" + str(assignment["id"])) != changed:
                    raise SafetyError("A collection did not retain all other fields during profile migration")
            for old in state["retiring"]:
                titles = call("series" if app == "sonarr" else "movie")
                if any(t.get("qualityProfileId") == old["id"] for t in titles):
                    raise SafetyError("A factory profile became assigned; it was retained")
                references = call("importlist")
                if app == "radarr":
                    references = [*references, *call("collection")]
                if old["id"] in _profile_references(references):
                    raise SafetyError("An import list or collection now references a factory profile; it was retained")
                requests = inventory.seerr("request?take=1000&skip=0")
                if (not isinstance(requests, dict) or not isinstance(requests.get("results"), list)
                        or requests.get("pageInfo", {}).get("results") != len(requests["results"])
                        or any(r.get("profileId") == old["id"] for r in requests["results"])
                        or old["id"] in _profile_references(inventory.seerr("overrideRule"))
                        or old["id"] in _profile_references(inventory.seerr("settings/" + app))):
                    raise SafetyError("A request/default now references a factory profile; it was retained")
                actual = next((r for r in call("qualityprofile") if r["id"] == old["id"]), None)
                # New custom formats may have been appended automatically at zero.
                if actual is None or not _same_with_appended_formats(actual, old):
                    raise SafetyError("A factory profile changed during review; it was retained")
                if any(f.get("score") != 0 for f in actual["formatItems"]):
                    raise SafetyError("A factory profile has custom scoring; it was retained")
                call("qualityprofile/" + str(old["id"]), "DELETE")
                profiles_changed = True
                if any(r["id"] == old["id"] for r in call("qualityprofile")):
                    raise SafetyError("Starr did not retire the reviewed unused factory profile")
            dropdown_path = "service/" + app + "/" + str(old_connection["id"])
            expected = {identifier: inventory.names[kind] for kind, identifier in profile_ids.items()}
            if profiles_changed or not _dropdown_matches(inventory.seerr(dropdown_path), expected):
                # Seerr caches these lookups for five minutes. Reading Starr
                # alone can incorrectly report success while its request UI
                # still offers retired Any/Ultra-HD IDs.
                inventory.seerr("settings/cache/" + app + "/flush", "POST")
            if not _dropdown_matches(inventory.seerr(dropdown_path), expected):
                raise SafetyError("Seerr's native request dropdown did not retain exactly four current profiles")
        result = verify(c, include_global_sizes=include_sizes, runner=runner, request=request, secret_resolver=resolver)
        if not result["ok"]:
            raise SafetyError("Final four-choice request policy readback is incomplete")
        saved["state"] = "verified"
        save()
        return result


def verify(c, *, include_global_sizes=False, runner=None, request=None, secret_resolver=None):
    inventory = _inventory(c, include_global_sizes, runner, request, secret_resolver)
    missing, choices = [], {}
    for app, row in inventory.services.items():
        choices[app] = {kind: value["id"] if value else None for kind, value in row["existing_profiles"].items()}
        if (any(value is None for value in row["existing_profiles"].values()) or row["retiring"]
                or row["assignments"] or row["size_changes"]):
            missing.append(app)
        connection = inventory.connections[app][0]
        if (connection.get("activeProfileName") != inventory.names["hd"]
                or (app == "sonarr" and (connection.get("activeAnimeProfileId") != choices[app]["anime_hd"]
                     or connection.get("activeAnimeProfileName") != inventory.names["anime_hd"]))):
            missing.append(app + ":defaults")
        expected = {identifier: inventory.names[kind] for kind, identifier in choices[app].items() if identifier is not None}
        if not _dropdown_matches(inventory.seerr("service/" + app + "/" + str(connection["id"])), expected):
            missing.append(app + ":request-dropdown")
    return {"ok": not missing, "status": "verified" if not missing else "pending", "policy_version": VERSION,
            "profile_name": inventory.names["hd"], "profile_ids": {app: rows["hd"] for app, rows in choices.items()},
            "choices": choices, "profile_names": inventory.names, "missing": missing,
            "global_sizes": "verified" if include_global_sizes else "HD unchanged; bounded UHD verified",
            "label_updates_pending": 0, "limitations": LIMITATIONS}


def _dropdown_matches(response, expected):
    rows = response.get("profiles") if isinstance(response, dict) else None
    return (isinstance(rows, list) and len(rows) == 4 and len(expected) == 4
            and all(isinstance(row, dict) and type(row.get("id")) is int for row in rows)
            and {row["id"]: row.get("name") for row in rows} == expected)
