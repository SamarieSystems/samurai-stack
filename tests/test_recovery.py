"""Recovery discovery locates evidence without adopting or changing a host."""
import copy
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import recovery


OLD = "5ae106d6-9c25-4d6e-ac18-294332960d24"
OTHER = "305e0b48-df88-4b71-a0d4-1b15796b143a"
CONTAINER = "a" * 64


def container():
    return {"id": CONTAINER, "name": "/example-2-sonarr-1", "status": "running",
            "deployment_id": OLD, "stage": "2", "project": "example-2", "service": "sonarr",
            "working_dir": "/opt/samurai/stages/example-2",
            "compose_files": "/opt/samurai/stages/example-2/compose.yaml",
            "mounts": [
                {"Type": "bind", "Source": "/opt/samurai/appdata/sonarr", "Destination": "/config", "RW": True},
                {"Type": "bind", "Source": "/mnt/media/samurai", "Destination": "/data", "RW": True},
            ]}


class Runner:
    def __init__(self):
        self.calls = []
        self.context = "unix:///var/run/docker.sock\n"
        self.ids = CONTAINER + "\n"
        self.containers = {CONTAINER: container()}
        self.mount = {"filesystems": [{"target": "/mnt/media", "source": "/dev/sdb", "fstype": "ext4", "uuid": "fixture-disk"}]}
        self.inspect_error = False
        self.ps_error = False

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        if argv[:6] == ["docker", "--context", "default", "context", "inspect", "default"]:
            return SimpleNamespace(stdout=self.context, returncode=0)
        if argv[:5] == ["docker", "--context", "default", "ps", "--all"]:
            if self.ps_error:
                raise RuntimeError("fixture-private-value-must-not-escape")
            return SimpleNamespace(stdout=self.ids, returncode=0)
        if argv[:4] == ["docker", "--context", "default", "inspect"]:
            if self.inspect_error:
                raise RuntimeError("fixture-private-value-must-not-escape")
            return SimpleNamespace(stdout=json.dumps(self.containers[argv[4]]), returncode=0)
        if argv[:3] == ["findmnt", "--json", "--mountpoint"]:
            return SimpleNamespace(stdout=json.dumps(self.mount), returncode=0)
        raise AssertionError("Unexpected command")


def absent(path):
    return {"path": path, "exists": False}


