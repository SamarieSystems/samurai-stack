import copy
import json
import unittest
from urllib.parse import urlsplit, parse_qs

from samurai_stack import integrations
from test_catalog import config


class VendorModel:
    """Stateful vendor API double for retry, conflict and readback behavior."""
    def __init__(self):
        self.calls = []
        self.collections = {}
        self.roots = {"sonarr": [], "radarr": []}
        self.media_management = {s: {"id": 1, "copyUsingHardlinks": True, "recycleBin": "/existing/recycle",
                                    "setPermissionsLinux": False, "minimumFreeSpaceWhenImporting": 250,
                                    "unrelatedFutureSetting": {"enabled": False}} for s in ("sonarr", "radarr")}
        self.sab = {"download_dir": "Downloads/incomplete", "complete_dir": "Downloads/complete"}
        self.sab_categories = []
        self.bazarr = {"general": {}, "sonarr": {}, "radarr": {}}
        self.libraries = []
        self.next_library_id = 1
        self.jellyfin_info = {"Id": "new-installation", "ServerName": "Fixture Jellyfin"}
        self.seerr_jellyfin = {"ip": "192.168.1.50", "port": 8096, "useSsl": False,
            "serverId": "new-installation", "name": "Fixture Jellyfin", "urlBase": "", "libraries": [],
            "externalHostname": "https://example.invalid"}
        self.prefs = dict(integrations.QBIT_PREFERENCES)
        self.categories = {}
        self.delay_profiles = {s: [{"id": 1, "preferredProtocol": "usenet", "tags": [], "usenetDelay": 0, "torrentDelay": 0}] for s in ["sonarr", "radarr"]}
        self.torrents = []
        self.ignore_writes = False
        self.raise_secret = False
        self.bad_test_response = False

    def __call__(self, method, url, headers=None, json=None, timeout=None, form=None):
        self.calls.append((method, url, copy.deepcopy(json), copy.deepcopy(form)))
        if self.raise_secret:
            raise RuntimeError("HTTP failed with password=never-print-this")
        uri = urlsplit(url)
        ports = {8989: "sonarr", 7878: "radarr", 9696: "prowlarr", 6767: "bazarr", 5055: "seerr", 8096: "jellyfin", 8081: "qbit", 8080: "sab"}
        service, path = ports.get(uri.port), uri.path
        if path.startswith('/api/v3/config/mediamanagement'):
            if method == 'PUT' and not self.ignore_writes:
                self.media_management[service] = copy.deepcopy(json)
            return copy.deepcopy(self.media_management[service])
        if "/delayprofile" in path:
            if method == "PUT" and not self.ignore_writes:
                self.delay_profiles[service] = [copy.deepcopy(json)]
                return copy.deepcopy(json)
            return copy.deepcopy(self.delay_profiles[service])
        if uri.port in {5001, 3001, 4533}:
            return "<!doctype html><html><body>Application</body></html>"
        if service == "sab":
            q = {k: v[0] for k, v in parse_qs(uri.query).items()}
            if q["mode"] == "set_config" and not self.ignore_writes:
                if q["section"] == "misc":
                    self.sab[q["keyword"]] = q["value"]
                else:
                    self.sab_categories.append({"name": q["name"], "dir": q["dir"]})
            value = self.sab if q["section"] == "misc" else self.sab_categories
            return {"config": {q["section"]: copy.deepcopy(value)}}
        if service == "qbit":
            if path.endswith("/auth/login"):
                return "Ok."
            if path.endswith("/app/preferences"):
                return copy.deepcopy(self.prefs)
            if path.endswith("/app/setPreferences"):
                if not self.ignore_writes:
                    self.prefs.update(__import__("json").loads(form["json"]))
                return ""
            if path.endswith("/torrents/info"):
                return self.torrents
            if path.endswith("/torrents/categories"):
                return copy.deepcopy(self.categories)
            if path.endswith("/torrents/createCategory"):
                if not self.ignore_writes:
                    self.categories[form["category"]] = {"savePath": form["savePath"]}
                return ""
        if service == "bazarr":
            if method == "POST" and not self.ignore_writes:
                for key, value in form.items():
                    _, section, field = key.split("-", 2)
                    self.bazarr[section][field] = {"true": True, "false": False}.get(value, value)
            return copy.deepcopy(self.bazarr)
        if service == "jellyfin":
            if path == "/System/Info":
                return copy.deepcopy(self.jellyfin_info)
            if path == "/Library/VirtualFolders":
                if method == "POST" and not self.ignore_writes:
                    query = parse_qs(uri.query)
                    self.libraries.append({"Name": query["name"][0], "Locations": [json["LibraryOptions"]["PathInfos"][0]["Path"]],
                        "ItemId": format(self.next_library_id, "032x"), "CollectionType": query["collectionType"][0]})
                    self.next_library_id += 1
                return copy.deepcopy(self.libraries)
            if path == "/Library/MediaFolders":
                return {"Items": [{"Id": row["ItemId"], "Name": row["Name"], "Type": "CollectionFolder",
                    "CollectionType": row["CollectionType"]} for row in self.libraries
                    if "ItemId" in row and "CollectionType" in row]}
        if service == "seerr":
            if path.endswith("/settings/public"):
                return {"initialized": True}
            if path.endswith("/settings/jellyfin"):
                if method != "GET":
                    raise AssertionError("JellyfinSettings.libraries is read-only in the real OpenAPI request schema")
                return copy.deepcopy(self.seerr_jellyfin)
            if path == "/api/v1/settings/jellyfin/library/sync" and method == "POST":
                if json is not None or form is not None:
                    raise AssertionError("Library metadata discovery must not request a content scan")
                if not self.ignore_writes:
                    previous = {row["id"]: row for row in self.seerr_jellyfin["libraries"]}
                    rows = []
                    for library in self.libraries:
                        if not library.get("ItemId") or library.get("CollectionType") in {"music", "books", "musicvideos", "homevideos", "boxsets"}:
                            continue
                        old = previous.get(library["ItemId"], {})
                        row = {"id": library["ItemId"], "name": library["Name"],
                            "type": "movie" if library.get("CollectionType") == "movies" else "show", "enabled": old.get("enabled", False)}
                        if "lastScan" in old:
                            row["lastScan"] = old["lastScan"]
                        rows.append(row)
                    self.seerr_jellyfin["libraries"] = rows
                return copy.deepcopy(self.seerr_jellyfin["libraries"])
            if path.startswith("/api/v1/settings/jellyfin/library/") and method == "PUT":
                if json != {"enabled": True}:
                    raise AssertionError("Only a target library enabled flag may be changed")
                target = next(row for row in self.seerr_jellyfin["libraries"] if row["id"] == path.rsplit("/", 1)[-1])
                if not self.ignore_writes:
                    target["enabled"] = True
                return copy.deepcopy(target)
            if path.endswith("/test"):
                return {"profiles": [{"id": 1, "name": "HD-1080p"}],
                        "rootFolders": [{"path": "/data/library/movies"}, {"path": "/data/library/tv"}]}
            rows = self.collections.setdefault((service, path), [])
            if method == "POST" and not self.ignore_writes:
                rows.append({**copy.deepcopy(json), "id": len(rows) + 1})
            return copy.deepcopy(rows)
        if path.endswith("/rootfolder"):
            if method == "POST" and not self.ignore_writes:
                self.roots[service].append({"path": json["path"]})
            return copy.deepcopy(self.roots[service])
        if path.endswith("/test"):
            if self.bad_test_response:
                return {"error": "Schema changed"}
            return []
        if path.endswith("/schema"):
            impls = ["Sabnzbd", "QBittorrent"] if "downloadclient" in path else ["Radarr", "Sonarr"]
            fields = ["host", "port", "useSsl", "urlBase", "username", "password", "apiKey", "movieCategory", "tvCategory", "prowlarrUrl", "baseUrl"]
            return [{"implementation": i, "configContract": i + "Settings", "fields": [{"name": n, "value": None} for n in fields]} for i in impls]
        rows = self.collections.setdefault((service, path), [])
        if method == "POST" and not self.ignore_writes:
            rows.append({**copy.deepcopy(json), "id": len(rows) + 1})
        return copy.deepcopy(rows)


