"""Select only the deployment's two verified Jellyfin libraries in Seerr.

Pinned Seerr v3.5.1 contracts are server/routes/settings/index.ts and
server/lib/scanners/jellyfin/index.ts. New discovered libraries are disabled;
the scanner processes enabled libraries only. The supported library/sync route
discovers metadata without scanning content, but replaces the list. Before
using it for missing rows, prove every current selection will survive unchanged.
Otherwise refuse before writing. Already discovered rows need only the supported
individual library PUT. The broader settings schema marks libraries read-only.
No content scan, refresh or media request is started.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .catalog import role_host
from .integrations import Conflict, Pending, jellyfin_headers


def _text(value):
    return (isinstance(value, str) and bool(value) and len(value) <= 512
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _libraries(value):
    if not isinstance(value, list):
        raise Conflict("Seerr library schema is unknown; no selections were replaced")
    seen = set()
    for row in value:
        if (not isinstance(row, dict) or not _text(row.get("id"))
                or not _text(row.get("name")) or type(row.get("enabled")) is not bool
                or row.get("type") not in {"movie", "show"} or row["id"] in seen):
            raise Conflict("Seerr library identities or settings are ambiguous; no selections were replaced")
        seen.add(row["id"])
    return deepcopy(value)


def _settings(r, headers, info):
    value = r.call("GET", r.media["seerr"] + "/api/v1/settings/jellyfin", headers=headers)
    if (not isinstance(value, dict) or value.get("serverId") != info["Id"]
            or value.get("ip") != role_host(r.config, 2)
            or value.get("port") != 8096 or value.get("useSsl", False) is not False
            or str(value.get("urlBase", "")).strip("/")):
        raise Conflict("Seerr's Jellyfin connection differs; library selections were not replaced")
    return value, _libraries(value.get("libraries"))


def _canonical_library(virtual, folders, name, path, collection, kind):
    """Prove one canonical path/type/real ID, preserving its display name.

    A reserved name pointing elsewhere is still a conflict. Overlapping or
    mixed locations must never be mistaken for a missing library and cause
    duplicate registration of an existing collection.
    """
    if (not isinstance(virtual, list) or any(not isinstance(row, dict) for row in virtual)
            or not isinstance(folders, dict) or not isinstance(folders.get("Items"), list)
            or any(not isinstance(row, dict) for row in folders["Items"])):
        raise Conflict("Jellyfin library identities could not be verified; no library was changed")
    named, touching = [], []
    for row in virtual:
        locations = row.get("Locations")
        if not isinstance(locations, list) or any(not isinstance(value, str) for value in locations):
            raise Conflict("Jellyfin library locations are unknown; no library was changed")
        if row.get("Name") == name:
            named.append(row)
            if locations != [path]:
                raise Conflict("Deployment-named Jellyfin library points elsewhere; refusing to replace it")
        if any(value.rstrip("/") == path or value.rstrip("/").startswith(path + "/")
               or path.startswith(value.rstrip("/") + "/") for value in locations):
            touching.append(row)
    if len(named) > 1 or len(touching) > 1:
        raise Conflict("Canonical Jellyfin library paths are duplicated or overlapping; no library was changed")
    if not touching:
        return None
    row = touching[0]
    if row["Locations"] != [path]:
        raise Conflict("Canonical Jellyfin library uses mixed or overlapping locations; no library was changed")
    identifier = row.get("ItemId")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", identifier)
            or not _text(row.get("Name"))
            or row.get("CollectionType", collection) != collection
            or len([item for item in virtual if item.get("ItemId") == identifier]) != 1):
        raise Conflict("Canonical Jellyfin library identifier or media type is ambiguous")
    actual = [item for item in folders["Items"] if item.get("Id") == identifier]
    if (len(actual) != 1 or actual[0].get("Name") != row["Name"]
            or actual[0].get("Type") != "CollectionFolder" or actual[0].get("CollectionType") != collection):
        raise Conflict("Canonical Jellyfin library ID, display metadata or media type differs")
    return {"id": identifier, "name": row["Name"], "enabled": True, "type": kind}


def _managed(r, headers):
    """Use canonical paths, collection types and actual IDs, never names alone."""
    base = r.media["jellyfin"]
    virtual = r.call("GET", base + "/Library/VirtualFolders", headers=headers)
    folders = r.call("GET", base + "/Library/MediaFolders", headers=headers)
    if (not isinstance(virtual, list) or any(not isinstance(row, dict) for row in virtual)
            or not isinstance(folders, dict) or not isinstance(folders.get("Items"), list)
            or any(not isinstance(row, dict) for row in folders["Items"])):
        raise Conflict("Jellyfin library identities could not be verified; no Seerr selection was changed")
    discovered = []
    for row in folders["Items"]:
        if row.get("Type") != "CollectionFolder" or row.get("CollectionType") in {
            "music", "books", "musicvideos", "homevideos", "boxsets"
        }:
            continue
        if (not isinstance(row.get("Id"), str) or not re.fullmatch(r"[0-9a-fA-F]{32}", row["Id"])
                or not _text(row.get("Name")) or any(item["id"] == row["Id"] for item in discovered)):
            raise Conflict("Jellyfin discovery contains ambiguous library metadata")
        discovered.append({"id": row["Id"], "name": row["Name"],
                           "type": "movie" if row.get("CollectionType") == "movies" else "show"})
    desired = []
    for suffix, path, collection, kind in (
        ("Movies", "/data/library/movies", "movies", "movie"),
        ("TV", "/data/library/tv", "tvshows", "show"),
    ):
        name = "Samurai " + r.config["deployment_name"] + " " + suffix
        matched = _canonical_library(virtual, folders, name, path, collection, kind)
        if matched is None:
            raise Conflict("Canonical Jellyfin library is missing; no Seerr selection was changed")
        if any(row["id"] == matched["id"] for row in desired):
            raise Conflict("Managed Jellyfin library identifiers are duplicated")
        desired.append(matched)
    return desired, discovered


def _merged(current, desired):
    proposed = deepcopy(current)
    for wanted in desired:
        same_name = [row for row in current if row["name"] == wanted["name"]]
        if any(row["id"] != wanted["id"] for row in same_name):
            raise Conflict("Seerr has a different ID for a managed library; no selections were replaced")
        existing = [row for row in proposed if row["id"] == wanted["id"]]
        if existing:
            row = existing[0]
            if row["type"] != wanted["type"]:
                raise Conflict("Seerr managed library identity differs; no selections were replaced")
            # Display names may have been customized in either application.
            # The verified canonical Jellyfin ID/type establishes identity.
            row["enabled"] = True
        else:
            proposed.append(deepcopy(wanted))
    return proposed


def _by_id(rows):
    return {row["id"]: row for row in rows}


def _sync_preview(current, discovered, desired):
    """Predict the exact public metadata-sync operation before permitting it."""
    live = _by_id(discovered)
    old = _by_id(current)
    owned = {row["id"] for row in desired}
    for row in current:
        match = live.get(row["id"])
        if (match is None or match["type"] != row["type"]
                or match["name"] != row["name"] and row["id"] not in owned
                or set(row) - {"id", "name", "enabled", "type", "lastScan"}):
            raise Conflict("Seerr library discovery would alter an unrelated selection; sync was not started")
    proposed = []
    for item in discovered:
        row = {**item, "enabled": old.get(item["id"], {}).get("enabled", False)}
        if "lastScan" in old.get(item["id"], {}):
            row["lastScan"] = old[item["id"]]["lastScan"]
        proposed.append(row)
    return proposed


def _run(r, *, write):
    headers = {"X-Api-Key": r.key("seerr_api_key")}
    jf_headers = jellyfin_headers(r.key("jellyfin_api_key"))
    public = r.call("GET", r.media["seerr"] + "/api/v1/settings/public")
    if not isinstance(public, dict) or public.get("initialized") is not True:
        raise Pending("Complete guided Seerr account enrollment before selecting its Jellyfin libraries")
    info = r.call("GET", r.media["jellyfin"] + "/System/Info", headers=jf_headers)
    if not isinstance(info, dict) or not _text(info.get("Id")):
        raise Conflict("Jellyfin server identity could not be verified")
    _, current = _settings(r, headers, info)
    desired, discovered = _managed(r, jf_headers)
    proposed = _merged(current, desired)
    if proposed != current:
        if not write:
            raise Conflict("Managed Jellyfin libraries are not enabled in Seerr; run configure for this stage")
        _, latest_rows = _settings(r, headers, info)
        if latest_rows != current:
            raise Conflict("Seerr library selections changed during review; retry without replacing them")
        base = r.media["seerr"] + "/api/v1/settings/jellyfin/library"
        if any(row["id"] not in _by_id(current) for row in desired):
            expected = _sync_preview(current, discovered, desired)
            r.call("POST", base + "/sync", headers=headers)
            _, saved = _settings(r, headers, info)
            if _by_id(saved) != _by_id(expected):
                raise Conflict("Seerr library discovery readback differs; inspect its saved selections")
            current = saved
        for wanted in desired:
            row = _by_id(current).get(wanted["id"])
            if row is None:
                raise Conflict("Seerr did not discover the managed Jellyfin library")
            if row["enabled"] is True:
                continue
            _, latest_rows = _settings(r, headers, info)
            if _by_id(latest_rows) != _by_id(current):
                raise Conflict("Seerr library selections changed during review; retry without replacing them")
            expected = deepcopy(current)
            _by_id(expected)[wanted["id"]]["enabled"] = True
            r.call("PUT", base + "/" + wanted["id"], headers=headers, json={"enabled": True})
            _, saved = _settings(r, headers, info)
            if _by_id(saved) != _by_id(expected):
                raise Conflict("Seerr library selection readback differs; inspect its saved settings")
            current = saved
    return "Managed Jellyfin movie/TV libraries enabled in Seerr; unrelated selections preserved; no content scan started"


def configure(run):
    """Enable only the matched managed libraries, then verify saved selections."""
    return _run(run, write=run.write is True)


def verify(run):
    """Read-only proof of the managed selections and actual Jellyfin paths/IDs."""
    return _run(run, write=False)
