"""Optional additive Prowlarr source intake, separate from process health.

API keys live only in private retry inputs, never in site.json, reviewed plans
or reports. Existing sources are tested and preserved; only a reviewed new
Newznab/Torznab resource can be created. No searches, grabs or account resets.

Contracts: Prowlarr ProviderControllerBase schema/test/create and IndexerResource
capabilities; category 5070 is Newznab TV/Anime. Capabilities are discovered by
Prowlarr, not supplied or inferred from a broad TV parent category.
"""
from copy import deepcopy
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from . import catalog, engine, onboarding
from .safety import SafetyError, digest, no_symlinks, write_json

PRESETS = {"nzbgeek": {"implementation": "Newznab", "label": "NZBGeek",
                        "base_url": "https://api.nzbgeek.info", "api_path": "/api"}}
IMPLEMENTATIONS = {"Newznab": "usenet", "Torznab": "torrent"}
MAX_SOURCES = 128


def _binding(c):
    return {"deployment_id": c["deployment_id"], "host_id": engine.host_identity(),
            "install_root": c["install_root"], "bind_address": c["bind_address"]}


def _paths(c):
    onboarding._guard(c)
    root, _ = onboarding._paths(c)
    return root, root / "state/indexer-setup.json"


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise SafetyError("Choose an existing private indexer input ID") from None
    return value


def _source(value):
    allowed = {"preset", "implementation", "label", "base_url", "api_path", "api_key", "app_profile_id", "priority"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise SafetyError("Indexer input has unsupported fields")
    preset = value.get("preset")
    if preset is not None and preset not in PRESETS:
        raise SafetyError("Unknown indexer preset")
    result = {**PRESETS.get(preset, {}), **{k: v for k, v in value.items() if k != "preset"}}
    implementation = result.get("implementation")
    if implementation not in IMPLEMENTATIONS:
        raise SafetyError("Choose Newznab or Torznab source intake")
    label = result.get("label")
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", label):
        raise SafetyError("Indexer label must be 1-64 simple letters, digits, spaces, dots, hyphens or underscores")
    try:
        url = result.get("base_url")
        if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\s\\]", url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.port == 0
                or any(part in {".", ".."} for part in parsed.path.split("/"))
                or "%" in parsed.netloc):
            raise ValueError()
        result["base_url"] = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    except (ValueError, TypeError):
        raise SafetyError("Use an HTTPS indexer base URL without credentials, query parameters or a fragment") from None
    path = result.get("api_path", "/api")
    if (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,127}", path)
            or not path.rstrip("/") or any(part in {".", ".."} for part in path.split("/"))):
        raise SafetyError("Use a simple absolute indexer API path, usually /api")
    result["api_path"] = path.rstrip("/")
    key = result.get("api_key")
    if (not isinstance(key, str) or len(key) > 512 or key != key.strip()
            or any(ord(char) < 33 or ord(char) > 126 for char in key)
            or key and not key.strip("*")):
        raise SafetyError("Enter the actual indexer API key in hidden private input, not a masked key")
    if implementation == "Newznab" and not key:
        raise SafetyError("A Newznab API key is required for this intake; existing public sources can still be verified")
    profile = result.get("app_profile_id")
    if profile is not None and (type(profile) is not int or profile < 1):
        raise SafetyError("Choose a positive existing Prowlarr application-profile ID")
    priority = result.get("priority", 25)
    if type(priority) is not int or not 1 <= priority <= 50:
        raise SafetyError("Indexer priority must be 1-50")
    return {"implementation": implementation, "label": label, "base_url": result["base_url"],
            "api_path": result["api_path"], "api_key": key, "app_profile_id": profile, "priority": priority}


def _input_path(root, input_id):
    return no_symlinks(root / "secrets" / ("indexer-input-" + _id(input_id) + ".json"))