class RecoveryTests(unittest.TestCase):
    def inspect(self, runner, systemd_dir):
        with patch.object(recovery, "_path_info", side_effect=absent):
            return recovery.inventory(runner=runner, systemd_dir=systemd_dir)

    def test_discovers_original_uuid_data_root_and_config_without_adoption(self):
        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertEqual(result["mode"], "read_only_recovery_inventory")
        self.assertEqual(result["containers"][0]["deployment_id"], OLD)
        deployment = result["deployments"][0]
        self.assertEqual(deployment["install_roots"], [absent("/opt/samurai")])
        self.assertEqual(deployment["data_roots"], [absent("/mnt/media/samurai")])
        self.assertEqual(deployment["candidate_configs"], [absent("/opt/samurai/stages/example-2/site.json")])
        self.assertIs(deployment["running_missing_appdata"], True)
        self.assertTrue(any("preserve" in text for text in result["warnings"]))
        self.assertNotIn("restore", deployment)
        self.assertNotIn("adopt", json.dumps(result))

    def test_only_readonly_selected_inspection_commands_and_no_environment(self):
        runner = Runner()
        runner.containers[CONTAINER]["Env"] = ["MUST_NOT_PRINT=fixture-private-value"]
        runner.containers[CONTAINER]["Unexpected"] = "fixture-private-value"
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertNotIn("fixture-private-value", json.dumps(result))
        self.assertNotIn("Env", recovery._TEMPLATE)
        for argv in runner.calls:
            self.assertTrue(argv[0] in {"docker", "findmnt"})
            self.assertFalse(set(argv) & {"stop", "start", "restart", "rm", "exec", "cp", "up", "down"})
        self.assertEqual(result["media_mount"], runner.mount)

    def test_refuses_remote_context_but_retains_independent_mount_clue(self):
        runner = Runner()
        runner.context = "ssh://example.invalid\n"
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        docker = [argv for argv in runner.calls if argv[0] == "docker"]
        self.assertEqual(len(docker), 1)
        self.assertEqual(result["containers"], [])
        self.assertTrue(result["warnings"])
        self.assertEqual(result["media_mount"], runner.mount)

    def test_docker_unavailable_still_reports_managed_units_and_config_candidates(self):
        for failure in ("context", "ps"):
            with self.subTest(failure=failure):
                runner = Runner()
                if failure == "context":
                    runner.context = ""
                else:
                    runner.ps_error = True
                with tempfile.TemporaryDirectory() as directory:
                    unit = Path(directory) / "samurai-example-2.service"
                    unit.write_text("# Managed by Samurai Stack: " + OLD + "\n[Service]\n"
                        "ExecStartPre=/usr/bin/python3 /opt/samurai/runtime_guard.py /opt/samurai/stages/example-2/site.json 2\n")
                    result = self.inspect(runner, directory)
                self.assertEqual(result["media_mount"], runner.mount)
                self.assertEqual(len(result["systemd_units"]), 1)
                self.assertEqual(result["containers"], [])
                self.assertEqual(result["deployments"][0]["deployment_id"], OLD)
                self.assertEqual(result["deployments"][0]["candidate_configs"], [absent("/opt/samurai/stages/example-2/site.json")])
                self.assertEqual(result["deployments"][0]["install_roots"], [absent("/opt/samurai")])
                self.assertTrue(result["warnings"])

    def test_error_output_never_becomes_report_content(self):
        runner = Runner()
        runner.inspect_error = True
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertNotIn("fixture-private-value", json.dumps(result))
        self.assertEqual(result["containers"], [])
        self.assertTrue(result["warnings"])

    def test_invalid_ids_never_become_inspect_arguments(self):
        runner = Runner()
        runner.ids += "--help\n../../private\n"
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        inspect = [argv for argv in runner.calls if argv[:4] == ["docker", "--context", "default", "inspect"]]
        self.assertEqual(len(inspect), 1)
        self.assertEqual(len(result["containers"]), 1)
        self.assertTrue(result["warnings"])

    def test_keeps_different_uuid_deployments_separate(self):
        runner = Runner()
        second = "b" * 64
        runner.ids += second + "\n"
        runner.containers[second] = copy.deepcopy(container())
        runner.containers[second].update(id=second, deployment_id=OTHER)
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertEqual({row["deployment_id"] for row in result["deployments"]}, {OLD, OTHER})
        self.assertEqual(len(result["containers"]), 2)

    def test_managed_systemd_comments_locate_config_and_do_not_dump_unit(self):
        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "samurai-example-2.service"
            content = ("# Managed by Samurai Stack: " + OLD + "\n[Service]\n"
                "Environment=DO_NOT_PRINT=fixture-private-value\n"
                "ExecStartPre=/usr/bin/python3 /opt/samurai/runtime/guard.py /opt/samurai/stages/example-2/site.json 2\n")
            unit.write_text(content)
            metadata = unit.stat().st_mtime_ns
            result = self.inspect(runner, directory)
            self.assertEqual(unit.read_text(), content)
            self.assertEqual(unit.stat().st_mtime_ns, metadata)
        self.assertEqual(result["systemd_units"], [{"unit": "samurai-example-2.service", "deployment_id": OLD,
            "config_candidates": [absent("/opt/samurai/stages/example-2/site.json")]}])
        self.assertNotIn("fixture-private-value", json.dumps(result))

    def test_rejects_unmanaged_ambiguous_and_symlink_units(self):
        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "samurai-example-2.service"
            unit.write_text("# Managed by Samurai Stack: " + OLD + "\n# Managed by Samurai Stack: " + OTHER + "\n")
            self.assertFalse(self.inspect(runner, directory)["systemd_units"])
            unit.write_text("[Service]\nExecStart=/bin/true\n")
            self.assertFalse(self.inspect(runner, directory)["systemd_units"])
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK, st_size=10)):
                self.assertFalse(recovery._systemd(directory))

    def test_malformed_paths_and_unknown_schema_are_not_inferred(self):
        for source in ("/opt/../private", "/opt/private\nsecret", "relative", "//host/share"):
            with self.subTest(source=source):
                runner = Runner()
                runner.containers[CONTAINER]["mounts"][0]["Source"] = source
                with tempfile.TemporaryDirectory() as directory:
                    result = self.inspect(runner, directory)
                self.assertFalse(result["containers"])

    def test_existing_appdata_does_not_emit_missing_data_warning(self):
        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(recovery, "_path_info", side_effect=lambda value: {"path": value, "exists": True, "directory": True, "symlink": False}):
                result = recovery.inventory(runner=runner, systemd_dir=directory)
        self.assertIs(result["containers"][0]["missing_appdata"], False)
        self.assertFalse(result["deployments"][0]["running_missing_appdata"])
        self.assertFalse(result["warnings"])

    def test_tmpfs_has_no_host_source_and_does_not_drop_container(self):
        runner = Runner()
        runner.containers[CONTAINER]["mounts"].append({"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True})
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertEqual(len(result["containers"]), 1)
        self.assertEqual(result["containers"][0]["mounts"][-1]["type"], "tmpfs")
        self.assertIsNone(result["containers"][0]["mounts"][-1]["source_status"])

    def test_mount_options_and_url_credentials_are_never_reported(self):
        runner = Runner()
        userinfo = "fixture-user" + ":" + "fixture-private-value"
        runner.mount["filesystems"][0].update(source="https://" + userinfo + "@example.invalid/path",
                                              options="password=fixture-private-value")
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertNotIn("fixture-private-value", json.dumps(result))
        self.assertNotIn("options", result["media_mount"]["filesystems"][0])

    def test_data_state_on_data_destination_is_not_treated_as_media(self):
        runner = Runner()
        runner.containers[CONTAINER]["mounts"] = [
            {"Type": "bind", "Source": "/opt/samurai/appdata/music-assistant", "Destination": "/data", "RW": True}]
        with tempfile.TemporaryDirectory() as directory:
            result = self.inspect(runner, directory)
        self.assertEqual(result["deployments"][0]["data_roots"], [])
        self.assertEqual(result["deployments"][0]["install_roots"], [absent("/opt/samurai")])


if __name__ == "__main__":
    unittest.main()
