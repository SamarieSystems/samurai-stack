"""Pinned Seerr library metadata contracts; no live applications or media."""
import copy
import unittest
from urllib.parse import urlsplit

from samurai_stack import integrations, seerr_libraries
from test_catalog import config


MOVIES, TV, OTHER = "a" * 32, "b" * 32, "c" * 32


class Vendor:
    def __init__(self):
        self.calls = []
        self.settings = {"serverId": "fixture-server", "name": "Fixture Jellyfin",
                         "ip": "192.168.1.50", "port": 8096, "useSsl": False,
                         "urlBase": "", "libraries": [], "externalHostname": "https://example.invalid"}
        self.info = {"Id": "fixture-server", "ServerName": "Fixture Jellyfin"}
        self.virtual = [
            {"ItemId": MOVIES, "Name": "Samurai example Movies", "Locations": ["/data/library/movies"]},
            {"ItemId": TV, "Name": "Samurai example TV", "Locations": ["/data/library/tv"]},
        ]
        self.folders = {"Items": [
            {"Id": MOVIES, "Name": "Samurai example Movies", "Type": "CollectionFolder", "CollectionType": "movies"},
            {"Id": TV, "Name": "Samurai example TV", "Type": "CollectionFolder", "CollectionType": "tvshows"},
        ]}
        self.initialized = True
        self.ignore_write = False
        self.alter_unrelated = False
        self.change_during_review = False
        self.reads = 0

    def __call__(self, method, url, *, headers=None, json=None, timeout=None):
        path = urlsplit(url).path
        self.calls.append((method, path, copy.deepcopy(json)))
        if path == "/System/Info":
            return copy.deepcopy(self.info)
        if path == "/Library/VirtualFolders":
            return copy.deepcopy(self.virtual)
        if path == "/Library/MediaFolders":
            return copy.deepcopy(self.folders)
        if path == "/api/v1/settings/public":
            return {"initialized": self.initialized}
        if path == "/api/v1/settings/jellyfin":
            if method == "POST":
                raise AssertionError("JellyfinSettings.libraries is read-only in OpenAPI requests")
            else:
                self.reads += 1
                if self.reads == 2 and self.change_during_review:
                    self.settings["libraries"].append({"id": OTHER, "name": "Manual selection", "type": "movie", "enabled": False})
            return copy.deepcopy(self.settings)
        if path == "/api/v1/settings/jellyfin/library/sync" and method == "POST":
            self.assert_no_content_scan_body(json)
            if not self.ignore_write:
                previous = {row["id"]: row for row in self.settings["libraries"]}
                self.settings["libraries"] = []
                for folder in self.folders["Items"]:
                    if folder.get("Type") != "CollectionFolder" or folder.get("CollectionType") in {
                        "music", "books", "musicvideos", "homevideos", "boxsets"
                    }:
                        continue
                    old = previous.get(folder["Id"], {})
                    row = {"id": folder["Id"], "name": folder["Name"],
                           "type": "movie" if folder.get("CollectionType") == "movies" else "show",
                           "enabled": old.get("enabled", False)}
                    if "lastScan" in old:
                        row["lastScan"] = old["lastScan"]
                    self.settings["libraries"].append(row)
            return copy.deepcopy(self.settings["libraries"])
        if path.startswith("/api/v1/settings/jellyfin/library/") and method == "PUT":
            if json != {"enabled": True}:
                raise AssertionError("Only an explicit enabled boolean may be updated")
            if not self.ignore_write:
                identifier = path.rsplit("/", 1)[-1]
                row = next(row for row in self.settings["libraries"] if row["id"] == identifier)
                row["enabled"] = True
                if self.alter_unrelated:
                    self.settings["libraries"][0]["enabled"] = False
            return copy.deepcopy(self.settings["libraries"])
        raise AssertionError("Unexpected request")

    @staticmethod
    def assert_no_content_scan_body(value):
        if value is not None:
            raise AssertionError("Library metadata discovery has no content-scan body")

    def run(self, write=True):
        # All keys are generated test values, not service credentials.
        return integrations._Run(config(), 2, self, lambda name: "fixture-" + name, write)

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class SeerrLibraryTests(unittest.TestCase):
    def test_fresh_missing_libraries_enable_exact_managed_ids_without_scan(self):
        vendor = Vendor()
        detail = seerr_libraries.configure(vendor.run())
        self.assertIn("no content scan started", detail)
        self.assertEqual(vendor.writes(), [
            ("POST", "/api/v1/settings/jellyfin/library/sync", None),
            ("PUT", "/api/v1/settings/jellyfin/library/" + MOVIES, {"enabled": True}),
            ("PUT", "/api/v1/settings/jellyfin/library/" + TV, {"enabled": True}),
        ])
        self.assertEqual(vendor.settings["externalHostname"], "https://example.invalid")
        vendor.calls.clear()
        seerr_libraries.configure(vendor.run())
        seerr_libraries.verify(vendor.run())
        self.assertFalse(vendor.writes())

    def test_known_managed_rows_preserve_custom_and_stale_rows_without_sync(self):
        vendor = Vendor()
        custom = [
            {"id": OTHER, "name": "Unrelated custom", "type": "movie", "enabled": True,
             "lastScan": 31, "futureSetting": {"manual": True}},
            {"id": "stale-custom-id", "name": "Missing from Jellyfin", "type": "show", "enabled": False},
        ]
        vendor.settings["libraries"] = copy.deepcopy(custom) + [
            {"id": MOVIES, "name": "Samurai example Movies", "type": "movie", "enabled": False},
            {"id": TV, "name": "Samurai example TV", "type": "show", "enabled": False},
        ]
        seerr_libraries.configure(vendor.run())
        self.assertEqual(vendor.settings["libraries"][:2], custom)
        self.assertEqual(len(vendor.settings["libraries"]), 4)
        self.assertTrue(all(call[0] == "PUT" for call in vendor.writes()))

    def test_metadata_sync_preserves_existing_real_custom_selection(self):
        vendor = Vendor()
        custom = {"id": OTHER, "name": "Custom movies", "type": "movie", "enabled": True, "lastScan": 31}
        vendor.settings["libraries"] = [copy.deepcopy(custom)]
        vendor.folders["Items"].append({"Id": OTHER, "Name": "Custom movies", "Type": "CollectionFolder", "CollectionType": "movies"})
        seerr_libraries.configure(vendor.run())
        self.assertIn(custom, vendor.settings["libraries"])
        puts = [call[1] for call in vendor.writes() if call[0] == "PUT"]
        self.assertNotIn("/api/v1/settings/jellyfin/library/" + OTHER, puts)

    def test_metadata_sync_would_drop_custom_or_stale_metadata_refuses_before_write(self):
        rows = [
            {"id": OTHER, "name": "Missing library", "type": "movie", "enabled": True},
            {"id": MOVIES, "name": "Samurai example Movies", "type": "movie", "enabled": False,
             "futureSetting": True},
        ]
        for row in rows:
            with self.subTest(row=row):
                vendor = Vendor()
                vendor.settings["libraries"] = [copy.deepcopy(row)]
                with self.assertRaises(integrations.Conflict):
                    seerr_libraries.configure(vendor.run())
                self.assertFalse(vendor.writes())
                self.assertEqual(vendor.settings["libraries"], [row])

    def test_new_unrelated_discovered_libraries_stay_disabled(self):
        vendor = Vendor()
        vendor.folders["Items"].append({"Id": OTHER, "Name": "Custom movies", "Type": "CollectionFolder", "CollectionType": "movies"})
        seerr_libraries.configure(vendor.run())
        self.assertIs(next(row for row in vendor.settings["libraries"] if row["id"] == OTHER)["enabled"], False)

    def test_enables_managed_disabled_preserving_scan_metadata(self):
        vendor = Vendor()
        vendor.settings["libraries"] = [{"id": MOVIES, "name": "Samurai example Movies",
                                             "type": "movie", "enabled": False, "lastScan": 44}]
        seerr_libraries.configure(vendor.run())
        self.assertEqual(vendor.settings["libraries"][0]["lastScan"], 44)
        self.assertIs(vendor.settings["libraries"][0]["enabled"], True)

    def test_verify_and_readonly_context_never_write_missing_selection(self):
        for action in (seerr_libraries.verify, seerr_libraries.configure):
            with self.subTest(action=action.__name__):
                vendor = Vendor()
                with self.assertRaises(integrations.Conflict):
                    action(vendor.run(write=False))
                self.assertFalse(vendor.writes())

    def test_refuses_managed_name_path_id_type_and_server_mismatches(self):
        mutations = (
            lambda v: v.virtual[0].update(Locations=["/existing/photos"]),
            lambda v: v.virtual.append(copy.deepcopy(v.virtual[0])),
            lambda v: v.virtual[0].update(ItemId="not-a-jellyfin-id"),
            lambda v: v.folders["Items"][0].update(Id=OTHER),
            lambda v: v.folders["Items"][0].update(CollectionType="homevideos"),
            lambda v: v.folders["Items"][0].update(Name="Manual movies"),
            lambda v: v.folders["Items"][0].update(Type="Video"),
            lambda v: v.settings.update(serverId="different-server"),
            lambda v: v.settings.update(ip="192.168.1.99"),
            lambda v: v.settings.update(useSsl=True),
            lambda v: v.settings.update(urlBase="/different"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                vendor = Vendor()
                mutate(vendor)
                with self.assertRaises(integrations.Conflict):
                    seerr_libraries.configure(vendor.run())
                self.assertFalse(vendor.writes())

    def test_refuses_ambiguous_or_different_existing_managed_selections(self):
        rows = (
            [{"id": OTHER, "name": "Samurai example Movies", "type": "movie", "enabled": True}],
            [{"id": MOVIES, "name": "Manual name", "type": "movie", "enabled": True}],
            [{"id": MOVIES, "name": "Samurai example Movies", "type": "show", "enabled": True}],
            [{"id": OTHER, "name": "Custom", "type": "movie", "enabled": "false"}],
            [{"id": OTHER, "name": "Custom", "type": "movie", "enabled": True}] * 2,
            {},
        )
        for current in rows:
            with self.subTest(current=current):
                vendor = Vendor()
                vendor.settings["libraries"] = copy.deepcopy(current)
                with self.assertRaises(integrations.Conflict):
                    seerr_libraries.configure(vendor.run())
                self.assertFalse(vendor.writes())

    def test_initialization_needed_is_pending_without_writes(self):
        vendor = Vendor()
        vendor.initialized = False
        with self.assertRaises(integrations.Pending):
            seerr_libraries.configure(vendor.run())
        self.assertFalse(vendor.writes())

    def test_changed_snapshot_refuses_write(self):
        vendor = Vendor()
        vendor.change_during_review = True
        with self.assertRaises(integrations.Conflict):
            seerr_libraries.configure(vendor.run())
        self.assertFalse(vendor.writes())

    def test_success_response_without_persistence_fails_readback(self):
        vendor = Vendor()
        vendor.ignore_write = True
        with self.assertRaises(integrations.Conflict):
            seerr_libraries.configure(vendor.run())
        self.assertEqual(len(vendor.writes()), 1)

    def test_readback_checks_unrelated_selection_too(self):
        vendor = Vendor()
        vendor.settings["libraries"] = [
            {"id": OTHER, "name": "Custom", "type": "movie", "enabled": True},
            {"id": MOVIES, "name": "Samurai example Movies", "type": "movie", "enabled": False},
            {"id": TV, "name": "Samurai example TV", "type": "show", "enabled": False},
        ]
        vendor.alter_unrelated = True
        with self.assertRaises(integrations.Conflict):
            seerr_libraries.configure(vendor.run())

    def test_custom_server_display_name_is_not_overwritten(self):
        vendor = Vendor()
        vendor.settings["name"] = "My custom display name"
        seerr_libraries.configure(vendor.run())
        self.assertEqual(vendor.settings["name"], "My custom display name")


if __name__ == "__main__":
    unittest.main()