def save_input(c, source):
    """Save hidden intake supplied by the wizard, returning only its opaque ID.

    This local private write does not add an indexer or authorize a vendor write.
    Reuse the returned ID for retries; credentials never become CLI arguments.
    """
    source = _source(source)
    root, _ = _paths(c)
    input_id = str(uuid.uuid4())
    write_json(_input_path(root, input_id), {"schema_version": 1, **_binding(c), "source": source})
    return input_id


def _load_input(c, root, input_id):
    path = _input_path(root, input_id)
    value = onboarding._private_json(path)
    if value.get("schema_version") != 1 or any(value.get(k) != v for k, v in _binding(c).items()):
        raise SafetyError("Private indexer input belongs to another host or installation")
    return _source(value.get("source"))


def saved_inputs(c):
    """Display retry choices without API keys or credential-bearing URLs."""
    root, _ = _paths(c)
    paths = sorted((root / "secrets").glob("indexer-input-*.json"))
    if len(paths) > 32:
        raise SafetyError("Too many saved indexer inputs; review the private retry files")
    result = []
    for path in paths:
        input_id = path.name[len("indexer-input-"):-len(".json")]
        source = _load_input(c, root, input_id)
        result.append({"input_id": input_id, **{k: v for k, v in source.items() if k != "api_key"}})
    return result


def _journal(c, path):
    if not path.exists():
        return {"schema_version": 1, **_binding(c), "inputs": {}}
    value = onboarding._private_json(path)
    if (value.get("schema_version") != 1 or not isinstance(value.get("inputs"), dict)
            or len(value["inputs"]) > 32 or any(value.get(k) != v for k, v in _binding(c).items())):
        raise SafetyError("Indexer setup receipt belongs to another host or installation")
    return value


class _Api:
    def __init__(self, c, request, resolver):
        self.base = catalog.stage_endpoints(c, 2)["prowlarr"]
        self.request, self.key = request, resolver("prowlarr_api_key")
        if not isinstance(self.key, str) or not self.key.strip():
            raise SafetyError("The owned Prowlarr API key is required")

    def call(self, method, path, body=None):
        try:
            options = {"headers": {"X-Api-Key": self.key.rstrip("\r\n")}, "timeout": 30}
            if body is not None:
                options["json"] = body
            return self.request(method, self.base + "/api/v1" + path, **options)
        except Exception:
            raise SafetyError("Prowlarr source request failed; inspect the service locally. No existing source was replaced") from None


def _rows(value, *, ids=True, limit=MAX_SOURCES):
    if (not isinstance(value, list) or len(value) > limit
            or any(not isinstance(row, dict) for row in value)):
        raise SafetyError("Prowlarr returned an unsupported source/profile collection")
    if ids and (any(type(row.get("id")) is not int or row["id"] < 1 for row in value)
                or len({row["id"] for row in value}) != len(value)):
        raise SafetyError("Prowlarr resource identities are invalid or ambiguous")
    return value


def _fields(row):
    values = row.get("fields")
    if (not isinstance(values, list) or len(values) > 128
            or any(not isinstance(field, dict) or not isinstance(field.get("name"), str) for field in values)
            or len({field["name"] for field in values}) != len(values)):
        raise SafetyError("Prowlarr returned an unsupported source-field schema")
    return {field["name"]: field.get("value") for field in values}


def _policy(row):
    # Do not bind review hashes to transient health/status timestamps. Bind all
    # settings and custom fields so unrelated operator edits still invalidate it.
    keys = ("id", "name", "implementation", "configContract", "enable", "protocol", "priority",
            "appProfileId", "redirect", "downloadClientId", "tags")
    return {**{key: row.get(key) for key in keys}, "fields": _fields(row)}


def _snapshot_digest(rows):
    return digest([_policy(row) for row in sorted(rows, key=lambda row: row["id"])])


def _categories(row):
    capabilities = row.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("categories"), list):
        return []
    found = set()
    def visit(rows, depth=0):
        if depth > 8 or not isinstance(rows, list) or len(rows) > 256:
            raise SafetyError("Prowlarr category capabilities exceed the supported bounds")
        for item in rows:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                raise SafetyError("Prowlarr returned invalid category capabilities")
            found.add(item["id"])
            visit(item.get("subCategories", []), depth + 1)
    visit(capabilities["categories"])
    return sorted(found)


