"""Mounted filesystem choices and bounded fresh-site storage correction."""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import setup_local_storage as local, installation_registry as registry, storage
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json


MOUNT = {"target": "/mnt/media", "source": "/dev/fixture-data", "fstype": "ext4", "options": "rw,relatime"}


class MountHost:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(stdout=json.dumps({"filesystems": self.rows}))


class ChoicePath:
    """Linux path representation for config-write tests on Windows as well."""
    def __init__(self, occupied=False):
        self.occupied = occupied

    def __str__(self):
        return "/mnt/media/new-empty-tree"

    def __fspath__(self):
        return str(self)

    def exists(self):
        return self.occupied

    def is_dir(self):
        return True

    def iterdir(self):
        return iter(["existing-item"] if self.occupied else [])


class LocalMountChoiceTests(unittest.TestCase):
    def test_filters_os_system_readonly_remote_and_bind_mounts(self):
        rows = [dict(MOUNT)]
        rows.extend({**MOUNT, "target": target} for target in ("/", "/boot/efi", "/var/lib/docker", "/proc/test", "/run/user", "/home", "/snap/test"))
        rows.extend(({**MOUNT, "target": "/mnt/readonly", "options": "ro"},
                     {**MOUNT, "target": "/mnt/nas", "fstype": "nfs4"},
                     {**MOUNT, "target": "/mnt/bind", "options": "rw,bind"}))
        host = MountHost(rows)
        with patch.object(local, "_capacity", return_value={"size_bytes": 4 * 1024 ** 4}):
            choices = local.mounted_local_choices(host)
        self.assertEqual(choices, [{**MOUNT, "size_bytes": 4 * 1024 ** 4}])
        self.assertTrue(all(call[0] == "findmnt" for call in host.calls))

    def test_ambiguous_duplicate_mountpoint_refuses_selection(self):
        with self.assertRaisesRegex(SafetyError, "same mountpoint"):
            local.mounted_local_choices(MountHost([MOUNT, {**MOUNT, "source": "/dev/other"}]))

    def test_single_data_mount_is_default_instead_of_os_disk(self):
        selected = Path("/mnt/media/new-empty-tree")
        output, prompts = [], []
        def read(prompt):
            prompts.append(prompt)
            return ""
        with patch.object(local, "mounted_local_choices", return_value=[MOUNT]), \
                patch.object(local, "_empty_child", return_value=(selected, MOUNT)) as child:
            result = local.choose_local_directory(input_fn=read, output_fn=output.append)
        self.assertEqual(result, (selected, MOUNT))
        self.assertEqual(child.call_args.args[-1], MOUNT)
        self.assertIn("[1]", prompts[0])
        self.assertIn("/mnt/media", " ".join(output))

    def test_correction_requires_an_existing_non_system_mount(self):
        with patch.object(local, "mounted_local_choices", return_value=[]):
            with self.assertRaisesRegex(SafetyError, "Mount the intended data disk"):
                local.choose_local_directory(require_mounted=True, output_fn=lambda _: None)

    @unittest.skipUnless(os.name == "posix", "Exact Linux mount/path integration")
    def test_first_wizard_defaults_to_existing_data_mount_not_srv(self):
        from samurai_stack import setup_storage
        from test_setup_storage import FakeHost, InputAnswers
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disk = root / "mounted-data"
            disk.mkdir()
            host = FakeHost()
            host.interfaces = host.interfaces[1:2]
            host.rows.append({**MOUNT, "target": str(disk)})
            site = root / "site.json"
            answers = InputAnswers(["", str(root / "apps"), "Etc/UTC", "", "2", "", "", "", "", "", "", "", "", "", "yes"])
            with patch.object(local, "_SYSTEM", local._SYSTEM - {"/tmp"}), \
                    patch.object(local, "_capacity", return_value={}):
                config = setup_storage.ensure_site_config(site, input_fn=answers, output_fn=lambda _: None, runner=host)
            self.assertEqual(config["storage"]["data_root"], str(disk / "samurai"))
            self.assertEqual(config["storage"]["expected_mount"], {key: host.rows[1][key] for key in ("target", "source", "fstype")})
            self.assertFalse((disk / "samurai").exists())
            self.assertFalse(any(call[0] in {"mount", "chown", "mv", "rm"} for call in host.calls))

    @unittest.skipUnless(os.name == "posix", "Exact Linux mount/path integration")
    def test_occupied_default_child_requires_new_empty_path_and_preserves_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            occupied = target / "samurai"
            occupied.mkdir()
            marker = occupied / ".samurai-storage.json"
            marker.write_text("old-marker-sentinel")
            selected = {**MOUNT, "target": str(target)}
            answers = iter(["", str(target / "fresh")])
            output = []
            with patch.object(storage, "mount_identity", return_value=selected):
                path, found = local._empty_child(lambda _: next(answers), output.append, None, selected)
            self.assertEqual(path, target / "fresh")
            self.assertEqual(found, selected)
            self.assertFalse(path.exists())
            self.assertEqual(marker.read_text(), "old-marker-sentinel")
            self.assertIn("already contains files", " ".join(output))


class FreshCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config()
        self.c["storage"].update(mode="local", data_root="/srv/samurai-data", expected_mount={
            "target": "/", "source": "/dev/fixture-os", "fstype": "ext4"})
        self.site = self.root / "site.json"
        write_json(self.site, self.c)
        self.before = self.site.read_bytes()
        self.backups = self.root / "backups"
        self.backups.mkdir(mode=0o700)
        self.patch(registry, "_guard")
        if os.name == "posix" and os.geteuid() != 0:
            self.patch(registry, "_owner")
        self.guards = self.patch(local, "_guards")
        self.choose = self.patch(local, "choose_local_directory", return_value=(ChoicePath(), MOUNT))
        self.mount = self.patch(storage, "mount_identity", return_value=MOUNT)
        self.draft = self.patch(registry, "draft_directory", return_value=self.backups)

    def patch(self, target, name, **kwargs):
        item = patch.object(target, name, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def correct(self, answer="yes", **kwargs):
        return local.choose_replacement(self.c, self.site, input_fn=lambda _: answer,
            output_fn=lambda _: None, **kwargs)

    def test_changes_only_storage_preserves_uuid_features_and_exact_private_backup(self):
        updated = self.correct()
        self.assertEqual(json.loads(self.site.read_text()), updated)
        for name in self.c.keys() - {"storage"}:
            self.assertEqual(updated[name], self.c[name])
        self.assertEqual(updated["storage"]["data_root"], "/mnt/media/new-empty-tree")
        self.assertEqual(updated["storage"]["expected_mount"], {key: MOUNT[key] for key in ("target", "source", "fstype")})
        self.assertIs(updated["storage"]["allow_existing_contents"], False)
        saved = list(self.backups.glob("site-before-storage-*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), self.before)
        self.assertEqual(self.c["storage"]["data_root"], "/srv/samurai-data")
        self.assertEqual(self.guards.call_count, 2)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(saved[0].stat().st_mode), 0o600)

    def test_decline_keeps_config_and_creates_no_backup(self):
        with self.assertRaisesRegex(SafetyError, "paused"):
            self.correct("no")
        self.assertEqual(self.site.read_bytes(), self.before)
        self.assertFalse(list(self.backups.iterdir()))
        self.draft.assert_not_called()

    def test_data_or_mount_change_during_review_refuses_config_write(self):
        self.choose.return_value = (ChoicePath(occupied=True), MOUNT)
        with self.assertRaisesRegex(SafetyError, "became occupied"):
            self.correct()
        self.assertEqual(self.site.read_bytes(), self.before)
        self.assertFalse(list(self.backups.iterdir()))
        self.choose.return_value = (ChoicePath(), MOUNT)
        self.mount.return_value = {**MOUNT, "source": "/dev/swapped-device"}
        with self.assertRaisesRegex(SafetyError, "mount changed"):
            self.correct()
        self.assertEqual(self.site.read_bytes(), self.before)

    def test_concurrent_site_edit_is_preserved(self):
        changed = deepcopy(self.c)
        changed["features"]["photos"] = True
        def confirm(_):
            write_json(self.site, changed, replace=True)
            return "yes"
        with self.assertRaisesRegex(SafetyError, "site changed"):
            local.choose_replacement(self.c, self.site, input_fn=confirm, output_fn=lambda _: None)
        self.assertEqual(json.loads(self.site.read_text()), changed)
        self.assertFalse(list(self.backups.iterdir()))

    def test_registered_or_existing_app_guard_failure_never_changes_site(self):
        self.guards.side_effect = SafetyError("registered identity or existing applications")
        with self.assertRaises(SafetyError):
            self.correct()
        self.assertEqual(self.site.read_bytes(), self.before)
        self.choose.assert_not_called()
        self.assertFalse(list(self.backups.iterdir()))

    def test_already_selected_non_os_filesystem_is_outside_this_correction(self):
        self.c["storage"]["expected_mount"] = {key: MOUNT[key] for key in ("target", "source", "fstype")}
        self.c["storage"]["data_root"] = "/mnt/media/another-tree"
        with self.assertRaisesRegex(SafetyError, "limited to fresh"):
            self.correct()
        self.assertEqual(self.site.read_bytes(), self.before)
        self.guards.assert_not_called()


class CorrectionGuardTests(unittest.TestCase):
    def test_only_exact_empty_installer_tree_and_current_marker_are_permitted(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            root = Path(directory) / "data"
            c["storage"]["data_root"] = str(root)
            root.mkdir()
            for value in storage.DIRECTORIES:
                (root / value).mkdir(parents=True, exist_ok=True)
            marker = root / ".samurai-storage.json"
            write_json(marker, {"deployment_id": c["deployment_id"], "expected_mount": c["storage"]["expected_mount"]}, mode=0o644)
            before = marker.read_bytes()
            local._empty_prepared_tree(c)
            self.assertEqual(marker.read_bytes(), before)
            media = root / "library/movies/personal-file.mkv"
            media.write_bytes(b"personal-sentinel")
            with self.assertRaisesRegex(SafetyError, "contains files"):
                local._empty_prepared_tree(c)
            self.assertEqual(media.read_bytes(), b"personal-sentinel")

    def test_foreign_marker_and_unmanaged_empty_directory_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            root = Path(directory)
            c["storage"]["data_root"] = directory
            write_json(root / ".samurai-storage.json", {"deployment_id": "foreign", "expected_mount": c["storage"]["expected_mount"]})
            with self.assertRaisesRegex(SafetyError, "another installation"):
                local._empty_prepared_tree(c)
        with tempfile.TemporaryDirectory() as directory:
            c["storage"]["data_root"] = directory
            (Path(directory) / "documents").mkdir()
            with self.assertRaisesRegex(SafetyError, "unmanaged directories"):
                local._empty_prepared_tree(c)

    def test_docker_project_and_uuid_checks_include_stopped_containers(self):
        c = default_config()
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if "context" in argv:
                return SimpleNamespace(stdout="unix:///var/run/docker.sock")
            if "label=systems.samarie.deployment=" + c["deployment_id"] in argv:
                return SimpleNamespace(stdout="a" * 64)
            return SimpleNamespace(stdout="")
        with self.assertRaisesRegex(SafetyError, "Existing Docker containers"):
            local._no_containers(c, runner)
        self.assertTrue(any("--all" in call for call in calls))
        calls.clear()
        def empty(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(stdout="unix:///var/run/docker.sock" if "context" in argv else "")
        local._no_containers(c, empty)
        self.assertEqual(len([call for call in calls if "ps" in call]), 6)
        self.assertTrue(all(not set(call) & {"rm", "stop", "restart", "up", "down", "exec"} for call in calls))

    def test_canonical_registry_identity_is_not_repointed(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            (Path(directory) / c["deployment_id"]).mkdir()
            with self.assertRaisesRegex(SafetyError, "already registered"):
                local._unregistered(c, directory)

    def test_current_owner_or_same_project_active_unit_blocks_zero_container_proof(self):
        for same_uuid in (True, False):
            for state, substate, job in (("active", "running", "0"), ("activating", "auto-restart", "0"),
                                        ("reloading", "reload", "0"), ("inactive", "dead", "12")):
                with self.subTest(same_uuid=same_uuid, state=state, substate=substate):
                    with tempfile.TemporaryDirectory() as directory:
                        c = default_config()
                        unit = Path(directory) / ("samurai-renamed-2.service" if same_uuid else "samurai-homelab-2.service")
                        owner = c["deployment_id"] if same_uuid else "0176e12d-88ad-4c87-86f6-371b714d10c4"
                        unit.write_text("# Managed by Samurai Stack: " + owner + "\n[Service]\n")
                        calls = []
                        def runner(argv, **kwargs):
                            calls.append(argv)
                            return SimpleNamespace(stdout="LoadState=loaded\nActiveState=" + state + "\nSubState=" + substate + "\nJob=" + job + "\n", returncode=0)
                        with self.assertRaisesRegex(SafetyError, "active, restarting, queued"):
                            local._no_containers(c, runner, unit_root=directory)
                        self.assertTrue(calls and all(call[0] == "systemctl" for call in calls))

    def test_inert_old_uuid_unit_allows_clean_restart_without_service_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            unit = Path(directory) / "samurai-homelab-2.service"
            original = "# Managed by Samurai Stack: 0176e12d-88ad-4c87-86f6-371b714d10c4\n[Service]\n"
            unit.write_text(original)
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "systemctl":
                    return SimpleNamespace(stdout="LoadState=loaded\nActiveState=failed\nSubState=failed\nJob=0\n", returncode=0)
                return SimpleNamespace(stdout="unix:///var/run/docker.sock" if "context" in argv else "")
            local._no_containers(c, runner, unit_root=directory)
            self.assertEqual(unit.read_text(), original)
            self.assertEqual(len([call for call in calls if call[0] == "systemctl"]), 2)
            self.assertTrue(all("show" in call for call in calls if call[0] == "systemctl"))

    def test_unit_that_starts_during_zero_container_read_is_caught_afterward(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            (Path(directory) / "samurai-homelab-2.service").write_text("# Managed by Samurai Stack: " + c["deployment_id"] + "\n")
            states = iter(("LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=0\n",
                           "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\nJob=0\n"))
            def runner(argv, **kwargs):
                if argv[0] == "systemctl":
                    return SimpleNamespace(stdout=next(states), returncode=0)
                return SimpleNamespace(stdout="unix:///var/run/docker.sock" if "context" in argv else "")
            with self.assertRaisesRegex(SafetyError, "active, restarting, queued"):
                local._no_containers(c, runner, unit_root=directory)

    def test_unknown_systemd_state_is_sanitized_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            c = default_config()
            (Path(directory) / "samurai-homelab-2.service").write_text("# Managed by Samurai Stack: " + c["deployment_id"] + "\n")
            def runner(argv, **kwargs):
                raise RuntimeError("fixture-private-vendor-body")
            with self.assertRaises(SafetyError) as result:
                local._no_containers(c, runner, unit_root=directory)
            self.assertNotIn("fixture-private-vendor-body", str(result.exception))

    def test_nonempty_appdata_cannot_be_repointed_even_without_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = default_config()
            c["install_root"] = str(root / "apps")
            c["storage"]["data_root"] = str(root / "absent-data")
            appdata = Path(c["install_root"]) / "appdata" / "sonarr"
            appdata.mkdir(parents=True)
            personal = appdata / "sonarr.db"
            personal.write_bytes(b"existing-application-sentinel")
            write_json(Path(c["install_root"]) / ".samurai-install.json", {"deployment_id": c["deployment_id"]})
            with patch.object(local, "_unregistered"), patch.object(local, "_no_containers"), \
                    patch.object(storage, "check_mount"):
                with self.assertRaisesRegex(SafetyError, "Application data already exists"):
                    local._guards(c, root / "registry", None)
            self.assertEqual(personal.read_bytes(), b"existing-application-sentinel")

    def test_hardlinked_storage_marker_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            c = default_config()
            c["storage"]["data_root"] = str(data)
            original = root / "marker-original"
            write_json(original, {"deployment_id": c["deployment_id"], "expected_mount": c["storage"]["expected_mount"]})
            try:
                os.link(original, data / ".samurai-storage.json")
            except OSError:
                self.skipTest("Hardlinks unavailable in this test filesystem")
            with self.assertRaisesRegex(SafetyError, "marker is invalid"):
                local._empty_prepared_tree(c)


if __name__ == "__main__":
    unittest.main()
