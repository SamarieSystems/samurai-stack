import copy
import json
import unittest

from samurai_stack import catalog


def config():
    return {"deployment_name": "example", "install_root": "/opt/samurai", "bind_address": "192.168.1.50",
            "timezone": "Etc/UTC", "puid": 1000, "pgid": 1000,
            "storage": {"data_root": "/mnt/media/samurai"}, "hardware": {"acceleration": "cpu"},
            "hosts": {"media": "192.168.1.50", "torrent": "192.168.1.51", "management": "192.168.1.52",
                      "photos": "192.168.1.53", "music": "192.168.1.54"},
            "vpn": {"provider": "protonvpn", "type": "wireguard", "port_forwarding": True},
            "features": {"usenet": True, "torrents": True, "photos": True, "music": True, "dashboard": True}}


class CatalogTests(unittest.TestCase):
    def test_all_bind_sources_must_exist_and_no_global_names(self):
        c = config()
        for stage in range(2, 7):
            d = catalog.render_stage(c, stage)
            for name, service in d["services"].items():
                with self.subTest(stage=stage, service=name):
                    self.assertNotIn("container_name", service)
                    self.assertEqual(service["restart"], "no")
                    for mount in service.get("volumes", []):
                        self.assertIs(mount["bind"]["create_host_path"], False)

    def test_common_data_root_prevents_cross_filesystem_import_mappings(self):
        services = catalog.render_stage(config(), 2)["services"]
        for name in ["radarr", "sonarr", "sabnzbd"]:
            mappings = {v["target"]: v["source"] for v in services[name]["volumes"]}
            self.assertEqual(mappings["/data"], "/mnt/media/samurai")
        media = next(v for v in services["jellyfin"]["volumes"] if v["target"] == "/data/library")
        self.assertIs(media["read_only"], True)

    def test_torrent_has_no_independent_network_or_host_ports(self):
        services = catalog.render_stage(config(), 3)["services"]
        qbit = services["qbittorrent"]
        self.assertEqual(qbit["network_mode"], "service:gluetun")
        self.assertNotIn("ports", qbit)
        self.assertNotIn("networks", qbit)
        self.assertEqual(services["gluetun"]["ports"], ["192.168.1.51:8081:8081/tcp"])
        self.assertEqual(qbit["depends_on"]["gluetun"]["condition"], "service_healthy")

    def test_vpn_secrets_are_provider_and_protocol_specific(self):
        c = config()
        self.assertEqual(set(catalog.expected_secret_names(c, 3)), {"qbit_username", "qbit_password", "vpn_wireguard_private_key"})
        c["vpn"].update({"provider": "custom", "type": "wireguard", "port_forwarding": False})
        self.assertEqual(set(catalog.expected_secret_names(c, 3)), {"qbit_username", "qbit_password", "vpn_custom.conf"})
        mounts = catalog.render_stage(c, 3)["services"]["gluetun"]["volumes"]
        custom = next(v for v in mounts if v["target"] == "/gluetun/wireguard/wg0.conf")
        self.assertTrue(custom["read_only"])
        c["vpn"]["type"] = "openvpn"
        self.assertEqual(set(catalog.expected_secret_names(c, 3)), {"qbit_username", "qbit_password", "vpn_custom.conf", "vpn_openvpn_username", "vpn_openvpn_password"})

    def test_cpu_profile_never_requires_gpu(self):
        c = config()
        for stage in [2, 5]:
            for service in catalog.render_stage(c, stage)["services"].values():
                self.assertNotIn("devices", service)
                self.assertNotIn("deploy", service)
                self.assertNotIn("-cuda", service["image"])

    def test_hardware_acceleration_opt_in_and_host_groups(self):
        c = config()
        c["hardware"] = {"acceleration": "intel", "render_groups": [44, 109]}
        jellyfin = catalog.render_stage(c, 2)["services"]["jellyfin"]
        self.assertEqual(jellyfin["devices"], ["/dev/dri:/dev/dri"])
        self.assertEqual(jellyfin["group_add"], ["44", "109"])
        c["hardware"]["acceleration"] = "nvidia"
        jellyfin = catalog.render_stage(c, 2)["services"]["jellyfin"]
        self.assertIn("deploy", jellyfin)
        self.assertNotIn("devices", jellyfin)

    def test_external_photo_tree_readonly_database_local(self):
        services = catalog.render_stage(config(), 5)["services"]
        mounts = services["immich-server"]["volumes"]
        self.assertTrue(next(v for v in mounts if v["target"] == "/external")["read_only"])
        db_mount = services["database"]["volumes"][0]
        self.assertTrue(db_mount["source"].startswith("/opt/samurai/appdata/"))
        self.assertNotIn("ports", services["database"])
        self.assertNotIn("ports", services["redis"])

    def test_immich_family_matches_the_validated_v3_3_5_release(self):
        services = catalog.render_stage(config(), 5)["services"]
        # Server/ML v3.3.5 and the database/cache selected by that release.
        expected = {"immich-server": "485d546a2e31d6a8b4db8423fcba5c109de8c218d93410af115fd1ccfc4352a6",
                    "immich-machine-learning": "5b28c9352c8374b8a1ed96fbdd8d6755fea6e43510ec43fdf6538aa02ea40f56",
                    "redis": "c123e3715db63d06d4ad6964884037aa0d5d4d703939b9929954112889708e1d",
                    "database": "bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23"}
        for name, digest in expected.items():
            ref = services[name]["image"]
            self.assertEqual(ref.split("@sha256:")[1], digest)
            self.assertNotIn(":", ref.split("@")[0].rsplit("/", 1)[-1])

    def test_every_default_image_is_immutable_on_first_install(self):
        for name, ref in catalog.IMAGES.items():
            with self.subTest(image=name):
                self.assertRegex(ref, r"^[^@\s]+@sha256:[0-9a-f]{64}$")
                self.assertNotIn(":", ref.split("@")[0].rsplit("/", 1)[-1])

    def test_every_rendered_profile_uses_pins_including_optional_helpers(self):
        observed = set()
        for acceleration in ["cpu", "intel", "amd", "nvidia"]:
            for port_forwarding in [False, True]:
                c = config()
                c["features"]["music_assistant"] = True
                c["hardware"]["acceleration"] = acceleration
                c["vpn"]["port_forwarding"] = port_forwarding
                for stage in range(2, 7):
                    for name, service in catalog.render_stage(c, stage)["services"].items():
                        with self.subTest(acceleration=acceleration, forwarding=port_forwarding,
                                          stage=stage, service=name):
                            self.assertRegex(service["image"], r"^[^@\s]+@sha256:[0-9a-f]{64}$")
                            observed.add(service["image"])
        self.assertEqual(observed, set(catalog.IMAGES.values()))

    def test_user_credentials_never_enter_public_compose(self):
        c = config()
        c["private_notes"] = "sentinel-nonrendered-private-content"
        text = json.dumps([catalog.render_stage(c, n) for n in range(2, 7)])
        self.assertNotIn(c["private_notes"], text)
        self.assertIn("slskd.env", text)
        self.assertNotIn('"SLSKD_PASSWORD":', text)
        self.assertIn("DB_PASSWORD_FILE", text)

    def test_disable_optional_stages_and_usenet(self):
        c = config()
        c["features"] = {"usenet": False, "torrents": False, "photos": False, "music": False, "dashboard": False}
        self.assertNotIn("sabnzbd", catalog.render_stage(c, 2)["services"])
        self.assertNotIn("homarr", catalog.render_stage(c, 4)["services"])
        for stage in [3, 5, 6]:
            self.assertEqual(catalog.render_stage(c, stage)["services"], {})
            self.assertEqual(catalog.stage_directories(c, stage), [])
            self.assertEqual(catalog.stage_endpoints(c, stage), {})

    def test_render_does_not_modify_config(self):
        c = config()
        before = copy.deepcopy(c)
        for stage in range(2, 7):
            catalog.render_stage(c, stage)
        self.assertEqual(c, before)

    def test_only_dockge_has_powerful_socket_access(self):
        services = catalog.render_stage(config(), 4)["services"]
        self.assertFalse(services["dockge"]["volumes"][-1]["read_only"])
        self.assertNotIn("ports", services["socket-proxy"])
        self.assertEqual(services["socket-proxy"]["environment"]["POST"], "0")
        self.assertNotIn("/var/run/docker.sock", json.dumps(services["homarr"]))

    def test_dockge_project_folder_and_env_paths_share_the_project_name(self):
        c = config()
        for stage in range(2, 7):
            document = catalog.render_stage(c, stage)
            project = f"example-{stage}"
            self.assertEqual(document["name"], project)
            for name, service in document["services"].items():
                for path in service.get("env_file", []):
                    self.assertEqual(path, f"/opt/samurai/stages/{project}/{name}.env")

    def test_management_controls_local_daemon_on_every_host_role(self):
        c = config()
        self.assertNotEqual(c["hosts"]["management"], c["bind_address"])
        services = catalog.render_stage(c, 4)["services"]
        for name in ["dockge", "uptime-kuma", "homarr"]:
            self.assertEqual(services[name]["ports"],
                             [f"{c['bind_address']}:{catalog.PORTS[name]}:{catalog.PORTS[name]}/tcp"])
            self.assertEqual(catalog.stage_endpoints(c, 4)[name],
                             f"http://{c['bind_address']}:{catalog.PORTS[name]}")
        mounts = services["dockge"]["volumes"]
        self.assertEqual({v["target"] for v in mounts},
                         {"/app/data", "/opt/samurai/stages", "/var/run/docker.sock"})

    def test_storage_directory_plan_excludes_devices_and_existing_data(self):
        c = config()
        for stage in range(2, 7):
            for name in catalog.stage_directories(c, stage):
                self.assertTrue(name.startswith("/opt/samurai/appdata/"))
        self.assertNotIn("/var/run/docker.sock", catalog.stage_directories(c, 4))


if __name__ == "__main__":
    unittest.main()