def _display_name(row):
    name = row.get("name")
    return name if isinstance(name, str) and re.fullmatch(r"[\w .()&+-]{1,128}", name) else "Source " + str(row["id"])


def _test(api, row):
    response = api.call("POST", "/indexer/test", row)
    if response not in (None, {}, [], "{}"):
        raise SafetyError("Prowlarr did not confirm the source connection test")


def _readiness(c, api, rows, profiles):
    profile_map = {row["id"]: row for row in profiles}
    sources, protocols = [], {}
    for protocol in IMPLEMENTATIONS.values():
        protocols[protocol] = {"tested_sources": 0, "movies": False, "tv": False, "anime": False}
    for row in rows:
        if type(row.get("enable")) is not bool:
            raise SafetyError("Prowlarr source enable state is unknown; no readiness was assumed")
        item = {"id": row["id"], "name": _display_name(row), "protocol": row.get("protocol"),
                "enabled": row["enable"], "status": "disabled", "categories": _categories(row)}
        if item["protocol"] not in protocols:
            item["protocol"] = "unknown"
        if row["enable"]:
            try:
                _test(api, row)
                profile = profile_map.get(row.get("appProfileId"), {})
                searchable = row.get("supportsSearch") is True and profile.get("enableAutomaticSearch") is True
                item["status"] = "passed" if searchable and item["protocol"] != "unknown" else "search-disabled"
                if item["status"] == "passed":
                    supported = protocols[item["protocol"]]
                    supported["tested_sources"] += 1
                    supported["movies"] |= any(2000 <= category < 3000 for category in item["categories"])
                    supported["tv"] |= any(5000 <= category < 6000 for category in item["categories"])
                    supported["anime"] |= 5070 in item["categories"]
            except SafetyError:
                item["status"] = "failed"
        sources.append(item)
    required = [protocol for protocol, feature in (("usenet", "usenet"), ("torrent", "torrents"))
                if c["features"].get(feature)]
    missing = [protocol for protocol in required if not protocols[protocol]["tested_sources"]]
    enabled = [row for row in sources if row["enabled"]]
    missing_categories = [kind for kind in ("movies", "tv")
                          if not any(protocols[protocol][kind] for protocol in required)]
    ok = bool(enabled) and not missing and not missing_categories and all(row["status"] == "passed" for row in enabled)
    return {"ok": ok, "status": "verified" if ok else "pending", "sources": sources, "protocols": protocols,
            "missing_protocols": missing, "missing_categories": missing_categories, "actual_download_tested": False,
            "scope": "Source authentication/search capability only; downloads, application sync and VPN safety are separate checks"}


def _profile(profiles, wanted):
    eligible = [row for row in profiles if row.get("enableAutomaticSearch") is True
                and row.get("enableInteractiveSearch") is True]
    if wanted is not None:
        eligible = [row for row in eligible if row["id"] == wanted]
    if len(eligible) != 1:
        raise SafetyError("Choose one existing Prowlarr profile with automatic and interactive searches enabled")
    return eligible[0]["id"]


