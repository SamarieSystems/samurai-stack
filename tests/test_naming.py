"""Compose names remain consistent without migrating existing installations."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from samurai_stack import catalog, engine, installation_registry as registry, naming
from samurai_stack import retired_units, setup_fresh, setup_local_storage, setup_recovery
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, validate_config, write_json


class NamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.21")
        # Site paths remain Linux syntax for validation. On Windows, removing
        # the temporary directory's drive keeps the same current-drive path.
        self.c["install_root"] = (self.root / "application").as_posix().removeprefix(self.root.drive)
        self.enterContext(patch.object(registry, "REGISTRY_ROOT", self.root / "registry"))
        self.enterContext(patch.object(naming, "UNIT_ROOT", self.root / "units"))

    def test_absent_and_empty_maps_preserve_legacy_names_and_identity(self):
        identity = registry._identity(self.c)
        self.assertNotIn("compose_projects", identity)
        for stage in range(7):
            self.assertEqual(naming.project_name(self.c, stage), "homelab-" + str(stage))
        self.c["compose_projects"] = {}
        validate_config(self.c)
        self.assertEqual(registry._identity(self.c), identity)
        self.assertEqual(naming.project_name(self.c, 4), "homelab-4")

    def test_fresh_defaults_are_readable_unique_and_valid(self):
        self.c["compose_projects"] = naming.default_projects("homelab")
        self.assertEqual(self.c["compose_projects"], {"2": "homelab-media", "3": "homelab-torrents",
            "4": "homelab-management", "5": "homelab-photos", "6": "homelab-music"})
        validate_config(self.c)

    def test_invalid_or_ambiguous_names_fail_config_validation(self):
        invalid = [None, [], "media", {2: "media"}, {"1": "host"}, {"7": "other"},
                   {"2": "Media"}, {"2": "bad/name"}, {"2": "two words"}, {"2": "-media"},
                   {"2": ""}, {"2": "a" * 64}, {"2": 3}, {"2": "same", "3": "same"},
                   {"2": "homelab-3"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SafetyError):
                validate_config(dict(self.c, compose_projects=value))

    def test_mixed_custom_and_legacy_names_drive_all_runtime_outputs(self):
        self.c["compose_projects"] = {"2": "house-media", "4": "house-tools", "5": "house-photos", "6": "house-music"}
        self.c["features"].update(torrents=True, photos=True, music=True)
        expected = {2: "house-media", 3: "homelab-3", 4: "house-tools", 5: "house-photos", 6: "house-music"}
        for stage, project in expected.items():
            with self.subTest(stage=stage):
                document = engine.recipe(self.c, stage, use_lock=False)
                self.assertEqual(document["name"], project)
                self.assertEqual(engine.stage_path(self.c, stage), Path(self.c["install_root"]) / "stages" / project)
                self.assertEqual(engine.compose_args(self.c, stage), ["compose", "--project-name", project,
                    "--file", str(Path(self.c["install_root"]) / "stages" / project / "compose.yaml")])
                for service in document["services"].values():
                    for env in service.get("env_file", []):
                        self.assertIn("/stages/" + project + "/", env)
                    self.assertEqual(service["labels"]["systems.samarie.stage"], str(stage))
                self.assertEqual(engine.receipt_path(self.c, stage).name, "stage-" + str(stage) + ".json")

    def test_project_inventory_queries_the_configured_compose_identity(self):
        self.c["compose_projects"] = {"4": "house-tools"}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.assertIsNone(engine.project_conflicts(self.c, 4, runner))
        self.assertIn("label=com.docker.compose.project=house-tools", runner.call_args.args[0])

    def test_source_recovery_uses_custom_names_and_rejects_renamed_uuid(self):
        self.c["compose_projects"] = {"4": "house-tools", "5": "house-photos", "6": "house-music"}
        for stage, name in ((4, "house-tools"), (5, "house-photos"), (6, "house-music")):
            row = {"project": name, "deployment_id": self.c["deployment_id"], "mounts": []}
            setup_recovery._source_matches_runtime(self.c, {"containers": [row]})
            with self.subTest(stage=stage), self.assertRaises(SafetyError):
                setup_recovery._source_matches_runtime(default_config(), {"containers": [row]})

    def test_fresh_and_storage_correction_guards_check_custom_optional_stages(self):
        self.c["compose_projects"] = {"4": "house-tools", "5": "house-photos", "6": "house-music"}
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "unix:///var/run/docker.sock" if "context" in argv[3:4] else "", "")
        setup_fresh._no_containers([], [{"config": self.c}], runner, True)
        with patch.object(setup_local_storage, "_inactive_service_units"):
            setup_local_storage._no_containers(self.c, runner)
        for name in ("house-tools", "house-photos", "house-music"):
            self.assertEqual(sum("label=com.docker.compose.project=" + name in argv for argv in calls), 2)


class InstalledNameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.21")
        self.c["install_root"] = str(self.root)
        self.registry_root = self.root / "registry"
        self.unit_root = self.root / "units"
        self.enterContext(patch.object(registry, "REGISTRY_ROOT", self.registry_root))
        self.enterContext(patch.object(naming, "UNIT_ROOT", self.unit_root))
        self.enterContext(patch.object(registry, "host_identity", return_value="fixture-host"))
        if os.name == "posix" and os.geteuid() != 0:
            self.enterContext(patch.object(registry, "_owner"))
            self.enterContext(patch.object(retired_units, "_owner"))

    def save_stage(self, stage):
        directory = engine.stage_path(self.c, stage)
        directory.mkdir(parents=True, mode=0o700)
        write_json(directory / "site.json", self.c)
        return directory

    def test_legacy_stage_cannot_be_silently_renamed_before_docker_lookup(self):
        self.save_stage(4)
        changed = dict(self.c, compose_projects={"4": "house-tools"})
        runner = Mock(side_effect=AssertionError("No Docker mutation or query expected"))
        with self.assertRaisesRegex(SafetyError, "fixed as homelab-4"):
            engine.project_conflicts(changed, 4, runner)
        runner.assert_not_called()
        self.assertFalse((self.root / "stages" / "house-tools").exists())

    def test_removing_custom_map_is_also_a_refused_migration(self):
        self.c["compose_projects"] = {"4": "house-tools"}
        self.save_stage(4)
        changed = dict(self.c)
        changed.pop("compose_projects")
        with self.assertRaisesRegex(SafetyError, "fixed as house-tools"):
            naming.check_installed_names(changed)
        naming.check_installed_names(self.c)

    def test_snapshot_guards_only_already_installed_stages(self):
        self.save_stage(4)
        naming.check_installed_names(dict(self.c, compose_projects={"2": "house-media"}))

    def test_conflicting_uuid_or_ambiguous_snapshot_is_not_adopted(self):
        directory = self.save_stage(4)
        saved = dict(self.c, deployment_id=default_config()["deployment_id"])
        write_json(directory / "site.json", saved, replace=True)
        with self.assertRaisesRegex(SafetyError, "Saved stage identity"):
            naming.check_installed_names(self.c)

    def test_canonical_identity_alone_blocks_renaming_a_missing_runtime(self):
        self.c['compose_projects'] = {'4': 'house-tools'}
        directory = registry._mkdir(self.registry_root / self.c['deployment_id'])
        write_json(directory / 'identity.json', {'schema_version': 1, 'host_id': 'fixture-host',
                                                 'identity': registry._identity(self.c)})
        changed = dict(self.c, compose_projects={'4': 'other-tools'})
        with self.assertRaisesRegex(SafetyError, 'Restore its original compose_projects'):
            naming.check_installed_names(changed)
        naming.check_installed_names(self.c)

    def test_owned_unit_alone_blocks_rename_or_map_removal_without_runtime(self):
        from test_retired_units import generated
        self.c['compose_projects'] = {'4': 'house-tools'}
        self.unit_root.mkdir(mode=0o755)
        unit = self.unit_root / 'samurai-homelab-4.service'
        unit.write_text(generated(self.c, stage=4, old=self.c['deployment_id']).replace('homelab-4', 'house-tools'))
        unit.chmod(0o644)
        original = unit.read_bytes()
        for names in ({'4': 'other-tools'}, {}):
            with self.subTest(names=names), self.assertRaisesRegex(SafetyError, 'Restore its original compose_projects'):
                naming.check_installed_names(dict(self.c, compose_projects=names))
        naming.check_installed_names(self.c)
        self.assertEqual(unit.read_bytes(), original)

    def test_owned_custom_unit_directives_fail_closed_and_legacy_guard_is_accepted(self):
        from test_retired_units import generated
        self.unit_root.mkdir(mode=0o755)
        unit = self.unit_root / 'samurai-homelab-4.service'
        text = generated(self.c, stage=4, old=self.c['deployment_id'])
        text = text.replace(self.c['install_root'] + '/stages/homelab-4/site.json', self.c['install_root'] + '/site.json')
        unit.write_text(text)
        unit.chmod(0o644)
        naming.check_installed_names(self.c)
        unit.write_text(text.replace('Restart=on-failure', 'Restart=always'))
        with self.assertRaisesRegex(SafetyError, 'generated paths'):
            naming.check_installed_names(self.c)

    def test_owned_reboot_safe_unit_keeps_name_authority_on_retry(self):
        from test_retired_units import generated
        self.unit_root.mkdir(mode=0o755)
        unit = self.unit_root / 'samurai-homelab-4.service'
        text = generated(self.c, stage=4, old=self.c['deployment_id']).replace('Requires=docker.service\n',
            'Requires=docker.service\nPartOf=docker.service\nStartLimitIntervalSec=0\n')
        unit.write_text(text); unit.chmod(0o644)
        naming.check_installed_names(self.c)
        changed = dict(self.c, compose_projects={'4': 'different-tools'})
        with self.assertRaisesRegex(SafetyError, 'Restore its original compose_projects'):
            naming.check_installed_names(changed)
        self.assertEqual(unit.read_text(), text)

    def test_permission_failure_is_actionable_and_never_ignored_as_no_install(self):
        units = Mock(side_effect=AssertionError('Unknown canonical identity must stop inspection'))
        with patch.object(naming, '_registered_names', side_effect=PermissionError('fixture-private-path')), \
             patch.object(naming, '_unit_names', units):
            with self.assertRaisesRegex(SafetyError, 'sudo ./setup or sudo ./stack') as captured:
                naming.check_installed_names(self.c)
        self.assertIn('not treated as a fresh installation', str(captured.exception))
        self.assertNotIn('fixture-private-path', str(captured.exception))
        units.assert_not_called()

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() != 0, 'Real permission denial requires an ordinary Linux account')
    def test_unreadable_canonical_parent_fails_closed_without_changing_its_files(self):
        directory = self.registry_root / self.c['deployment_id']
        directory.mkdir(parents=True, mode=0o700)
        sentinel = directory / 'identity.json'
        write_json(sentinel, {'fixture': 'original evidence retained'})
        before = sentinel.read_bytes()
        self.registry_root.chmod(0o000)
        try:
            with self.assertRaisesRegex(SafetyError, 'sudo ./setup or sudo ./stack'):
                naming.check_installed_names(self.c)
        finally:
            self.registry_root.chmod(0o700)
        self.assertEqual(sentinel.read_bytes(), before)


class RegistryNameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.21")
        self.c["compose_projects"] = naming.default_projects("homelab")
        self.enterContext(patch.object(registry, "_guard"))
        self.enterContext(patch.object(registry, "host_identity", return_value="fixture-host"))
        self.enterContext(patch.object(registry.storage, "verify", side_effect=lambda c, runner: {
            "storage": "verified", "mount": c["storage"]["expected_mount"]}))
        if os.name == "posix" and os.geteuid() != 0:
            self.enterContext(patch.object(registry, "_owner"))
            self.enterContext(patch.object(retired_units, "_owner"))

    def test_registered_names_survive_source_replacement_and_cannot_be_changed(self):
        path = registry.remember_verified(self.c, registry_root=self.root / "registry")
        self.assertEqual(registry.load(self.c["deployment_id"], registry_root=self.root / "registry"), self.c)
        original = path.read_bytes()
        changed = copy.deepcopy(self.c)
        changed["compose_projects"]["6"] = "other-music"
        with self.assertRaisesRegex(SafetyError, "Restore its original compose_projects"):
            registry.remember_verified(changed, registry_root=self.root / "registry")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((path.parent / "history").exists())

    @unittest.skipUnless(os.name == "posix", "Generated unit paths are Linux paths")
    def test_custom_runtime_path_keeps_numeric_systemd_id(self):
        self.c["install_root"] = str(self.root / "apps")
        saved = engine.stage_path(self.c, 6) / "site.json"
        saved.parent.mkdir(parents=True, mode=0o700)
        write_json(saved, self.c)
        unit = self.root / "samurai-homelab-6.service"
        unit.write_text("# Managed by Samurai Stack: " + self.c["deployment_id"] + "\nExecStartPre=/usr/bin/python3 "
                        + str(Path(self.c["install_root"]) / "runtime_guard.py") + " " + str(saved) + " 6\n")
        unit.chmod(0o644)
        c, path, missing = registry._unit_config(unit)
        self.assertEqual((c, path, missing), (self.c, saved, False))

    def test_incomplete_canonical_metadata_retains_custom_names(self):
        directory = registry._mkdir(self.root / self.c["deployment_id"])
        write_json(directory / "identity.json", {"schema_version": 1, "host_id": "fixture-host",
                                                 "identity": registry._identity(self.c)})
        self.assertEqual(setup_fresh._entry_config(directory)["compose_projects"], self.c["compose_projects"])

    def test_fresh_unit_inventory_and_retirement_preserve_custom_stage_identity(self):
        from test_retired_units import Runner, generated, OLD
        units = self.root / "units"
        units.mkdir(mode=0o755)
        self.enterContext(patch.object(retired_units, "_guard"))
        self.enterContext(patch.object(setup_fresh, "_empty_application_root"))
        for stage, name in ((4, "house-tools"), (5, "house-photos"), (6, "house-music")):
            c = dict(self.c, compose_projects={str(stage): name})
            unit = units / ("samurai-homelab-" + str(stage) + ".service")
            text = generated(c, stage=stage).replace("homelab-" + str(stage), name)
            unit.write_text(text)
            unit.chmod(0o644)
            candidates = setup_fresh._unit_candidates(units)
            self.assertEqual(candidates[-1]["config"]["compose_projects"], {str(stage): name})
            runner = Runner()
            runner.fragment_path = str(unit)
            result = retired_units.retire_conflicting(c, stage, runner=runner,
                unit_root=units, archive_root=self.root / "archives")
            self.assertEqual(result["unit_name"], "samurai-homelab-" + str(stage) + ".service")
            self.assertEqual(result["old_deployment_id"], OLD)
            self.assertFalse(unit.exists())
            self.assertIn("label=com.docker.compose.project=" + name, [item for argv in runner.calls for item in argv])

    def test_fresh_unit_recovery_accepts_a_valid_other_stages_numbered_name(self):
        from test_retired_units import generated
        units = self.root / "units"
        units.mkdir(mode=0o755)
        c = dict(self.c, compose_projects={"2": "house-media", "4": "homelab-2"})
        validate_config(c)
        unit = units / "samurai-homelab-4.service"
        unit.write_text(generated(c, stage=4).replace("homelab-4", "homelab-2"))
        unit.chmod(0o644)
        with patch.object(setup_fresh, "_empty_application_root"):
            row, = setup_fresh._unit_candidates(units)
        self.assertEqual(naming.project_name(row["config"], 4), "homelab-2")
        self.assertEqual(row["stage"], 4)
        self.assertEqual(retired_units._generated(row["config"], 4, unit.read_text()), row["old_id"])


if __name__ == "__main__":
    unittest.main()