def secrets(name):
    return "test-only-placeholder-" + name


class IntegrationTests(unittest.TestCase):
    def test_new_seerr_connection_prefers_opted_in_profile_and_existing_choice_survives(self):
        vendor = VendorModel()
        c = config()
        managed = {'id': 42, 'name': 'Samurai ' + c['deployment_name'] + ' 1080p Original'}
        def transport(method, url, **kwargs):
            value = vendor(method, url, **kwargs)
            if urlsplit(url).port == 5055 and urlsplit(url).path.endswith('/test'):
                value['profiles'].append(managed)
            return value
        self.assertTrue(integrations.configure_stage(c, 2, request=transport, secret_resolver=secrets)['ok'])
        for service in ('sonarr', 'radarr'):
            row = vendor.collections[('seerr', '/api/v1/settings/' + service)][0]
            self.assertEqual(42, row['activeProfileId'])
            row.update(activeProfileId=1, activeProfileName='HD-1080p')
        self.assertTrue(integrations.configure_stage(c, 2, request=transport, secret_resolver=secrets)['ok'])
        for service in ('sonarr', 'radarr'):
            self.assertEqual(1, vendor.collections[('seerr', '/api/v1/settings/' + service)][0]['activeProfileId'])

    def test_current_starr_empty_object_connection_test_success_is_supported(self):
        vendor = VendorModel()
        def transport(method, url, **kwargs):
            if urlsplit(url).port in {7878, 8989, 9696} and urlsplit(url).path.endswith("/test"):
                return {}
            return vendor(method, url, **kwargs)
        result = integrations.configure_stage(config(), 2, request=transport, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        verified = integrations.verify_stage(config(), 2, request=transport, secret_resolver=secrets)
        self.assertTrue(verified["ok"], verified)

    def test_jellyfin_uses_standard_authorization_without_removed_legacy_header(self):
        seen = []
        vendor = VendorModel()
        def transport(method, url, **kwargs):
            if urlsplit(url).port == 8096:
                headers = kwargs.get("headers", {})
                self.assertNotIn("X-Emby-Token", headers)
                self.assertTrue(headers.get("Authorization", "").startswith("MediaBrowser "))
                self.assertIn('Token="' + secrets("jellyfin_api_key") + '"', headers["Authorization"])
                seen.append(urlsplit(url).path)
            return vendor(method, url, **kwargs)
        result = integrations.configure_stage(config(), 2, request=transport, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        self.assertIn("/System/Info", seen)
        self.assertIn("/Library/VirtualFolders", seen)

    def test_media_configure_retry_is_idempotent_and_then_verifies(self):
        vendor = VendorModel()
        c = config()
        first = integrations.configure_stage(c, 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(first["status"], "configured", first)
        before = copy.deepcopy((vendor.collections, vendor.roots, vendor.libraries, vendor.sab_categories, vendor.seerr_jellyfin))
        second = integrations.configure_stage(c, 2, request=vendor, secret_resolver=secrets)
        self.assertTrue(second["ok"], second)
        self.assertEqual(before, (vendor.collections, vendor.roots, vendor.libraries, vendor.sab_categories, vendor.seerr_jellyfin))
        vendor.calls.clear()
        verified = integrations.verify_stage(c, 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(verified["status"], "verified", verified)
        for method, url, body, form in vendor.calls:
            if method == "POST":
                self.assertTrue(url.endswith("/test"), url)
            self.assertNotIn(method, {"PUT", "DELETE"})

    def test_no_existing_root_is_deleted_or_repointed(self):
        vendor = VendorModel()
        vendor.roots["radarr"] = [{"path": "/existing/family-library"}]
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        self.assertIn({"path": "/existing/family-library"}, vendor.roots["radarr"])
        self.assertFalse(any(method == "DELETE" or method == "PUT" and "/rootfolder" in urlsplit(url).path
                             for method, url, *_ in vendor.calls))

    def test_existing_named_client_conflict_never_overwrites(self):
        vendor = VendorModel()
        vendor.collections[("radarr", "/api/v3/downloadclient")] = [{"name": "Samurai example SABnzbd", "fields": [{"name": "host", "value": "192.168.1.99"}]}]
        before = copy.deepcopy(vendor.collections[("radarr", "/api/v3/downloadclient")])
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(e["name"] == "radarr:sabnzbd" for e in result["errors"]))
        self.assertEqual(vendor.collections[("radarr", "/api/v3/downloadclient")], before)
        self.assertFalse(any(method == "DELETE" or method == "PUT" and "/downloadclient" in urlsplit(url).path
                             for method, url, *_ in vendor.calls))

    def test_vendor_secret_echoes_are_not_reported(self):
        vendor = VendorModel()
        vendor.raise_secret = True
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        text = json.dumps(result)
        self.assertNotIn("never-print-this", text)
        self.assertNotIn("test-only-placeholder", text)
        self.assertEqual(result["status"], "failed")

    def test_missing_secrets_are_pending_not_green(self):
        def missing(_):
            raise FileNotFoundError()
        result = integrations.configure_stage(config(), 2, request=VendorModel(), secret_resolver=missing)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending"])

    def test_a_success_status_without_persisted_settings_is_failure(self):
        vendor = VendorModel()
        vendor.ignore_writes = True
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        creations = [(url, body.get("name")) for method, url, body, _ in vendor.calls if method == "POST" and urlsplit(url).path in {"/api/v1/applications", "/api/v3/downloadclient", "/api/v1/settings/sonarr", "/api/v1/settings/radarr"}]
        self.assertEqual(len(creations), len(set(creations)))

    def test_unrecognized_connection_test_response_is_not_success(self):
        vendor = VendorModel()
        vendor.bad_test_response = True
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(items for (service, path), items in vendor.collections.items() if path == "/api/v3/downloadclient"))

    def test_seerr_managed_server_must_be_default(self):
        vendor = VendorModel()
        self.assertTrue(integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)["ok"])
        vendor.collections[("seerr", "/api/v1/settings/radarr")][0]["isDefault"] = False
        result = integrations.verify_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")

    def test_seerr_different_server_not_adopted(self):
        vendor = VendorModel()
        vendor.collections[("seerr", "/api/v1/settings/radarr")] = [{"name": "Unrelated", "hostname": "192.168.1.99"}]
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(vendor.collections[("seerr", "/api/v1/settings/radarr")]), 1)

    def test_full_media_wiring_enables_owned_seerr_libraries_and_preserves_other_selections(self):
        vendor = VendorModel()
        vendor.libraries = [
            {"Name": "Samurai example Movies", "Locations": ["/data/library/movies"], "ItemId": "a" * 32, "CollectionType": "movies"},
            {"Name": "Samurai example TV", "Locations": ["/data/library/tv"], "ItemId": "b" * 32, "CollectionType": "tvshows"},
        ]
        custom = [{"id": "c" * 32, "name": "Family movies", "type": "movie", "enabled": True,
                   "lastScan": 123, "futureSetting": {"keep": True}},
                  {"id": "stale-unrelated", "name": "Offline manual TV", "type": "show", "enabled": False}]
        vendor.seerr_jellyfin["libraries"] = copy.deepcopy(custom) + [
            {"id": row["ItemId"], "name": row["Name"], "type": "movie" if row["CollectionType"] == "movies" else "show", "enabled": False}
            for row in vendor.libraries]
        unrelated = {key: copy.deepcopy(value) for key, value in vendor.seerr_jellyfin.items() if key != "libraries"}
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        self.assertIn("seerr:libraries", {row["name"] for row in result["checks"]})
        expected = custom + [{"id": row["ItemId"], "name": row["Name"], "enabled": True,
                              "type": "movie" if row["CollectionType"] == "movies" else "show"}
                             for row in vendor.libraries]
        self.assertEqual(vendor.seerr_jellyfin["libraries"], expected)
        self.assertEqual({key: value for key, value in vendor.seerr_jellyfin.items() if key != "libraries"}, unrelated)
        writes = [(method, urlsplit(url).path, body) for method, url, body, _ in vendor.calls
                  if method != "GET" and urlsplit(url).path.startswith("/api/v1/settings/jellyfin")]
        self.assertEqual(writes, [("PUT", "/api/v1/settings/jellyfin/library/" + row["ItemId"], {"enabled": True})
                                 for row in vendor.libraries])
        self.assertFalse(any(urlsplit(url).path.endswith("/sync") or urlsplit(url).path == "/Library/Refresh"
                             for _, url, _, _ in vendor.calls))
        for method, url, _, _ in vendor.calls:
            if method == "POST" and urlsplit(url).path == "/Library/VirtualFolders":
                self.assertEqual(parse_qs(urlsplit(url).query).get("refreshLibrary"), ["false"])

    def test_full_media_discovery_enables_new_owned_rows_without_content_scan(self):
        vendor = VendorModel()
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        calls = [(method, urlsplit(url).path, body) for method, url, body, _ in vendor.calls
                 if method != "GET" and urlsplit(url).path.startswith("/api/v1/settings/jellyfin")]
        self.assertEqual(calls[0], ("POST", "/api/v1/settings/jellyfin/library/sync", None))
        self.assertEqual(calls[1:], [("PUT", "/api/v1/settings/jellyfin/library/" + row["ItemId"], {"enabled": True})
                                    for row in vendor.libraries])
        self.assertFalse(any(urlsplit(url).path in {"/api/v1/settings/jellyfin/sync", "/Library/Refresh"}
                             for _, url, _, _ in vendor.calls))

    def test_full_media_discovery_refuses_before_dropping_stale_custom_seerr_row(self):
        vendor = VendorModel()
        custom = [{"id": "stale-custom", "name": "Manual missing movies", "type": "movie", "enabled": True}]
        vendor.seerr_jellyfin["libraries"] = copy.deepcopy(custom)
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(row["name"] == "seerr:libraries" for row in result["errors"]), result)
        self.assertEqual(vendor.seerr_jellyfin["libraries"], custom)
        self.assertFalse(any(method != "GET" and urlsplit(url).path.startswith("/api/v1/settings/jellyfin")
                             for method, url, _, _ in vendor.calls))

    def test_full_readonly_media_verify_refuses_disabled_seerr_selection_without_fixing_it(self):
        vendor = VendorModel()
        self.assertTrue(integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)["ok"])
        vendor.seerr_jellyfin["libraries"][0]["enabled"] = False
        before = copy.deepcopy(vendor.seerr_jellyfin)
        vendor.calls.clear()
        result = integrations.verify_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(row["name"] == "seerr:libraries" for row in result["errors"]), result)
        self.assertEqual(vendor.seerr_jellyfin, before)
        self.assertFalse(any(method in {"PUT", "DELETE", "PATCH"} or method == "POST" and not url.endswith("/test")
                             for method, url, _, _ in vendor.calls))

    def test_custom_sab_download_path_is_preserved(self):
        vendor = VendorModel()
        vendor.sab["complete_dir"] = "/existing/documents"
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(vendor.sab["complete_dir"], "/existing/documents")

    def test_qbit_verifies_before_any_starr_registration(self):
        vendor = VendorModel()
        vendor.prefs["current_network_interface"] = "eth0"
        result = integrations.configure_stage(config(), 3, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(":7878" in url or ":8989" in url for _, url, *_ in vendor.calls))

    def test_qbit_existing_torrents_block_path_policy_changes(self):
        vendor = VendorModel()
        vendor.prefs["save_path"] = "/existing/games"
        vendor.torrents = [{"hash": "test"}]
        result = integrations.configure_stage(config(), 3, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(vendor.prefs["save_path"], "/existing/games")

    def test_qbit_new_install_uncaps_and_uses_separate_torrent_host(self):
        vendor = VendorModel()
        vendor.prefs["up_limit"] = 50000
        c = config()
        c["downloads"] = {"prefer": "torrents"}
        result = integrations.configure_stage(c, 3, request=vendor, secret_resolver=secrets)
        self.assertTrue(result["ok"], result)
        self.assertEqual(vendor.prefs["up_limit"], 0)
        for service in ["sonarr", "radarr"]:
            self.assertEqual(vendor.delay_profiles[service][0]["preferredProtocol"], "torrent")
            item = vendor.collections[(service, "/api/v3/downloadclient")][0]
            self.assertEqual(item["priority"], 1)
            self.assertEqual(next(f["value"] for f in item["fields"] if f["name"] == "host"), c["hosts"]["torrent"])
            self.assertFalse(item["removeCompletedDownloads"])

    def test_torrent_preference_preserves_custom_delay_policies(self):
        vendor = VendorModel()
        vendor.delay_profiles["radarr"][0]["usenetDelay"] = 30
        c = config()
        c["downloads"] = {"prefer": "torrents"}
        result = integrations.configure_stage(c, 3, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(vendor.delay_profiles["radarr"][0]["preferredProtocol"], "usenet")
        self.assertEqual(vendor.delay_profiles["radarr"][0]["usenetDelay"], 30)

    def test_management_does_not_claim_enrollment_verified(self):
        result = integrations.verify_stage(config(), 4, request=VendorModel(), secret_resolver=secrets)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["ok"])

    def test_password_spaces_are_preserved_through_the_transport(self):
        vendor = VendorModel()
        def resolve(name):
            return " leading and trailing spaces " if name == "qbit_password" else secrets(name)
        result = integrations.configure_stage(config(), 3, request=vendor, secret_resolver=resolve)
        self.assertTrue(result["ok"], result)
        login = next(form for method, url, body, form in vendor.calls if url.endswith("/auth/login"))
        self.assertEqual(login["password"], " leading and trailing spaces ")


class HardlinksIntegrationTests(unittest.TestCase):
    def vendor(self, enabled=False):
        vendor = VendorModel()
        for service in ('radarr', 'sonarr'):
            vendor.media_management[service]['copyUsingHardlinks'] = enabled
        return vendor

    def test_configure_changes_only_hardlink_flag_and_reads_back_both_services(self):
        vendor = self.vendor()
        before = copy.deepcopy(vendor.media_management)
        result = integrations.configure_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertEqual(('configured', 'hardlinks-only'), (result['status'], result['scope']))
        self.assertEqual({'radarr:hardlinks', 'sonarr:hardlinks'}, {item['name'] for item in result['checks']})
        for service in ('radarr', 'sonarr'):
            expected = dict(before[service], copyUsingHardlinks=True)
            self.assertEqual(expected, vendor.media_management[service])
        writes = [(method, url, body) for method, url, body, _ in vendor.calls if method != 'GET']
        self.assertEqual(2, len(writes))
        for method, url, body in writes:
            self.assertEqual('PUT', method)
            self.assertEqual('/api/v3/config/mediamanagement/1', urlsplit(url).path)
            service = 'radarr' if urlsplit(url).port == 7878 else 'sonarr'
            self.assertEqual(dict(before[service], copyUsingHardlinks=True), body)
        self.assertEqual(6, len(vendor.calls))
        self.assertTrue(all(urlsplit(url).path in {'/api/v3/config/mediamanagement', '/api/v3/config/mediamanagement/1'} for _, url, _, _ in vendor.calls))
        self.assertNotIn('/existing/recycle', json.dumps(result))

    def test_repeated_configure_and_verify_are_readonly_after_setting_is_enabled(self):
        vendor = self.vendor()
        self.assertTrue(integrations.configure_hardlinks(config(), request=vendor, secret_resolver=secrets)['ok'])
        vendor.calls.clear()
        again = integrations.configure_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertTrue(again['ok'])
        self.assertEqual(['GET', 'GET'], [call[0] for call in vendor.calls])
        vendor.calls.clear()
        verified = integrations.verify_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertEqual('verified', verified['status'])
        self.assertEqual(['GET', 'GET'], [call[0] for call in vendor.calls])

    def test_verify_disabled_setting_fails_without_writes(self):
        vendor = self.vendor()
        before = copy.deepcopy(vendor.media_management)
        result = integrations.verify_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['ok'])
        self.assertEqual(before, vendor.media_management)
        self.assertTrue(all(method == 'GET' for method, *_ in vendor.calls))
        self.assertEqual({'radarr:hardlinks', 'sonarr:hardlinks'}, {item['name'] for item in result['errors']})

    def test_successful_put_without_persisted_readback_is_failure(self):
        vendor = self.vendor(); vendor.ignore_writes = True
        result = integrations.configure_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertEqual('failed', result['status'])
        self.assertEqual(2, len(result['errors']))
        self.assertTrue(all(value['copyUsingHardlinks'] is False for value in vendor.media_management.values()))
        self.assertEqual(2, sum(method == 'PUT' for method, *_ in vendor.calls))

    def test_unknown_or_nonboolean_schema_refuses_before_writing(self):
        for value in (None, [], {}, {'id': 1, 'useHardlinks': True},
                      {'id': 1, 'copyUsingHardlinks': 'true'}, {'id': 1, 'copyUsingHardlinks': 1},
                      {'id': 1, 'copyUsingHardlinks': None}, {'id': '1', 'copyUsingHardlinks': False},
                      {'id': True, 'copyUsingHardlinks': False}, {'id': 0, 'copyUsingHardlinks': False}):
            for operation in (integrations.configure_hardlinks, integrations.verify_hardlinks):
                with self.subTest(value=value, operation=operation.__name__):
                    vendor = self.vendor()
                    vendor.media_management = {service: copy.deepcopy(value) for service in ('radarr', 'sonarr')}
                    result = operation(config(), request=vendor, secret_resolver=secrets)
                    self.assertEqual('failed', result['status'])
                    self.assertTrue(all(method == 'GET' for method, *_ in vendor.calls))

    def test_readback_requires_boolean_true_and_matching_resource_id(self):
        for readback in ({'id': 1, 'copyUsingHardlinks': 'true'}, {'id': 1, 'copyUsingHardlinks': 1},
                         {'id': 1}, {'id': 2, 'copyUsingHardlinks': True}):
            with self.subTest(readback=readback):
                vendor = self.vendor()
                def transport(method, url, **kwargs):
                    response = vendor(method, url, **kwargs)
                    service = 'radarr' if urlsplit(url).port == 7878 else 'sonarr'
                    if method == 'GET' and vendor.media_management[service]['copyUsingHardlinks'] is True:
                        return copy.deepcopy(readback)
                    return response
                result = integrations.configure_hardlinks(config(), request=transport, secret_resolver=secrets)
                self.assertEqual('failed', result['status'])

    def test_authentication_and_vendor_errors_remain_sanitized(self):
        vendor = self.vendor(); vendor.raise_secret = True
        result = integrations.configure_hardlinks(config(), request=vendor, secret_resolver=secrets)
        self.assertEqual('failed', result['status'])
        self.assertNotIn('never-print-this', json.dumps(result))
        self.assertNotIn('test-only-placeholder', json.dumps(result))
        missing = integrations.configure_hardlinks(config(), request=self.vendor(), secret_resolver=lambda _: '')
        self.assertEqual('pending', missing['status'])
        self.assertFalse(missing['ok'])

    def test_normal_media_and_torrent_entrypoints_include_both_hardlink_checks(self):
        for stage in (2, 3):
            with self.subTest(stage=stage):
                vendor = self.vendor()
                configured = integrations.configure_stage(config(), stage, request=vendor, secret_resolver=secrets)
                self.assertTrue(configured['ok'], configured)
                verified = integrations.verify_stage(config(), stage, request=vendor, secret_resolver=secrets)
                self.assertTrue(verified['ok'], verified)
                expected = {'radarr:hardlinks', 'sonarr:hardlinks'}
                self.assertTrue(expected.issubset({item['name'] for item in configured['checks']}))
                self.assertTrue(expected.issubset({item['name'] for item in verified['checks']}))


if __name__ == "__main__":
    unittest.main()