def _desired(c, api, source, profiles):
    feature = "usenet" if source["implementation"] == "Newznab" else "torrents"
    if not c["features"].get(feature):
        raise SafetyError("Enable this download protocol before adding its indexer source")
    rows = _rows(api.call("GET", "/indexer/schema"), ids=False, limit=2048)
    candidates = []
    for row in rows:
        presets = row.get("presets") or []
        candidates.extend([row, *_rows(presets, ids=False)])
    name = "Generic " + source["implementation"]
    matches = [row for row in candidates if row.get("implementation") == source["implementation"] and row.get("name") == name]
    if len(matches) != 1:
        raise SafetyError("This Prowlarr version lacks an unambiguous generic indexer template")
    desired = deepcopy(matches[0])
    desired.pop("id", None)
    desired.pop("presets", None)
    fields = _fields(desired)
    if not {"baseUrl", "apiPath", "apiKey"} <= set(fields):
        raise SafetyError("This Prowlarr version lacks required Newznab/Torznab fields")
    if desired.get("protocol") != IMPLEMENTATIONS[source["implementation"]] or not desired.get("configContract"):
        raise SafetyError("Prowlarr generic indexer protocol/schema differs from the reviewed intake")
    desired.update(name="Samurai " + c["deployment_name"] + " " + source["label"], enable=True,
                   appProfileId=_profile(profiles, source["app_profile_id"]), priority=source["priority"],
                   downloadClientId=0, tags=[])
    if desired["protocol"] == "usenet" and desired.get("supportsRedirect") is True:
        desired["redirect"] = True
    replacements = {"baseUrl": source["base_url"], "apiPath": source["api_path"], "apiKey": source["api_key"]}
    for field in desired["fields"]:
        if field["name"] in replacements:
            field["value"] = replacements[field["name"]]
    return desired


def _matches(row, desired):
    actual, expected = _policy(row), _policy(desired)
    actual.pop("id"); expected.pop("id")
    # A masked saved key is tested by ID with the original stored secret.
    key = actual["fields"].get("apiKey")
    if isinstance(key, str) and key and not key.strip("*"):
        actual["fields"]["apiKey"] = expected["fields"]["apiKey"]
    return actual == expected


def _operation(c, runner, request, secret_resolver):
    root, path = _paths(c)
    engine.check_container_ownership(c, 2, runner)
    onboarding.check_targets(c, ["prowlarr"], runner)
    return root, path, _Api(c, request, secret_resolver or engine.secret_resolver(c))


def _plan(c, input_id, runner, request, secret_resolver):
    root, path, api = _operation(c, runner, request, secret_resolver)
    journal = _journal(c, path)
    source = _load_input(c, root, input_id) if input_id is not None else None
    rows, profiles = _rows(api.call("GET", "/indexer")), _rows(api.call("GET", "/appprofile"))
    report = _readiness(c, api, rows, profiles)
    desired, action = None, None
    if input_id is not None:
        desired = _desired(c, api, source, profiles)
        previous = journal["inputs"].get(input_id)
        if previous and previous.get("input_digest") != digest(source):
            raise SafetyError("Private indexer retry input changed after creation intent; no source was replaced")
        matches = [row for row in rows if row.get("name", "").casefold() == desired["name"].casefold()]
        if len(matches) > 1:
            raise SafetyError("Indexer resource names are ambiguous; no source was adopted")
        if matches:
            if not previous or previous.get("state") not in {"intent", "verified"} or not _matches(matches[0], desired):
                raise SafetyError("A deployment-named indexer already exists without matching saved intent; it was preserved")
            if previous.get("resource_id") not in (None, matches[0]["id"]):
                raise SafetyError("The saved indexer resource identity changed; it was preserved")
            _test(api, matches[0])
            action = {"mode": "verify-existing", "resource_id": matches[0]["id"]}
        else:
            if previous and previous.get("state") == "verified":
                raise SafetyError("The previously verified indexer is missing; it will not be recreated automatically")
            for row in rows:
                fields = _fields(row)
                if (row.get("implementation") == desired["implementation"]
                        and str(fields.get("baseUrl", "")).rstrip("/") == source["base_url"]
                        and str(fields.get("apiPath", "")).rstrip("/") == source["api_path"]):
                    raise SafetyError("This indexer endpoint already exists; test and preserve that source instead of adding a duplicate")
            _test(api, desired)
            action = {"mode": "create", "resource_id": None}
        action.update(input_id=input_id, name=desired["name"], implementation=source["implementation"],
                      base_url=source["base_url"], api_path=source["api_path"], app_profile_id=desired["appProfileId"],
                      input_digest=digest(source), desired_digest=digest(_policy(desired)))
    # Test endpoints may update transient health, never source settings. Refuse
    # any concurrent operator setting edit before publishing a reviewable plan.
    if _snapshot_digest(_rows(api.call("GET", "/indexer"))) != _snapshot_digest(rows):
        raise SafetyError("Indexer settings changed during inspection; review a fresh plan")
    engine.check_container_ownership(c, 2, runner)
    document = {"schema_version": 1, "kind": "indexer-source-intake", **_binding(c), "config_digest": digest(c),
                "sources_digest": _snapshot_digest(rows), "profiles_digest": digest(profiles),
                "journal_digest": digest(journal), "action": action, "readiness": report,
                "app_profiles": [{k: row.get(k) for k in ("id", "name", "enableRss", "enableAutomaticSearch", "enableInteractiveSearch")}
                                 for row in profiles]}
    document["review_hash"] = digest(document)
    return document, rows, desired, journal, path, api


