"""Source replacement cannot silently create or select another deployment."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import installation_registry as registry, setup_recovery
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json


class SetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry_root = self.root / "registry"
        self.setup_root = self.root / "setup"
        self.unit_root = self.root / "units"
        self.unit_root.mkdir()
        self.c = default_config("192.168.50.21")
        self.host = "a" * 64
        self.discover = self.patch(registry, "discover", return_value=[])
        self.remember = self.patch(registry, "remember_verified")
        self.draft = self.patch(registry, "draft_directory", return_value=self.setup_root / self.host)
        self.patch(registry, "host_identity", return_value=self.host)
        self.inventory = self.patch(setup_recovery.recovery, "inventory", return_value={
            "containers": [], "systemd_units": [], "warnings": []})
        self.patch(setup_recovery.shutil, "which", return_value=None)
        self.patch(setup_recovery.Path, "cwd", return_value=self.root)
        if os.name == "posix" and os.geteuid() != 0:
            self.patch(setup_recovery, "_root_owned", return_value=True)

    def patch(self, target, name, **kwargs):
        item = patch.object(target, name, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def resolve(self, path=None, **kwargs):
        return setup_recovery.resolve_configuration(path, registry_root=self.registry_root,
            setup_root=self.setup_root, unit_root=self.unit_root, **kwargs)

    def record(self, config=None, path=None, source="registry"):
        c = config or self.c
        return {"config": c, "path": path or self.registry_root / c["deployment_id"] / "site.json",
                "source": source, "aliases": []}

    def private_source(self, config=None):
        path = self.root / "site.json"
        write_json(path, config or self.c)
        return path

    def test_explicit_relative_or_absolute_path_never_autoreplaced(self):
        for path in (Path("chosen-site.json"), self.root / "missing.json"):
            with self.subTest(path=path):
                self.assertEqual(self.resolve(path), path.absolute())
        self.discover.assert_not_called()
        self.remember.assert_not_called()
        self.inventory.assert_not_called()
        self.draft.assert_not_called()

    def test_source_replacement_reuses_canonical_registry_before_new_source(self):
        old = self.record()
        changed = copy.deepcopy(self.c)
        changed["deployment_id"] = "2f0e2b88-70ad-40af-bd74-23961ca18cab"
        changed["storage"]["data_root"] = "/srv/new-wrong-tree"
        path = self.private_source(changed)
        original = path.read_bytes()
        self.discover.return_value = [old]
        self.remember.return_value = old["path"]
        self.assertEqual(self.resolve(output_fn=lambda _: None), old["path"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.remember.call_args.args, (self.c,))
        self.inventory.assert_not_called()

    def test_trusted_service_candidate_kept_if_storage_registration_fails(self):
        original = self.root / "runtime" / "site.json"
        candidate = self.record(path=original, source="service")
        self.discover.return_value = [candidate]
        self.remember.side_effect = SafetyError("fixture private reason must not escape")
        output = []
        self.assertEqual(self.resolve(output_fn=output.append), original)
        self.assertIn("original identity", " ".join(output))
        self.assertNotIn("fixture private reason", " ".join(output))
        self.draft.assert_not_called()

    def test_missing_runtime_warning_surfaces_without_echoing_record_text(self):
        candidate = self.record()
        candidate["warnings"] = ["fixture-private-reason-must-not-be-echoed"]
        self.discover.return_value = [candidate]
        self.remember.return_value = candidate["path"]
        output = []
        self.assertEqual(self.resolve(output_fn=output.append), candidate["path"])
        text = " ".join(output)
        self.assertIn("runtime configuration is missing", text)
        self.assertIn("before any redeployment", text)
        self.assertIn("--recover", text)
        self.assertNotIn("fixture-private-reason", text)

    def test_legacy_private_site_in_normal_source_parent_preserves_exact_file(self):
        path = self.private_source()
        # Source parents need not be mode 700; only this root-owned file is private.
        self.root.chmod(0o755)
        before = path.read_bytes()
        self.remember.side_effect = SafetyError("storage not prepared yet")
        self.assertEqual(self.resolve(output_fn=lambda _: None), path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.remember.call_args.args, (self.c,))
        self.draft.assert_not_called()

    def test_old_source_registers_only_after_registry_live_storage_verification(self):
        self.private_source()
        canonical = self.registry_root / self.c["deployment_id"] / "site.json"
        self.remember.return_value = canonical
        self.assertEqual(self.resolve(output_fn=lambda _: None), canonical)
        self.remember.assert_called_once()

    def test_missing_site_with_old_containers_refuses_new_draft(self):
        self.inventory.return_value["containers"] = [{"deployment_id": self.c["deployment_id"], "project": "homelab-2", "mounts": []}]
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.draft.assert_not_called()
        self.remember.assert_not_called()

    def test_existing_unit_without_readable_site_refuses_new_draft(self):
        (self.unit_root / "samurai-homelab-2.service").write_text("[Service]\n")
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.draft.assert_not_called()

    def test_discovery_conflict_never_falls_through_to_source_or_new_id(self):
        self.private_source()
        self.discover.side_effect = SafetyError("runtime config missing")
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.draft.assert_not_called()
        self.remember.assert_not_called()
        self.inventory.assert_not_called()

    def test_private_persistent_draft_survives_source_folder_replacement(self):
        saved = self.setup_root / self.host / "site.json"
        saved.parent.mkdir(parents=True)
        write_json(saved, self.c)
        self.discover.side_effect = [[], [self.record(path=saved, source="saved")]]
        self.remember.side_effect = SafetyError("storage not connected yet")
        self.assertEqual(self.resolve(output_fn=lambda _: None), saved)
        self.assertEqual(self.discover.call_args.kwargs["config_paths"], (saved,))
        self.draft.assert_not_called()

    def test_multiple_configs_require_numeric_choice_not_yes_approval(self):
        other = copy.deepcopy(self.c)
        other.update(deployment_id="2f0e2b88-70ad-40af-bd74-23961ca18cab", deployment_name="other", install_root="/opt/other")
        rows = [self.record(), self.record(other)]
        self.discover.return_value = rows
        self.remember.return_value = rows[1]["path"]
        answers = iter(["yes", "0", "99", "2"])
        output = []
        chosen = self.resolve(input_fn=lambda _: next(answers), output_fn=output.append)
        self.assertEqual(chosen, rows[1]["path"])
        self.assertEqual(self.remember.call_args.args, (other,))
        text = " ".join(output)
        for row in rows:
            self.assertIn(row["config"]["deployment_id"], text)
            self.assertIn(row["config"]["storage"]["data_root"], text)
        self.assertNotIn("password", text.lower())

    def test_blank_choice_pauses_without_registration(self):
        self.discover.return_value = [self.record(), self.record(path=self.root / "other-site.json")]
        with self.assertRaisesRegex(SafetyError, "paused"):
            self.resolve(input_fn=lambda _: "", output_fn=lambda _: None)
        self.remember.assert_not_called()

    def test_legacy_source_new_uuid_cannot_replace_existing_project(self):
        self.private_source()
        self.inventory.return_value["containers"] = [{"deployment_id": "2f0e2b88-70ad-40af-bd74-23961ca18cab",
            "project": "homelab-2", "mounts": []}]
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.remember.assert_not_called()

    def test_matching_legacy_source_runtime_is_reused_without_account_adoption(self):
        path = self.private_source()
        self.inventory.return_value["containers"] = [{"deployment_id": self.c["deployment_id"], "project": "homelab-2", "mounts": []}]
        self.remember.side_effect = SafetyError("storage unavailable")
        self.assertEqual(self.resolve(output_fn=lambda _: None), path)
        self.draft.assert_not_called()

    def test_changed_name_still_refuses_occupied_old_application_root(self):
        self.private_source()
        self.inventory.return_value["containers"] = [{"deployment_id": "2f0e2b88-70ad-40af-bd74-23961ca18cab",
            "project": "different-2", "mounts": [{"source": "/opt/samurai/appdata/sonarr"}]}]
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.remember.assert_not_called()

    def test_empty_new_guest_uses_host_persistent_draft_outside_checkout(self):
        selected = self.resolve()
        self.assertEqual(selected, self.setup_root / self.host / "site.json")
        self.draft.assert_called_once_with(setup_root=self.setup_root)
        self.inventory.assert_called_once()
        self.remember.assert_not_called()
        self.assertFalse((self.root / "site.json").exists())

    def test_installed_but_unavailable_docker_does_not_mean_empty_host(self):
        self.inventory.return_value["warnings"] = ["Local default Docker context unavailable"]
        self.patch(setup_recovery.shutil, "which", return_value="/usr/bin/docker")
        with self.assertRaisesRegex(SafetyError, "--recover"):
            self.resolve()
        self.draft.assert_not_called()

    def test_unreadable_or_unowned_existing_source_is_not_overwritten(self):
        path = self.private_source()
        before = path.read_bytes()
        with patch.object(setup_recovery, "_root_owned", return_value=False):
            with self.assertRaisesRegex(SafetyError, "owned by root"):
                self.resolve()
        self.assertEqual(path.read_bytes(), before)
        self.remember.assert_not_called()
        self.draft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
