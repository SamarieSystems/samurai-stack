"""Coordinator boundary tests; commands, APIs and production writes are injected."""
from contextlib import ExitStack, contextmanager, nullcontext
import configparser
import copy
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid

from samurai_stack import catalog, engine, installation_registry, integrations
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(mock.patch.object(installation_registry, "REGISTRY_ROOT", self.root / "registry"))
        self.enterContext(mock.patch.object(engine.naming, "UNIT_ROOT", self.root / "units"))
        self.c = default_config("192.168.50.20")
        unique = "/tmp/samurai-test-" + uuid.uuid4().hex
        self.c["install_root"] = unique + "/application"
        self.c["storage"] = {"mode": "local", "data_root": unique + "/data", "allow_existing_contents": False,
                             "expected_mount": {"target": unique, "source": "fixture", "fstype": "ext4"}}
        self.host = {"host_id": "fixture-host", "mount": None}
        self.recipe = {"services": {"demo": {"image": "alpine:3.21"}}}

    def plan(self, stage=4, offline=False):
        with mock.patch.object(engine, "preflight", return_value=self.host), mock.patch.object(engine, "recipe", return_value=self.recipe):
            return engine.plan(self.c, stage, offline=offline)

    def test_reviewed_plan_rejects_tampering_before_preflight(self):
        document = self.plan()
        document["actions"].append("silently change something")
        with mock.patch.object(engine, "preflight") as preflight:
            with self.assertRaisesRegex(SafetyError, "hash"):
                engine.verify_plan(self.c, 4, document, document["review_hash"])
        preflight.assert_not_called()

    def test_offline_plan_cannot_be_applied(self):
        document = self.plan(offline=True)
        with self.assertRaisesRegex(SafetyError, "Offline plans"):
            engine.verify_plan(self.c, 4, document, document["review_hash"])

    def test_stale_host_is_rejected(self):
        document = self.plan()
        with mock.patch.object(engine, "preflight", return_value={"host_id": "another-host", "mount": None}), mock.patch.object(engine, "recipe", return_value=self.recipe):
            with self.assertRaisesRegex(SafetyError, "stale"):
                engine.verify_plan(self.c, 4, document, document["review_hash"])

    def test_changed_configuration_invalidates_review(self):
        document = self.plan()
        changed = copy.deepcopy(self.c)
        changed["timezone"] = "America/Denver"
        with mock.patch.object(engine, "preflight", return_value=self.host), mock.patch.object(engine, "recipe", return_value=self.recipe):
            with self.assertRaisesRegex(SafetyError, "stale"):
                engine.verify_plan(changed, 4, document, document["review_hash"])

    def test_existing_compose_change_invalidates_review(self):
        path = self.root / "compose.yaml"
        path.write_text('{"services":{}}')
        with mock.patch.object(engine, "compose_path", return_value=path):
            document = self.plan()
            path.write_text('{"services":{"unexpected":{}}}')
            with mock.patch.object(engine, "preflight", return_value=self.host), mock.patch.object(engine, "recipe", return_value=self.recipe):
                with self.assertRaisesRegex(SafetyError, "stale"):
                    engine.verify_plan(self.c, 4, document, document["review_hash"])

    def test_state_change_before_lock_acquisition_is_rechecked_before_writes(self):
        path = self.root / 'compose.yaml'
        path.write_text('{"services":{}}')

        @contextmanager
        def concurrent_apply(_config):
            path.write_text('{"services":{"changed-after-review":{}}}')
            yield

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine, 'compose_path', return_value=path))
            stack.enter_context(mock.patch.object(engine, 'preflight', return_value=self.host))
            stack.enter_context(mock.patch.object(engine, 'recipe', return_value=self.recipe))
            document = engine.plan(self.c, 4)
            stack.enter_context(mock.patch.object(engine.os, 'geteuid', return_value=0, create=True))
            stack.enter_context(mock.patch.object(engine, 'check_container_ownership'))
            stack.enter_context(mock.patch.object(engine, 'initialize_install'))
            stack.enter_context(mock.patch.object(engine, 'deployment_lock', concurrent_apply))
            write = stack.enter_context(mock.patch.object(engine, 'write_json'))
            images = stack.enter_context(mock.patch.object(engine, 'pin_images'))
            with self.assertRaisesRegex(SafetyError, 'stale'):
                engine.apply(self.c, 4, document, document['review_hash'])
        write.assert_not_called()
        images.assert_not_called()

    def test_recomputed_hash_cannot_change_the_target_stage(self):
        document = self.plan()
        document["stage"] = 6
        document["review_hash"] = digest({k: v for k, v in document.items() if k != "review_hash"})
        with mock.patch.object(engine, "preflight", return_value=self.host), mock.patch.object(engine, "recipe", return_value=self.recipe):
            with self.assertRaisesRegex(SafetyError, "stale"):
                engine.verify_plan(self.c, 4, document, document["review_hash"])

    def test_management_preflight_has_no_nas_dependency(self):
        def fake(argv, **kwargs):
            self.assertEqual(["ip", "-j", "address", "show"], argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"addr_info": [{"local": self.c["bind_address"]}]}]), "")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine, "linux_only"))
            stack.enter_context(mock.patch.object(engine, "host_identity", return_value="fixture"))
            stack.enter_context(mock.patch.object(engine, "check_docker"))
            check_mount = stack.enter_context(mock.patch.object(engine.storage, "check_mount", side_effect=AssertionError("NAS must not be checked")))
            verify = stack.enter_context(mock.patch.object(engine.storage, "verify", side_effect=AssertionError("NAS must not be verified")))
            stack.enter_context(mock.patch.object(engine.storage, "mount_identity", return_value={"fstype": "ext4"}))
            result = engine.preflight(self.c, 4, fake)
        self.assertIsNone(result["mount"])
        check_mount.assert_not_called()
        verify.assert_not_called()

    def test_management_apply_cannot_change_another_stages_guard_configuration(self):
        media = copy.deepcopy(self.c)
        media['install_root'] = str(self.root)
        management = copy.deepcopy(media)
        management['storage']['data_root'] = '/srv/other-data'
        management['storage']['expected_mount'] = {'target': '/', 'source': '/dev/other', 'fstype': 'ext4'}
        for stage in (2, 4):
            engine.stage_path(media, stage).mkdir(parents=True)
            engine.compose_path(media, stage).write_text('{"services":{}}')
        legacy = self.root / 'site.json'
        legacy.write_text('legacy guard configuration retained for unreapplied units\n')
        legacy_before = legacy.read_bytes()

        _, _, media_config = engine.runtime_files(media, 2)
        media_before = media_config.read_bytes()
        _, _, management_config = engine.runtime_files(management, 4)

        self.assertNotEqual(media_config, management_config)
        self.assertEqual(media_config.parent, engine.stage_path(media, 2))
        self.assertEqual(management_config.parent, engine.stage_path(media, 4))
        self.assertEqual(json.loads(media_config.read_text()), media)
        self.assertEqual(json.loads(management_config.read_text()), management)
        self.assertEqual(media_config.read_bytes(), media_before)
        self.assertEqual(legacy.read_bytes(), legacy_before)

    def test_foreign_project_collision_blocks_container_recreation(self):
        calls = []
        def fake(argv, **kwargs):
            calls.append(list(argv))
            if argv[3] == "ps":
                value = "a" * 12 + "\n"
            elif argv[3] == "inspect":
                value = json.dumps({"id": "a" * 64, "name": "/homelab-4-demo-1", "status": "running",
                    "project": "homelab-4", "deployment_id": str(uuid.uuid4()), "stage": "4", "service": "demo", "mounts": []})
            else:
                raise AssertionError("Unexpected container mutation: " + repr(argv))
            return subprocess.CompletedProcess(argv, 0, value, "")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
            stack.enter_context(mock.patch.object(engine, "verify_plan"))
            writes = {name: stack.enter_context(mock.patch.object(engine, name)) for name in
                      ["initialize_install", "_new_directory", "prepare_secrets", "env_files", "write_json"]}
            lock = stack.enter_context(mock.patch.object(engine, "deployment_lock", return_value=nullcontext()))
            stack.enter_context(mock.patch.object(catalog, "stage_directories", return_value=[]))
            images = stack.enter_context(mock.patch.object(engine, "pin_images", return_value=self.recipe))
            installer = stack.enter_context(mock.patch.object(engine, "install_unit"))
            with self.assertRaisesRegex(SafetyError, "deployment"):
                engine.apply(self.c, 4, {}, "unused", runner=fake)
        installer.assert_not_called()
        images.assert_not_called()
        lock.assert_not_called()
        for write in writes.values():
            write.assert_not_called()
        self.assertEqual(["ps", "inspect"], [args[3] for args in calls])

    def test_project_collision_rechecked_under_lock_before_receipt_or_application_writes(self):
        conflict = engine.ProjectCollision({"project": "homelab-4", "requested_deployment_id": self.c["deployment_id"],
            "requested_stage": 4, "containers": []})
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
            stack.enter_context(mock.patch.object(engine, "verify_plan"))
            ownership = stack.enter_context(mock.patch.object(engine, "check_container_ownership", side_effect=[None, conflict]))
            initialized = stack.enter_context(mock.patch.object(engine, "initialize_install"))
            stack.enter_context(mock.patch.object(engine, "deployment_lock", return_value=nullcontext()))
            writes = {name: stack.enter_context(mock.patch.object(engine, name)) for name in
                      ["write_json", "_new_directory", "prepare_secrets", "env_files", "pin_images", "install_unit"]}
            with self.assertRaises(engine.ProjectCollision):
                engine.apply(self.c, 4, {}, "unused")
        initialized.assert_called_once()
        self.assertEqual(2, ownership.call_count)
        for write in writes.values():
            write.assert_not_called()

    def test_same_deployment_wrong_stage_labels_are_rejected(self):
        def fake(argv, **kwargs):
            value = "a" * 12 if argv[3] == "ps" else json.dumps({"id": "a" * 64,
                "name": "/homelab-4-demo-1", "status": "running", "project": "homelab-4",
                "deployment_id": self.c["deployment_id"], "stage": "6", "service": "demo", "mounts": []})
            return subprocess.CompletedProcess(argv, 0, value, "")
        with self.assertRaises(SafetyError):
            engine.check_container_ownership(self.c, 4, fake)

    def pin(self, image, refs):
        def fake(argv, **kwargs):
            self.assertEqual(["docker", "--context", "default"], argv[:3])
            return subprocess.CompletedProcess(argv, 0, json.dumps(refs) if argv[3:5] == ["image", "inspect"] else "", "")
        doc = {"services": {"demo": {"image": image}}}
        with mock.patch.object(engine, "recipe", return_value=doc), mock.patch.object(engine, "read_lock", return_value={}), mock.patch.object(engine, "write_json") as write:
            engine.pin_images(self.c, 4, fake)
        return write.call_args.args[1]["images"][image]

    def test_image_digest_normalizes_docker_hub_library_prefixes(self):
        ref = "docker.io/library/alpine@sha256:" + "a" * 64
        self.assertEqual(ref, self.pin("alpine:3.21", [ref]))

    def test_image_digest_preserves_registry_port(self):
        ref = "registry.example.invalid:5000/team/demo@sha256:" + "b" * 64
        self.assertEqual(ref, self.pin("registry.example.invalid:5000/team/demo:1.2", [ref]))

    def test_digest_from_another_repository_is_not_accepted(self):
        with self.assertRaisesRegex(SafetyError, "repository"):
            self.pin("alpine:3.21", ["someone/unrelated@sha256:" + "c" * 64])

    def test_cached_image_prune_recovers_using_locked_digest_without_tag_upgrade(self):
        image = 'alpine:3.21'
        locked = 'alpine@sha256:' + 'd' * 64
        calls = []
        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[3:5] == ['image','inspect']:
                raise SafetyError('synthetic missing cache')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(engine,'recipe',return_value={'services':{'demo':{'image':image}}}), mock.patch.object(engine,'read_lock',return_value={image:locked}), mock.patch.object(engine,'write_json'):
            engine.pin_images(self.c,4,fake)
        pulls = [args[-1] for args in calls if args[3]=='pull']
        self.assertEqual(pulls,[locked])

    def test_changed_lock_repository_is_refused(self):
        c = copy.deepcopy(self.c)
        c['install_root'] = str(self.root)
        folder = engine.stage_path(c,4)
        folder.mkdir(parents=True)
        (folder/'images.lock.json').write_text(json.dumps({'deployment_id':c['deployment_id'],'images':{'alpine:3.21':'someone/unrelated@sha256:'+'e'*64}}))
        with self.assertRaisesRegex(SafetyError,'repository'):
            engine.read_lock(c,4)

    def test_service_health_rejects_unknown_starting_and_missing_services(self):
        for rows in [[{"Service": "demo", "State": "running", "Health": "unknown"}],
                     [{"Service": "demo", "State": "running", "Health": "starting"}], []]:
            fake = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
            with mock.patch.object(engine, "recipe", return_value=self.recipe):
                with self.assertRaises(SafetyError):
                    engine.service_health(self.c, 4, fake)

    def verify(self, report):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine, "preflight", return_value=self.host))
            stack.enter_context(mock.patch.object(engine, "service_health", return_value={"demo": {"State": "running", "Health": "healthy"}}))
            stack.enter_context(mock.patch.object(engine, "check_container_ownership"))
            stack.enter_context(mock.patch.object(engine, "write_json"))
            stack.enter_context(mock.patch.object(integrations, "verify_stage", return_value=report))
            return engine.verify(self.c, 4)

    def test_running_containers_do_not_turn_pending_wiring_green(self):
        for report in [{"status": "pending", "pending": [{"name": "another:step"}], "errors": []},
                       {"status": "unknown", "pending": [], "errors": []},
                       {"status": "failed", "pending": [], "errors": [{"name": "api:broken"}]}]:
            with self.assertRaises(SafetyError):
                self.verify(report)

    def test_malformed_verification_reports_fail_usefully(self):
        for report in [None, [], "unknown"]:
            with self.subTest(report=report):
                with self.assertRaises(SafetyError):
                    self.verify(report)

    def attestation(self):
        value = {"deployment_id": self.c["deployment_id"], "config_digest": digest(self.c), "stage": 4,
                 "checks": engine.MANUAL_CHECKS[4], "method": "operator_attestation; not automated API verification"}
        (self.root / "manual-enrollment.json").write_text(json.dumps(value))
        return mock.patch.object(engine, "stage_path", return_value=self.root), mock.patch.object(engine, "private_read", return_value=json.dumps(value))

    @staticmethod
    def gui_report():
        return {"stage": 4, "mode": "verify", "status": "pending", "ok": False,
                "checks": [{"name": "management:dockge-http", "status": "passed"}, {"name": "management:uptime-kuma-http", "status": "passed"}],
                "errors": [], "pending": [{"name": "management:enrollment", "action": "Enroll administrator"}]}

    def test_attestation_covers_only_expected_gui_enrollment(self):
        with ExitStack() as stack:
            for patch in self.attestation():
                stack.enter_context(patch)
            result = self.verify(self.gui_report())
            self.assertEqual("verified_with_manual_checks", result["status"])
            unknown = self.gui_report()
            unknown["pending"].append({"name": "storage:unknown"})
            with self.assertRaises(SafetyError):
                self.verify(unknown)

    def test_attestation_cannot_override_unknown_or_failed_status(self):
        with ExitStack() as stack:
            for patch in self.attestation():
                stack.enter_context(patch)
            for status in ["unknown", "failed"]:
                report = self.gui_report()
                report["status"] = status
                with self.subTest(status=status):
                    with self.assertRaises(SafetyError):
                        self.verify(report)

    def test_no_attestation_path_for_media_or_photo_api_checks(self):
        report = {"status": "pending", "pending": [{"name": "management:enrollment"}], "errors": []}
        self.assertIsNone(engine.manual_enrollment(self.c, 2, report))
        self.assertIsNone(engine.manual_enrollment(self.c, 5, report))


class ProjectCollisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.20")
        self.c["install_root"] = str(self.root / "application")
        self.enterContext(mock.patch.object(installation_registry, "REGISTRY_ROOT", self.root / "registry"))
        self.enterContext(mock.patch.object(engine.naming, "UNIT_ROOT", self.root / "units"))
        self.old_id = str(uuid.uuid4())
        self.calls = []
        self.rows = [{"id": "a" * 64, "name": "/homelab-2-bazarr-1", "status": "running",
            "project": "homelab-2", "deployment_id": self.old_id, "stage": "2", "service": "bazarr",
            "mounts": [{"type": "bind", "source": "/opt/original/appdata/bazarr", "destination": "/config"},
                       {"type": "bind", "source": "/srv/media", "destination": "/data"}],
            "Config": {"Env": ["SECRET=fixture-private-do-not-display"]}, "unrelated": "fixture-private-do-not-display"}]

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.assertEqual(["docker", "--context", "default"], argv[:3])
        if argv[3] == "ps":
            return subprocess.CompletedProcess(argv, 0, "\n".join(row["id"][:12] for row in self.rows), "")
        self.assertEqual("inspect", argv[3])
        template = argv[-1]
        self.assertNotIn(".Config.Env", template)
        self.assertNotIn("{{json .}}", template)
        self.assertNotIn("{{json .Config}}", template)
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(row) for row in self.rows), "")

    def test_structured_collision_names_old_and_new_deployments_and_mount_existence(self):
        def path(value):
            return mock.Mock(exists=mock.Mock(return_value=value == "/srv/media"))
        with mock.patch.object(engine, "Path", side_effect=path):
            with self.assertRaises(engine.ProjectCollision) as captured:
                engine.check_container_ownership(self.c, 2, self.runner)
        conflict = captured.exception
        self.assertIsInstance(conflict, SafetyError)
        self.assertEqual(self.c["deployment_id"], conflict.details["requested_deployment_id"])
        row = conflict.details["containers"][0]
        self.assertEqual("homelab-2-bazarr-1", row["name"])
        self.assertEqual(self.old_id, row["deployment_id"])
        self.assertFalse(row["mounts"][0]["source_exists"])
        self.assertTrue(row["mounts"][0]["appdata"])
        self.assertTrue(row["mounts"][1]["source_exists"])
        message = str(conflict)
        for value in (self.old_id, self.c["deployment_id"], "running", "/opt/original/appdata/bazarr", "host source missing"):
            self.assertIn(value, message)
        self.assertIn("original site configuration", message)
        self.assertNotIn("choose a unique", message)
        self.assertNotIn("fixture-private", message)
        self.assertNotIn("fixture-private", json.dumps(conflict.details))
        self.assertEqual(["ps", "inspect"], [row[3] for row in self.calls])

    def test_owned_project_checks_appdata_without_writes(self):
        self.rows[0]["deployment_id"] = self.c["deployment_id"]
        path = mock.Mock(stat=mock.Mock(return_value=types.SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=2)))
        with mock.patch.object(engine, "no_symlinks", return_value=path) as checked:
            self.assertIsNone(engine.project_conflicts(self.c, 2, self.runner))
            engine.check_container_ownership(self.c, 2, self.runner)
        self.assertEqual([mock.call("/opt/original/appdata/bazarr")] * 2, checked.call_args_list)
        self.assertTrue(all(call[3] in {"ps", "inspect"} for call in self.calls))

    def test_owned_missing_or_nondirectory_appdata_blocks_before_any_apply_writes(self):
        self.rows[0]["deployment_id"] = self.c["deployment_id"]
        for problem in (FileNotFoundError(), PermissionError(), types.SimpleNamespace(st_mode=stat.S_IFREG)):
            with self.subTest(problem=type(problem).__name__), ExitStack() as stack:
                stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
                stack.enter_context(mock.patch.object(engine, "verify_plan"))
                path = mock.Mock()
                path.stat.side_effect = problem if isinstance(problem, Exception) else None
                path.stat.return_value = problem
                stack.enter_context(mock.patch.object(engine, "no_symlinks", return_value=path))
                writes = {name: stack.enter_context(mock.patch.object(engine, name)) for name in
                          ("initialize_install", "deployment_lock", "write_json", "_new_directory", "pin_images", "install_unit")}
                with self.assertRaisesRegex(SafetyError, "appdata path.*setup --recover"):
                    engine.apply(self.c, 2, {}, "unused", runner=self.runner)
                for write in writes.values():
                    write.assert_not_called()

    def test_owned_symlink_appdata_is_refused_without_echoing_source(self):
        self.rows[0]["deployment_id"] = self.c["deployment_id"]
        with mock.patch.object(engine, "no_symlinks", side_effect=SafetyError("private-symlink-target")):
            with self.assertRaisesRegex(SafetyError, "appdata path") as captured:
                engine.check_container_ownership(self.c, 2, self.runner)
        self.assertNotIn("private-symlink-target", str(captured.exception))

    def test_running_owned_appdata_must_match_live_bind_inode(self):
        self.rows[0].update(deployment_id=self.c["deployment_id"], pid=12345)
        host = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=2)
        old = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=3)
        for live, success in ((host, True), (old, False)):
            with self.subTest(matching=success), mock.patch.object(engine.sys, "platform", "linux"), \
                 mock.patch.object(engine.naming, "check_installed_names"), \
                 mock.patch.object(engine, "no_symlinks", return_value=mock.Mock(stat=mock.Mock(return_value=host))), \
                 mock.patch.object(engine.os, "stat", return_value=live) as inspected:
                if success:
                    engine.check_container_ownership(self.c, 2, self.runner)
                else:
                    with self.assertRaisesRegex(SafetyError, "appdata path"):
                        engine.check_container_ownership(self.c, 2, self.runner)
                inspected.assert_called_once_with("/proc/12345/root/config")

    def test_live_appdata_identity_unreadable_fails_closed(self):
        self.rows[0].update(deployment_id=self.c["deployment_id"], pid=12345)
        host = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=2)
        with mock.patch.object(engine.sys, "platform", "linux"), \
             mock.patch.object(engine.naming, "check_installed_names"), \
             mock.patch.object(engine, "no_symlinks", return_value=mock.Mock(stat=mock.Mock(return_value=host))), \
             mock.patch.object(engine.os, "stat", side_effect=PermissionError()):
            with self.assertRaisesRegex(SafetyError, "appdata path"):
                engine.check_container_ownership(self.c, 2, self.runner)

    def test_no_containers_needs_no_inspect(self):
        self.rows = []
        self.assertIsNone(engine.project_conflicts(self.c, 2, self.runner))
        self.assertEqual(["ps"], [row[3] for row in self.calls])

    def test_unknown_labels_and_control_or_credential_paths_are_not_echoed(self):
        self.rows[0].update(name="fixture-private\x1b[31m", deployment_id="fixture-private-unrecognized-label")
        self.rows[0]["mounts"][0]["source"] = "/mnt/https://fixture-user:" + "fixture-private-password@host"
        self.rows[0]["mounts"][1]["source"] = "/mnt/fixture-private\nsource"
        with self.assertRaises(engine.ProjectCollision) as captured:
            engine.check_container_ownership(self.c, 2, self.runner)
        self.assertNotIn("fixture-private", str(captured.exception))
        self.assertNotIn("\x1b", str(captured.exception))
        self.assertEqual("[unrecognized]", captured.exception.details["containers"][0]["deployment_id"])

    def test_incomplete_invalid_or_duplicate_container_inventory_fails_closed(self):
        for output in ("not-json", "", json.dumps({"id": "b" * 64}),
                       "\n".join([json.dumps(self.rows[0])] * 2)):
            with self.subTest(output=output[:20]):
                def runner(argv, **kwargs):
                    value = "a" * 12 if argv[3] == "ps" else output
                    return subprocess.CompletedProcess(argv, 0, value, "")
                with self.assertRaises(SafetyError):
                    engine.check_container_ownership(self.c, 2, runner)


class ObsoleteUnitIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config()
        self.c["install_root"] = str(self.root / "application")
        self.units, self.archive = self.root / "units", self.root / "archive"
        self.units.mkdir()
        self.unit = self.units / "samurai-homelab-4.service"
        self.old_id = str(uuid.uuid4())
        self.original = ("# Managed by Samurai Stack: " + self.old_id + "\n[Service]\n").encode()
        self.unit.write_bytes(self.original)
        self.action = {"status": "retired", "unit_name": self.unit.name, "old_deployment_id": self.old_id,
                       "archive_path": str(self.archive / self.unit.name)}

    def retirement_module(self, function):
        module = types.ModuleType("samurai_stack.retired_units")
        module.retire_conflicting = function
        return mock.patch.dict(sys.modules, {"samurai_stack.retired_units": module})

    def test_generated_units_retry_guarded_failures_without_expiring_or_overriding_manual_stops(self):
        self.unit.unlink()
        root = Path(self.c["install_root"])
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        for stage in (2, 3, 4, 5, 6):
            with self.subTest(stage=stage), mock.patch.object(engine, "runtime_files",
                    return_value=(root, root / "runtime_guard.py", root / "site.json")):
                name = engine.install_unit(self.c, stage, runner, unit_root=self.units, archive_root=self.archive)
                unit = configparser.ConfigParser(interpolation=None)
                unit.read(self.units / name)
                # After many failed startup attempts, systemd must still reach
                # the same guard, rather than remain start-limit-hit forever.
                self.assertEqual("0", unit["Unit"]["StartLimitIntervalSec"])
                self.assertNotIn("StartLimitBurst", unit["Unit"])
                self.assertEqual("on-failure", unit["Service"]["Restart"])
                self.assertEqual("15", unit["Service"]["RestartSec"])
                self.assertEqual("180", unit["Service"]["TimeoutStartSec"])
                self.assertEqual("/usr/bin/python3 " + str(root / "runtime_guard.py") + " "
                                 + str(root / "site.json") + " " + str(stage), unit["Service"]["ExecStartPre"])
                self.assertEqual("/usr/bin/python3 " + str(root / "runtime_startup.py") + " "
                                 + str(root / "site.json") + " " + str(stage), unit["Service"]["ExecStartPost"])
                self.assertEqual("docker.service", unit["Unit"]["Requires"])
                self.assertEqual("docker.service", unit["Unit"]["PartOf"])
                required = str(root) if stage == 4 else self.c["storage"]["data_root"] + " " + str(root)
                self.assertEqual(required, unit["Unit"]["RequiresMountsFor"])
                self.assertTrue(unit["Service"]["ExecStop"].endswith(" stop --timeout 45"))
                self.assertTrue(unit["Service"]["ExecStart"].endswith(
                    " up --no-color --force-recreate --abort-on-container-failure --pull never"))
                self.assertEqual(runner.call_args.kwargs, {"timeout": 200})

    def test_foreign_unit_is_archived_before_runtime_files_are_overwritten(self):
        order, actions = [], []

        def retire(config, stage, **kwargs):
            self.assertEqual((self.c, 4), (config, stage))
            self.assertEqual(self.units, kwargs["unit_root"])
            self.assertEqual(self.archive, kwargs["archive_root"])
            self.archive.mkdir()
            self.unit.rename(self.archive / self.unit.name)
            order.append("retire")
            return self.action

        def runtime(config, stage):
            self.assertFalse(self.unit.exists())
            self.assertEqual(self.original, (self.archive / self.unit.name).read_bytes())
            order.append("runtime")
            root = Path(self.c["install_root"])
            return root, root / "runtime_guard.py", root / "stages/homelab-4/site.json"

        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with self.retirement_module(retire), mock.patch.object(engine, "runtime_files", side_effect=runtime):
            value = engine.install_unit(self.c, 4, runner, unit_root=self.units, archive_root=self.archive, actions=actions)
        self.assertEqual(["retire", "runtime"], order)
        self.assertEqual(self.unit.name, value)
        self.assertEqual([self.action], actions)
        self.assertTrue(self.unit.read_text().startswith("# Managed by Samurai Stack: " + self.c["deployment_id"]))
        self.assertEqual(["daemon-reload", "enable", "reset-failed", "restart"], [call.args[0][1] for call in runner.call_args_list])
        self.assertEqual(["systemctl", "reset-failed", self.unit.name], runner.call_args_list[2].args[0])

    def test_unsafe_old_unit_never_rewrites_runtime_or_unit_files(self):
        retirement = mock.Mock(side_effect=SafetyError("Existing unit could not be safely retired"))
        runner = mock.Mock()
        with self.retirement_module(retirement), mock.patch.object(engine, "runtime_files") as runtime, \
             mock.patch.object(engine, "atomic_write") as write:
            with self.assertRaisesRegex(SafetyError, "safely retired"):
                engine.install_unit(self.c, 4, runner, unit_root=self.units, archive_root=self.archive)
        runtime.assert_not_called()
        write.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(self.original, self.unit.read_bytes())

    def test_real_retirement_module_archives_before_engine_runtime_and_reinstall(self):
        from samurai_stack import retired_units
        try:
            from tests.test_retired_units import Runner, generated
        except ModuleNotFoundError:
            from test_retired_units import Runner, generated
        c = default_config()
        self.unit.write_text(generated(c, stage=4, old=self.old_id))
        self.unit.chmod(0o644)
        original = self.unit.read_bytes()
        runner = Runner()
        runner.fragment_path = str(self.unit)
        order, actions = [], []

        def execute(argv, **kwargs):
            if argv[:2] == ["systemctl", "stop"]:
                order.append("stop")
            if argv[0] == "systemctl" and argv[1] in {"enable", "restart", "start"}:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return runner(argv, **kwargs)

        def runtime(config, stage):
            order.append("runtime")
            self.assertFalse(self.unit.exists())
            root = Path(c["install_root"])
            return root, root / "runtime_guard.py", root / "stages/homelab-4/site.json"

        with mock.patch.object(retired_units, "_guard"), mock.patch.object(retired_units, "_owner"), \
             mock.patch.object(engine, "runtime_files", side_effect=runtime):
            engine.install_unit(c, 4, execute, unit_root=self.units, archive_root=self.archive, actions=actions)
        self.assertEqual(["stop", "runtime"], order)
        self.assertEqual(1, len(actions))
        self.assertEqual("obsolete_unit_archived", actions[0]["status"])
        self.assertEqual(original, Path(actions[0]["archive_path"]).read_bytes())
        self.assertTrue(self.unit.read_text().startswith("# Managed by Samurai Stack: " + c["deployment_id"]))

    def test_new_unit_reset_notfound_can_start_but_other_reset_failures_stop(self):
        self.unit.unlink()
        root = Path(self.c["install_root"])
        for load_state, allowed in (("not-found", True), ("loaded", False)):
            calls = []
            def runner(argv, **kwargs):
                calls.append(list(argv))
                if argv[1] == "reset-failed":
                    raise SafetyError("systemctl failed")
                return subprocess.CompletedProcess(argv, 0, load_state if argv[1] == "show" else "", "")
            with self.subTest(load_state=load_state), mock.patch.object(engine, "runtime_files", return_value=(root, root / "runtime_guard.py", root / "site.json")):
                if allowed:
                    engine.install_unit(self.c, 4, runner, unit_root=self.units, archive_root=self.archive)
                    self.assertEqual("restart", calls[-1][1])
                else:
                    with self.assertRaises(SafetyError):
                        engine.install_unit(self.c, 4, runner, unit_root=self.units, archive_root=self.archive)
                    self.assertTrue(all(call[1] not in {"start", "restart"} for call in calls))

    def test_apply_retires_under_lock_before_receipt_images_or_appdata_preparation(self):
        action = self.action

        def retirement(config, stage, runner, *, actions):
            actions.append(action)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
            stack.enter_context(mock.patch.object(engine, "verify_plan"))
            stack.enter_context(mock.patch.object(engine, "check_container_ownership"))
            stack.enter_context(mock.patch.object(engine, "initialize_install"))
            stack.enter_context(mock.patch.object(engine, "deployment_lock", return_value=nullcontext()))
            retire = stack.enter_context(mock.patch.object(engine, "retire_obsolete_unit", side_effect=retirement))
            stack.enter_context(mock.patch.object(engine, "write_json"))
            stack.enter_context(mock.patch.object(engine, "_new_directory"))
            stack.enter_context(mock.patch.object(catalog, "stage_directories", return_value=[]))
            stack.enter_context(mock.patch.object(engine, "prepare_secrets"))
            stack.enter_context(mock.patch("samurai_stack.management_setup.reserve_fresh"))
            stack.enter_context(mock.patch.object(engine, "env_files"))
            stack.enter_context(mock.patch.object(engine, "pin_images", return_value={"services": {"demo": {}}}))
            unit = stack.enter_context(mock.patch.object(engine, "install_unit", return_value=self.unit.name))
            stack.enter_context(mock.patch.object(engine, "docker"))
            stack.enter_context(mock.patch.object(engine, "service_health", return_value={"demo": {"State": "running"}}))
            receipt = engine.apply(self.c, 4, {}, "unused")
        self.assertEqual([action], receipt["recovery_actions"])
        self.assertEqual([action], receipt["result"]["recovery_actions"])
        retire.assert_called_once()
        self.assertEqual([action], unit.call_args.kwargs["actions"])

    def test_refused_retirement_under_lock_precedes_receipt_and_appdata_writes(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
            stack.enter_context(mock.patch.object(engine, "verify_plan"))
            stack.enter_context(mock.patch.object(engine, "check_container_ownership"))
            stack.enter_context(mock.patch.object(engine, "initialize_install"))
            stack.enter_context(mock.patch.object(engine, "deployment_lock", return_value=nullcontext()))
            stack.enter_context(mock.patch.object(engine, "retire_obsolete_unit", side_effect=SafetyError("Cannot retire")))
            writes = {name: stack.enter_context(mock.patch.object(engine, name)) for name in
                      ("write_json", "_new_directory", "pin_images", "prepare_secrets", "install_unit")}
            with self.assertRaisesRegex(SafetyError, "Cannot retire"):
                engine.apply(self.c, 4, {}, "unused")
        for write in writes.values():
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