def plan(c, input_id=None, *, runner=None, request=None, secret_resolver=None):
    """Test sources read-only and optionally review one private additive input."""
    return _plan(c, input_id, runner or engine.run, request or engine.request, secret_resolver)[0]


def verify(c, *, runner=None, request=None, secret_resolver=None):
    """No configuration writes; zero enabled sources cannot pass verification."""
    return plan(c, runner=runner, request=request, secret_resolver=secret_resolver)["readiness"]


def apply(c, reviewed_plan, *, runner=None, request=None, secret_resolver=None):
    """Apply one exact additive plan, retaining durable intent after API errors."""
    if not isinstance(reviewed_plan, dict) or reviewed_plan.get("kind") != "indexer-source-intake":
        raise SafetyError("Review an indexer source plan before applying")
    runner, request = runner or engine.run, request or engine.request
    _paths(c)
    with engine.deployment_lock(c):
        action = reviewed_plan.get("action")
        input_id = action.get("input_id") if isinstance(action, dict) else None
        current, before, desired, journal, path, api = _plan(c, input_id, runner, request, secret_resolver)
        if current != reviewed_plan:
            raise SafetyError("Indexer source plan changed after review; no source was written")
        if current["action"] is None:
            return current["readiness"]
        action = current["action"]
        if action["mode"] == "create":
            journal["inputs"][input_id] = {"state": "intent", "input_digest": action["input_digest"],
                                          "desired_digest": action["desired_digest"]}
            write_json(path, journal, replace=path.exists())
            engine.check_container_ownership(c, 2, runner)
            if _snapshot_digest(_rows(api.call("GET", "/indexer"))) != current["sources_digest"]:
                raise SafetyError("Indexer sources changed before creation; retained private intent, no source was replaced")
            _test(api, desired)
            api.call("POST", "/indexer", desired)
        after = _rows(api.call("GET", "/indexer"))
        matches = [row for row in after if row.get("name") == desired["name"]]
        if len(matches) != 1 or not _matches(matches[0], desired):
            raise SafetyError("Saved indexer fields did not match the reviewed input; private retry state was retained")
        added = matches[0]
        if action["resource_id"] not in (None, added["id"]):
            raise SafetyError("Indexer resource ID changed during verification; no source was replaced")
        others_before = [row for row in before if row["id"] != added["id"]]
        others_after = [row for row in after if row["id"] != added["id"]]
        if _snapshot_digest(others_before) != _snapshot_digest(others_after):
            raise SafetyError("Other indexer settings changed during creation; inspect them before continuing")
        _test(api, added)
        engine.check_container_ownership(c, 2, runner)
        journal["inputs"][input_id] = {"state": "verified", "input_digest": action["input_digest"],
                                      "desired_digest": action["desired_digest"], "resource_id": added["id"]}
        write_json(path, journal, replace=path.exists())
        if _journal(c, path) != journal:
            raise SafetyError("Indexer receipt readback failed; private input and receipt were retained")
    return verify(c, runner=runner, request=request, secret_resolver=secret_resolver)
