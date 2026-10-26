"""Conservative, idempotent application wiring with sanitized result reports.

Only installation-named resources are created. Existing resources are verified,
never repointed, deleted or adopted. Configure may replace an exact legacy
display label after proving its unchanged ID/settings; verify never renames.
Seerr's selected-profile display cache can follow its existing profile ID.
No media is requested.
"""

from __future__ import annotations

from copy import deepcopy
import json as jsonlib
from urllib.parse import urlencode

from .catalog import PORTS, role_host, stage_endpoints
from . import naming


class Pending(RuntimeError):
    """An operator enrollment or explicit policy choice is still required."""


class Conflict(RuntimeError):
    """Existing state differs from the proposed deployment; do not overwrite it."""


def _fields(resource: dict) -> dict:
    return {f["name"]: f.get("value") for f in resource.get("fields", [])}


def _same(actual, expected) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int):
        return str(actual) == str(expected)
    return str(actual).rstrip("/") == str(expected).rstrip("/")


def _update_display_fields(collection, item, updates, read, write, *, minimum_id=1):
    """Update proven display metadata, preserving IDs and every other field.

    A collection recheck immediately precedes the supported PUT. Compare the
    complete readback, including unrelated entries, because a vendor update
    can have side effects such as changing another server's default flag.
    """
    if (not isinstance(updates, dict) or not updates or not set(updates) <= {"name", "activeProfileName"}
            or any(not isinstance(value, str) or not value.strip() for value in updates.values())):
        raise Conflict("Display updates must contain only known, nonempty labels")
    identifier = item.get("id")
    if type(identifier) is not int or identifier < minimum_id:
        raise Conflict("The legacy display label has no unambiguous resource ID")
    identifiers = [row.get("id") for row in collection]
    if (any(type(value) is not int or value < minimum_id for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise Conflict("The resource collection has ambiguous IDs; no label was changed")
    latest = read()
    if latest != collection:
        raise Conflict("Application resources changed during label review; no label was changed")
    proposed = deepcopy(item)
    proposed.update(updates)
    write(identifier, proposed)
    saved = read()
    expected = [proposed if row["id"] == identifier else row for row in collection]
    if (not isinstance(saved, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int for row in saved)
            or sorted(saved, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"])):
        raise Conflict("Label update readback changed identity or settings; inspect the saved resource")
    return "display labels updated; original IDs and settings preserved"


def _rename_label(collection, item, name, read, write, *, minimum_id=1):
    return _update_display_fields(collection, item, {"name": name}, read, write, minimum_id=minimum_id)


def jellyfin_headers(token: str) -> dict[str, str]:
    """Current Jellyfin rejects the removed legacy X-Emby-Token header."""
    return {"Authorization": 'MediaBrowser Client="Samurai Stack", Device="Stack Installer", '
            'DeviceId="samurai-stack", Version="0.1", Token="' + token + '"'}


class _Run:
    def __init__(self, config, stage, request, secret_resolver, write):
        if request is None or secret_resolver is None:
            raise ValueError("A request transport and private secret resolver are required")
        self.config, self.stage = config, stage
        self.request, self.secret_resolver, self.write = request, secret_resolver, write
        self.report = {"stage": stage, "mode": "configure" if write else "verify", "checks": [],
                       "pending": [], "errors": [], "status": "pending", "ok": False}
        self.media = stage_endpoints(config, 2)

    def key(self, name):
        try:
            value = self.secret_resolver(name)
        except (OSError, KeyError, RuntimeError, ValueError):
            raise Pending(f"Provide private secret {name}; complete the service's first-run setup if needed") from None
        if not isinstance(value, str) or not value.strip():
            raise Pending(f"Provide private secret {name}")
        return value.rstrip("\r\n")

    def call(self, method, url, *, headers=None, json=None, form=None):
        options = {"headers": headers or {}, "timeout": 15}
        if json is not None:
            options["json"] = json
        if form is not None:
            options["form"] = form
        try:
            return self.request(method, url, **options)
        except Exception:
            # Vendor errors can echo API keys, passwords, bodies and full URLs.
            raise RuntimeError("API request failed; inspect the service locally") from None

    def api(self, service, path, method="GET", body=None):
        headers = {"X-Api-Key": self.key(service + "_api_key")}
        return self.call(method, self.media[service] + path, headers=headers, json=body)

    def step(self, name, fn):
        try:
            detail = fn()
            self.report["checks"].append({"name": name, "status": "passed", "detail": detail or "verified"})
        except Pending as exc:
            self.report["pending"].append({"name": name, "action": str(exc)})
        except Conflict as exc:
            self.report["errors"].append({"name": name, "error": str(exc)})
        except Exception:
            self.report["errors"].append({"name": name, "error": "API/schema verification failed; inspect the service locally"})

    def finish(self):
        if self.report["errors"]:
            self.report["status"] = "failed"
        elif self.report["pending"]:
            self.report["status"] = "pending"
        else:
            self.report["status"] = "configured" if self.write else "verified"
            self.report["ok"] = True
        return self.report

    def ensure(self, service, path, name, implementation, field_values, top_values=None, *, readback_only=False,
               legacy_names=()):
        """Create from the live schema; verify existing managed entries and tests."""
        resources = self.api(service, path)
        if not isinstance(resources, list) or any(not isinstance(row, dict) for row in resources):
            raise RuntimeError("Unexpected collection schema")
        labels = {name, *legacy_names}
        existing = [r for r in resources if r.get("name") in labels]
        if len(existing) > 1:
            raise Conflict("Duplicate deployment-named resources; resolve them in the application UI")
        if existing:
            item = existing[0]
            if str(item.get("implementation", "")).lower() != implementation.lower():
                raise Conflict("Installation-named resource has a different implementation; no settings were changed")
            values = _fields(item)
            for field, expected in field_values.items():
                # Vendor APIs commonly mask stored passwords; test the saved
                # resource with its ID so the vendor checks the stored secret.
                if field.lower() in {"password", "apikey"} and values.get(field) and set(str(values[field])) <= {"*"}:
                    continue
                if not _same(values.get(field), expected):
                    raise Conflict("Deployment-named resource differs; refusing to replace existing settings")
            for key, value in (top_values or {}).items():
                if not _same(item.get(key), value):
                    raise Conflict("Deployment-named resource policy differs; review it in the application UI")
            response = self.api(service, path + "/test", "POST", item)
            # Current Starr/Prowlarr return HTTP 200 with {}; older supported
            # releases return an empty list or no body. Nonempty errors fail.
            if response not in (None, [], {}):
                raise Conflict("Saved application connection test failed")
            if item.get("name") != name and self.write and not readback_only:
                return _rename_label(resources, item, name,
                    lambda: self.api(service, path),
                    lambda identifier, proposed: self.api(service, path + "/" + str(identifier), "PUT", proposed))
            return "existing resource and saved connection verified"
        if not self.write or readback_only:
            raise Conflict("Deployment resource is missing; run configure for this stage")
        schemas = self.api(service, path + "/schema")
        schema = next((s for s in schemas if str(s.get("implementation", "")).lower() == implementation.lower()), None)
        if schema is None:
            raise RuntimeError("Provider not available in live schema")
        item = deepcopy(schema)
        item.pop("id", None)
        item.update({"name": name, **(top_values or {})})
        present = {f["name"] for f in item.get("fields", [])}
        if not set(field_values).issubset(present):
            raise RuntimeError("Provider field schema has changed")
        for f in item.get("fields", []):
            if f["name"] in field_values:
                f["value"] = field_values[f["name"]]
        response = self.api(service, path + "/test", "POST", item)
        if response not in (None, [], {}):
            raise RuntimeError("Proposed connection test failed")
        self.api(service, path, "POST", item)
        # Read back the actual persisted object rather than trust a 201 alone.
        return self.ensure(service, path, name, implementation, field_values, top_values,
                           readback_only=True, legacy_names=legacy_names)


def _root_folder(r, service, media_type):
    path = f"/data/library/{media_type}"
    found = r.api(service, "/api/v3/rootfolder")
    if any(item.get("path", "").rstrip("/") == path for item in found):
        return "library root verified"
    if not r.write:
        raise Conflict("Library root is missing; run configure")
    r.api(service, "/api/v3/rootfolder", "POST", {"path": path})
    found = r.api(service, "/api/v3/rootfolder")
    if not any(item.get("path", "").rstrip("/") == path for item in found):
        raise RuntimeError("Library root was not persisted")
    return "library root created and verified"


def _hardlinks(r, service):
    """Change only the supported Starr copy/hardlink policy, never import files."""
    path = "/api/v3/config/mediamanagement"
    current = r.api(service, path)
    # Both upstream V3 MediaManagementConfigResource classes expose
    # CopyUsingHardlinks; ConfigController saves the resource with RestPutById.
    if (not isinstance(current, dict) or type(current.get("copyUsingHardlinks")) is not bool
            or type(current.get("id")) is not int or current["id"] < 1):
        raise RuntimeError("Unsupported Starr media-management schema")
    if current["copyUsingHardlinks"] is False:
        if not r.write:
            raise Conflict("Use Hardlinks instead of Copy is disabled; enable it before verifying")
        proposed = deepcopy(current)
        proposed["copyUsingHardlinks"] = True
        r.api(service, path + "/" + str(current["id"]), "PUT", proposed)
        saved = r.api(service, path)
        if (not isinstance(saved, dict) or type(saved.get("id")) is not int
                or saved["id"] != current["id"] or saved.get("copyUsingHardlinks") is not True):
            raise Conflict("Hardlink setting did not persist as enabled; inspect application settings")
    return "Use Hardlinks instead of Copy enabled and verified; filesystem/import behavior not tested"


def _sab(r):
    key = r.key("sab_api_key")
    def api(mode, **fields):
        query = urlencode({"output": "json", "apikey": key, "mode": mode, **fields})
        result = r.call("POST" if mode == "set_config" else "GET", r.media["sabnzbd"] + "/api?" + query)
        if isinstance(result, dict) and result.get("status") is False:
            raise RuntimeError("SABnzbd rejected the operation")
        return result
    data = api("get_config", section="misc").get("config", {}).get("misc", {})
    expected = {"download_dir": "/data/downloads/usenet/incomplete", "complete_dir": "/data/downloads/usenet/complete"}
    # Only default initial directories may be replaced automatically. Existing
    # non-default paths require an operator decision, even in our own project.
    defaults = {"download_dir": {"", "Downloads/incomplete", "/config/Downloads/incomplete", "/downloads/incomplete", "/incomplete-downloads"},
                "complete_dir": {"", "Downloads/complete", "/config/Downloads/complete", "/downloads/complete", "/downloads"}}
    for setting, value in expected.items():
        actual = str(data.get(setting, "")).rstrip("/")
        if actual == value:
            continue
        if not r.write:
            raise Conflict("SABnzbd download paths differ; run configure or review existing paths")
        if actual not in defaults[setting]:
            raise Conflict("SABnzbd has an existing non-default download path; refusing to repoint it")
        api("set_config", section="misc", keyword=setting, value=value)
    categories = api("get_config", section="categories").get("config", {}).get("categories", [])
    if isinstance(categories, dict):
        categories = list(categories.values())
    for category in ["movies", "tv"]:
        old = next((c for c in categories if c.get("name") == category), None)
        if old:
            if str(old.get("dir", "")) not in {category, ""}:
                raise Conflict("Existing SABnzbd category uses a different path; refusing to replace it")
        elif r.write:
            api("set_config", section="categories", name=category, dir=category, pp=3)
        else:
            raise Conflict("SABnzbd categories missing; run configure")
    check = api("get_config", section="misc").get("config", {}).get("misc", {})
    if not all(str(check.get(k, "")).rstrip("/") == v for k, v in expected.items()):
        raise RuntimeError("SABnzbd path readback differs")
    saved_categories = api("get_config", section="categories").get("config", {}).get("categories", [])
    if isinstance(saved_categories, dict):
        saved_categories = list(saved_categories.values())
    if not all(any(c.get("name") == name and str(c.get("dir", "")) in {name, ""} for c in saved_categories) for name in ["movies", "tv"]):
        raise RuntimeError("SABnzbd category readback differs")
    return "download directories and categories verified"


def _download_client(r, service, category, *, torrent=False):
    kind = "qBittorrent" if torrent else "SABnzbd"
    field = "movieCategory" if service == "radarr" else "tvCategory"
    if torrent:
        host = role_host(r.config, 3)
        values = {"host": host, "port": 8081, "useSsl": False, "urlBase": "",
                  "username": r.key("qbit_username"), "password": r.key("qbit_password"), field: category}
        implementation = "QBittorrent"
    else:
        host = role_host(r.config, 2)
        values = {"host": host, "port": 8080, "useSsl": False, "urlBase": "",
                  "apiKey": r.key("sab_api_key"), field: category}
        implementation = "Sabnzbd"
    preference = r.config.get("downloads", {}).get("prefer", "usenet")
    priority = 1 if (torrent and preference == "torrents") or (not torrent and preference == "usenet") else 10
    stage = 3 if torrent else 2
    label = naming.service_label(r.config, "qbittorrent" if torrent else "sabnzbd", stage)
    return r.ensure(service, "/api/v3/downloadclient", label, implementation,
                    values, {"enable": True, "priority": priority, "removeCompletedDownloads": False,
                             "removeFailedDownloads": False},
                    legacy_names=(f"Samurai {r.config['deployment_name']} {kind}",))


def _protocol_preference(r, service):
    """Client priority alone cannot choose between torrent and Usenet releases."""
    preferred = r.config.get("downloads", {}).get("prefer", "usenet")
    protocol = "torrent" if preferred == "torrents" else "usenet"
    rows = r.api(service, "/api/v3/delayprofile")
    defaults = [item for item in rows if not item.get("tags")]
    if len(defaults) != 1:
        raise Conflict("Starr has ambiguous delay profiles; choose protocol preference in its UI")
    item = defaults[0]
    if item.get("preferredProtocol") == protocol:
        return "default release protocol preference verified"
    if not r.write:
        raise Conflict("Starr protocol preference differs; run configure or review its delay profiles")
    if len(rows) != 1 or any(item.get(k, 0) not in {0, False, None} for k in ["usenetDelay", "torrentDelay"]):
        raise Conflict("Custom Starr delay policy exists; refusing to overwrite its protocol preference")
    changed = {**item, "preferredProtocol": protocol}
    r.api(service, "/api/v3/delayprofile/" + str(item["id"]), "PUT", changed)
    saved = r.api(service, "/api/v3/delayprofile")
    if not any(i.get("id") == item["id"] and i.get("preferredProtocol") == protocol for i in saved):
        raise RuntimeError("Starr protocol preference readback failed")
    return "default release protocol preference configured and verified"


def _prowlarr_link(r, service):
    values = {"prowlarrUrl": r.media["prowlarr"], "baseUrl": r.media[service],
              "apiKey": r.key(service + "_api_key")}
    return r.ensure("prowlarr", "/api/v1/applications", naming.service_label(r.config, service, 2),
                    service, values, {"syncLevel": "fullSync"},
                    legacy_names=(f"Samurai {r.config['deployment_name']} {service.title()}",))


def _bazarr(r):
    headers = {"X-API-KEY": r.key("bazarr_api_key")}
    url = r.media["bazarr"] + "/api/system/settings"
    settings = r.call("GET", url, headers=headers)
    form, connections = {}, {}
    for service in ["sonarr", "radarr"]:
        current = settings.get(service, {})
        expected = {"ip": role_host(r.config, 2), "port": PORTS[service], "base_url": "/",
                    "ssl": False, "apikey": r.key(service + "_api_key")}
        connections[service] = expected
        if current.get("apikey") and current.get("apikey") != expected["apikey"]:
            raise Conflict("Bazarr already has a different Starr credential; refusing to overwrite it")
        for key, value in expected.items():
            if not _same(current.get(key), value):
                form[f"settings-{service}-{key}"] = str(value).lower() if isinstance(value, bool) else str(value)
        if not settings.get("general", {}).get("use_" + service):
            form["settings-general-use_" + service] = "true"
    if form:
        if not r.write:
            raise Conflict("Bazarr Starr connections differ; run configure")
        r.call("POST", url, headers=headers, form=form)
        settings = r.call("GET", url, headers=headers)
    for service in ["sonarr", "radarr"]:
        current = settings.get(service, {})
        expected = connections[service]
        # Check every persisted connection field. A successful POST can still
        # ignore a key or retain SSL/base-URL settings for a different target.
        # Keys must match exactly; URL normalization must not trim credentials.
        if (settings.get("general", {}).get("use_" + service) is not True
                or current.get("ip") != expected["ip"]
                or not _same(current.get("port"), expected["port"])
                or not _same(current.get("base_url"), expected["base_url"])
                or current.get("ssl") is not expected["ssl"]
                or current.get("apikey") != expected["apikey"]):
            raise RuntimeError("Bazarr connection readback failed")
    return "Sonarr and Radarr connections verified; subtitle provider/language choice remains in Bazarr"


def _jellyfin(r):
    from .seerr_libraries import _canonical_library
    headers = jellyfin_headers(r.key("jellyfin_api_key"))
    base = r.media["jellyfin"]
    info = r.call("GET", base + "/System/Info", headers=headers)
    if not info.get("Id"):
        raise Pending("Complete Jellyfin administrator onboarding and save its API key")
    libraries = r.call("GET", base + "/Library/VirtualFolders", headers=headers)
    folders = r.call("GET", base + "/Library/MediaFolders", headers=headers)
    missing = []
    for suffix, media_type in [("Movies", "movies"), ("TV", "tvshows")]:
        folder = "movies" if suffix == "Movies" else "tv"
        name, path = suffix + " | " + naming.project_name(r.config, 2), "/data/library/" + folder
        # Check the old reserved label too: changing defaults must not make a
        # legacy name pointing elsewhere look like permission to create a copy.
        kind = "movie" if suffix == "Movies" else "show"
        _canonical_library(libraries, folders, f"Samurai {r.config['deployment_name']} {suffix}", path, media_type, kind)
        if _canonical_library(libraries, folders, name, path, media_type, kind) is None:
            missing.append((name, path, media_type))
    # Validate both existing library identities before creating either missing
    # collection. A conflicting TV row must not first create a movie duplicate.
    if missing and not r.write:
        raise Conflict("Jellyfin canonical library missing; run configure")
    for name, path, media_type in missing:
        query = urlencode({"name": name, "collectionType": media_type, "refreshLibrary": "false"})
        r.call("POST", base + "/Library/VirtualFolders?" + query, headers=headers,
               json={"LibraryOptions": {"PathInfos": [{"Path": path}], "SaveLocalMetadata": False,
                                        "EnableRealtimeMonitor": False, "EnableInternetProviders": True}})
    # Fetch once more so a successful POST is not confused with persisted state.
    libraries = r.call("GET", base + "/Library/VirtualFolders", headers=headers)
    folders = r.call("GET", base + "/Library/MediaFolders", headers=headers)
    for suffix, path, collection, kind in (("Movies", "/data/library/movies", "movies", "movie"),
                                           ("TV", "/data/library/tv", "tvshows", "show")):
        if _canonical_library(libraries, folders, suffix + " | " + naming.project_name(r.config, 2),
                              path, collection, kind) is None:
            raise RuntimeError("Jellyfin library readback failed")
    return "administrator API access and read-only movie/TV library paths verified"


def _jellyfin_refresh(r, service):
    """Use native import events to refresh Jellyfin, including NFS libraries."""
    return r.ensure(service, "/api/v3/notification", naming.service_label(r.config, "jellyfin", 2),
        "MediaBrowser", {"host": role_host(r.config, 2), "port": PORTS["jellyfin"], "useSsl": False,
            "urlBase": "", "apiKey": r.key("jellyfin_api_key"), "notify": False, "updateLibrary": True,
            "mapFrom": "", "mapTo": ""}, {"onDownload": True, "onUpgrade": True, "onRename": True},
        legacy_names=("Samurai " + r.config["deployment_name"] + " Jellyfin",))


def _seerr(r, service, *, readback_only=False):
    key = r.key("seerr_api_key")
    headers = {"X-Api-Key": key}
    base = r.media["seerr"] + "/api/v1/settings/" + service
    current = r.call("GET", base, headers=headers)
    if not isinstance(current, list) or any(not isinstance(row, dict) for row in current):
        raise Conflict("Seerr server settings have an unknown schema")
    name = naming.service_label(r.config, service, 2)
    legacy_name = f"Samurai {r.config['deployment_name']} {service.title()}"
    saved = [item for item in current if item.get("name") in {name, legacy_name}]
    if saved:
        item = saved[0]
        path = "/data/library/" + ("movies" if service == "radarr" else "tv")
        if (len(saved) != 1 or item.get("hostname") != role_host(r.config, 2)
                or int(item.get("port", 0)) != PORTS[service] or item.get("activeDirectory") != path
                or item.get("isDefault") is not True or item.get("useSsl") is not False
                or str(item.get("baseUrl", "")).strip("/") != ""):
            raise Conflict("Deployment-named Seerr server differs; refusing to replace it")
        test = r.call("POST", base + "/test", headers=headers, json=item)
        if not isinstance(test, dict) or not isinstance(test.get("profiles"), list) or not isinstance(test.get("rootFolders"), list):
            raise RuntimeError("Unexpected Seerr server test response")
        profiles = [p for p in test["profiles"] if isinstance(p, dict) and p.get("id") == item.get("activeProfileId")]
        if not profiles:
            raise Conflict("Seerr's selected quality profile no longer exists")
        if (len(profiles) != 1 or not isinstance(profiles[0].get("name"), str) or not profiles[0]["name"].strip()):
            raise Conflict("Seerr's selected quality profile has ambiguous display metadata")
        if not any(p.get("path", "").rstrip("/") == path for p in test["rootFolders"]):
            raise Conflict("Seerr's selected library root is unavailable")
        display_updates = {}
        if item.get("name") != name:
            display_updates["name"] = name
        if item.get("activeProfileName") != profiles[0]["name"]:
            display_updates["activeProfileName"] = profiles[0]["name"]
        if display_updates and r.write and not readback_only:
            # Seerr's PUT clears other defaults for this same 4K/non-4K type.
            # Refuse inconsistent pre-existing defaults instead of repairing
            # that unrelated policy as a side effect of a display rename.
            if any(row is not item and row.get("isDefault") is True
                   and row.get("is4k") == item.get("is4k") for row in current):
                raise Conflict("Seerr has ambiguous same-type defaults; no label was changed")
            return _update_display_fields(current, item, display_updates,
                lambda: r.call("GET", base, headers=headers),
                # The OpenAPI request validator marks id read-only. Keep it
                # in the comparison object and URL, never in the PUT body.
                lambda identifier, proposed: r.call("PUT", base + "/" + str(identifier), headers=headers,
                                                    json={key: value for key, value in proposed.items() if key != "id"}),
                minimum_id=0)
        if "activeProfileName" in display_updates:
            return "saved Seerr server and connection verified; selected profile display cache is stale, configure refreshes its name only"
        return "saved Seerr server and connection verified"
    if readback_only:
        raise Conflict("Seerr creation returned without a persisted server; no duplicate was created")
    if not r.write:
        raise Pending("Complete Seerr first-run Jellyfin enrollment, then run configure")
    if current:
        raise Conflict("Seerr already has a different server; choose its default deliberately in the UI")
    item = {"name": name, "hostname": role_host(r.config, 2), "port": PORTS[service],
            "apiKey": r.key(service + "_api_key"), "useSsl": False, "baseUrl": "",
            "is4k": False, "isDefault": True, "syncEnabled": True, "preventSearch": False,
            "tagRequests": False, "tags": [], "overrideRule": [],
            "activeDirectory": "/data/library/" + ("movies" if service == "radarr" else "tv")}
    test = r.call("POST", base + "/test", headers=headers, json=item)
    if not isinstance(test, dict) or not isinstance(test.get("profiles"), list) or not isinstance(test.get("rootFolders"), list):
        raise RuntimeError("Unexpected Seerr server test response")
    profiles = test.get("profiles", [])
    from . import media_profiles
    preferred = [media_profiles.profile_name(r.config), media_profiles.legacy_profile_name(r.config),
                 "HD-1080p", "HD - 1080p", "Any"]
    profile = next((p for title in preferred for p in profiles if p.get("name") == title), None)
    if profile is None and len(profiles) == 1:
        profile = profiles[0]
    if profile is None:
        raise Pending("Choose a quality profile for this Seerr server in the UI; no arbitrary profile was selected")
    item.update({"activeProfileId": profile["id"], "activeProfileName": profile["name"]})
    if service == "radarr":
        item["minimumAvailability"] = "released"
    else:
        item.update({"seriesType": "standard", "animeSeriesType": "anime", "enableSeasonFolders": True,
                     "monitorNewItems": "all"})
    r.call("POST", base, headers=headers, json=item)
    return _seerr(r, service, readback_only=True)


def _seerr_jellyfin(r):
    headers = {"X-Api-Key": r.key("seerr_api_key")}
    base = r.media["seerr"] + "/api/v1/settings"
    public = r.call("GET", base + "/public")
    if not isinstance(public, dict) or public.get("initialized") is not True:
        raise Pending("Complete Seerr's first-run Jellyfin administrator sign-in in its LAN UI")
    settings = r.call("GET", base + "/jellyfin", headers=headers)
    if (settings.get("ip") != role_host(r.config, 2) or int(settings.get("port", 0)) != 8096
            or settings.get("useSsl", False) is not False
            or str(settings.get("urlBase", "")).strip("/")):
        raise Conflict("Seerr's Jellyfin connection differs; review its media-server settings in the UI")
    info = r.call("GET", r.media["jellyfin"] + "/System/Info", headers=jellyfin_headers(r.key("jellyfin_api_key")))
    if not info.get("Id") or settings.get("serverId") != info["Id"]:
        raise Conflict("Seerr and Jellyfin server identities do not match")
    return "Seerr enrollment and expected Jellyfin server identity verified"


def _frontend(r, url):
    response = r.call("GET", url)
    if not isinstance(response, str) or "<html" not in response.lower():
        raise RuntimeError("Expected frontend document unavailable")
    return "web frontend responded; account enrollment requires the separate checklist"


QBIT_PREFERENCES = {
    "current_network_interface": "tun0", "up_limit": 0, "dl_limit": 0,
    "max_uploads": -1, "max_uploads_per_torrent": -1, "upnp": False, "random_port": False,
    "bypass_local_auth": False, "bypass_auth_subnet_whitelist_enabled": False,
    "web_ui_upnp": False, "web_ui_csrf_protection_enabled": True,
    "web_ui_clickjacking_protection_enabled": True, "web_ui_host_header_validation_enabled": True,
    "save_path": "/data/downloads/torrents/manual", "temp_path_enabled": True,
    "temp_path": "/data/downloads/torrents/incomplete", "auto_tmm_enabled": False,
    "use_category_paths_in_manual_mode": True,
    "autorun_enabled": False, "autorun_on_torrent_added_enabled": False,
}


def _qbit(r):
    base = stage_endpoints(r.config, 3)["qbittorrent"]
    headers = {"Referer": base + "/", "Origin": base}
    r.call("POST", base + "/api/v2/auth/login", headers=headers,
           form={"username": r.key("qbit_username"), "password": r.key("qbit_password")})
    def api(path, method="GET", form=None):
        return r.call(method, base + "/api/v2/" + path, headers=headers, form=form)
    prefs = api("app/preferences")
    if prefs.get("current_network_interface") != "tun0":
        raise Conflict("qBittorrent is not bound to tun0; stop commissioning and inspect the VPN bootstrap")
    different = {k: v for k, v in QBIT_PREFERENCES.items() if not _same(prefs.get(k), v)}
    if different:
        if not r.write:
            raise Conflict("qBittorrent safety/path/bandwidth settings differ; inspect before configuring")
        if api("torrents/info") and any(k in different for k in ["save_path", "temp_path", "auto_tmm_enabled", "use_category_paths_in_manual_mode"]):
            raise Conflict("Existing torrents use different storage defaults; refusing to change their path policy")
        api("app/setPreferences", "POST", {"json": jsonlib.dumps(different)})
    paths = {"movies": "/data/downloads/torrents/movies", "tv": "/data/downloads/torrents/tv",
             "manual": "/data/downloads/torrents/manual"}
    cats = api("torrents/categories")
    for name, path in paths.items():
        if name in cats:
            if not _same(cats[name].get("savePath"), path):
                raise Conflict("Existing torrent category points elsewhere; refusing to replace it")
        elif r.write:
            api("torrents/createCategory", "POST", {"category": name, "savePath": path})
        else:
            raise Conflict("Torrent category missing; run configure")
    prefs = api("app/preferences")
    if not all(_same(prefs.get(k), v) for k, v in QBIT_PREFERENCES.items()):
        raise RuntimeError("qBittorrent preference readback differs")
    cats = api("torrents/categories")
    if not all(name in cats and _same(cats[name].get("savePath"), path) for name, path in paths.items()):
        raise RuntimeError("Torrent category readback differs")
    return "authenticated preferences and categories verified; tun0 bound, limits uncapped, no authorization bypass"


def _run(config, stage, request, secret_resolver, write):
    r = _Run(config, stage, request, secret_resolver, write)
    if stage == 2:
        for service, folder in [("radarr", "movies"), ("sonarr", "tv")]:
            r.step(service + ":hardlinks", lambda s=service: _hardlinks(r, s))
            r.step(service + ":root", lambda s=service, f=folder: _root_folder(r, s, f))
            r.step("prowlarr:" + service, lambda s=service: _prowlarr_link(r, s))
            r.step(service + ":protocol-preference", lambda s=service: _protocol_preference(r, s))
        if config.get("features", {}).get("usenet", True):
            r.step("sabnzbd:paths", lambda: _sab(r))
            for service, category in [("radarr", "movies"), ("sonarr", "tv")]:
                r.step(service + ":sabnzbd", lambda s=service, c=category: _download_client(r, s, c))
        r.step("bazarr:starr", lambda: _bazarr(r))
        r.step("jellyfin:libraries", lambda: _jellyfin(r))
        for service in ("sonarr", "radarr"):
            r.step(service + ":jellyfin-refresh", lambda s=service: _jellyfin_refresh(r, s))
        r.step("seerr:jellyfin", lambda: _seerr_jellyfin(r))
        from .seerr_libraries import configure as configure_libraries, verify as verify_libraries
        r.step("seerr:libraries", lambda: (configure_libraries if r.write else verify_libraries)(r))
        for service in ["radarr", "sonarr"]:
            r.step("seerr:" + service, lambda s=service: _seerr(r, s))
    elif stage == 3 and config.get("features", {}).get("torrents", True):
        r.step("qbittorrent:preferences", lambda: _qbit(r))
        if r.report["errors"] or r.report["pending"]:
            return r.finish()  # Never register an unverified torrent backend.
        for service, category in [("radarr", "movies"), ("sonarr", "tv")]:
            r.step(service + ":hardlinks", lambda s=service: _hardlinks(r, s))
            r.step(service + ":qbittorrent", lambda s=service, c=category: _download_client(r, s, c, torrent=True))
            r.step(service + ":protocol-preference", lambda s=service: _protocol_preference(r, s))
    elif stage == 4:
        endpoints = stage_endpoints(config, 4)
        for service in ["dockge", "uptime-kuma"]:
            # Direct frontend routes avoid sending credential-bearing transports
            # through root redirects. No redirect following is needed here.
            path = "/dashboard" if service == "uptime-kuma" else "/"
            r.step("management:" + service + "-http", lambda s=service, p=path: _frontend(r, endpoints[s] + p))
        from . import management_setup
        if management_setup.has_enrollment(config):
            r.step("management:accounts-and-monitors", lambda: management_setup.verify(config))
        else:
            r.report["pending"].append({"name": "management:enrollment",
                "action": "Run ./setup 5 for backend account enrollment and listener monitors. Existing owners must authenticate; no accounts are reset."})
        if config.get("features", {}).get("homarr", False):
            r.report["pending"].append({"name": "homarr:enrollment", "action": "Complete optional Homarr enrollment in its private LAN UI."})
    elif stage == 5 and config.get("features", {}).get("photos", False):
        endpoint = stage_endpoints(config, 5)["immich-server"]
        def photo():
            pong = r.call("GET", endpoint + "/api/server/ping")
            if pong.get("res") != "pong":
                raise RuntimeError("Immich ping failed")
            config_result = r.call("GET", endpoint + "/api/server/config")
            if config_result.get("isInitialized") is not True or config_result.get("isOnboarded") is not True:
                raise Pending("Create the first Immich administrator, configure a backup, then verify again")
            if config_result.get("maintenanceMode") is True:
                raise Pending("Immich is in maintenance mode; finish its maintenance before verification")
            return "Immich API ready and first administrator enrollment recorded"
        r.step("immich:api", photo)
    elif stage == 6 and config.get("features", {}).get("music", False):
        endpoints = stage_endpoints(config, 6)
        r.step("music:navidrome-http", lambda: _frontend(r, endpoints["navidrome"] + "/app/"))
        def soulseek():
            data = r.call("GET", endpoints["slskd"] + "/api/v0/server", headers={"X-API-Key": r.key("slskd_api_key")})
            if not isinstance(data, dict) or data.get("isConnected") is not True or data.get("isLoggedIn") is not True:
                raise Pending("Soulseek is not connected and signed in; check its private account credentials in slskd")
            return "Soulseek connection and sign-in verified"
        r.step("music:soulseek-session", soulseek)
        r.report["pending"].append({"name": "music:enrollment",
                                    "action": "Create the first Navidrome administrator; set Soulseek credentials through private secret inputs. SoulSync is intentionally not installed. Music Assistant, if enabled, needs its own administrator and player/provider enrollment."})
    elif stage not in {2, 3, 4, 5, 6}:
        raise ValueError("Application stages are 2 through 6")
    return r.finish()


def configure_stage(config, stage: int, request=None, secret_resolver=None) -> dict:
    return _run(config, stage, request, secret_resolver, True)


def verify_stage(config, stage: int, request=None, secret_resolver=None) -> dict:
    return _run(config, stage, request, secret_resolver, False)


def _run_hardlinks(config, request, secret_resolver, write):
    r = _Run(config, 2, request, secret_resolver, write)
    r.report["scope"] = "hardlinks-only"
    for service in ("radarr", "sonarr"):
        r.step(service + ":hardlinks", lambda s=service: _hardlinks(r, s))
    return r.finish()


def configure_hardlinks(config, request=None, secret_resolver=None) -> dict:
    """Enable two import-policy flags only; no downloads, imports or media I/O."""
    return _run_hardlinks(config, request, secret_resolver, True)


def verify_hardlinks(config, request=None, secret_resolver=None) -> dict:
    """Read back the two hardlink flags without changing application settings."""
    return _run_hardlinks(config, request, secret_resolver, False)
