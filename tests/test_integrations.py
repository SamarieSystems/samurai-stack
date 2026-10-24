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
        self.sab = {"download_dir": "Downloads/incomplete", "complete_dir": "Downloads/complete"}
        self.sab_categories = []
        self.bazarr = {"general": {}, "sonarr": {}, "radarr": {}}
        self.libraries = []
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
                return {"Id": "new-installation"}
            if path == "/Library/VirtualFolders":
                if method == "POST" and not self.ignore_writes:
                    self.libraries.append({"Name": parse_qs(uri.query)["name"][0], "Locations": [json["LibraryOptions"]["PathInfos"][0]["Path"]]})
                return copy.deepcopy(self.libraries)
        if service == "seerr":
            if path.endswith("/settings/public"):
                return {"initialized": True}
            if path.endswith("/settings/jellyfin"):
                return {"ip": "192.168.1.50", "port": 8096, "useSsl": False, "serverId": "new-installation"}
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
        before = copy.deepcopy((vendor.collections, vendor.roots, vendor.libraries, vendor.sab_categories))
        second = integrations.configure_stage(c, 2, request=vendor, secret_resolver=secrets)
        self.assertTrue(second["ok"], second)
        self.assertEqual(before, (vendor.collections, vendor.roots, vendor.libraries, vendor.sab_categories))
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
        self.assertFalse(any(m in {"PUT", "DELETE"} for m, *_ in vendor.calls))

    def test_existing_named_client_conflict_never_overwrites(self):
        vendor = VendorModel()
        vendor.collections[("radarr", "/api/v3/downloadclient")] = [{"name": "Samurai example SABnzbd", "fields": [{"name": "host", "value": "192.168.1.99"}]}]
        result = integrations.configure_stage(config(), 2, request=vendor, secret_resolver=secrets)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(e["name"] == "radarr:sabnzbd" for e in result["errors"]))
        self.assertFalse(any(m in {"PUT", "DELETE"} for m, *_ in vendor.calls))

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


if __name__ == "__main__":
    unittest.main()
