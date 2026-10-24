"""Coordinator boundary tests; commands, APIs and production writes are injected."""
from contextlib import ExitStack, contextmanager, nullcontext
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

from samurai_stack import catalog, engine, integrations
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
                value = "foreign-container-id\n"
            elif argv[3] == "inspect":
                value = json.dumps({"systems.samarie.deployment": str(uuid.uuid4()), "systems.samarie.stage": "4"})
            else:
                raise AssertionError("Unexpected container mutation: " + repr(argv))
            return subprocess.CompletedProcess(argv, 0, value, "")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine.os, "geteuid", return_value=0, create=True))
            for name in ["verify_plan", "initialize_install", "_new_directory", "prepare_secrets", "env_files"]:
                stack.enter_context(mock.patch.object(engine, name))
            stack.enter_context(mock.patch.object(engine, "deployment_lock", return_value=nullcontext()))
            stack.enter_context(mock.patch.object(engine, "write_json"))
            stack.enter_context(mock.patch.object(catalog, "stage_directories", return_value=[]))
            stack.enter_context(mock.patch.object(engine, "pin_images", return_value=self.recipe))
            installer = stack.enter_context(mock.patch.object(engine, "install_unit"))
            with self.assertRaisesRegex(SafetyError, "deployment"):
                engine.apply(self.c, 4, {}, "unused", runner=fake)
        installer.assert_not_called()
        self.assertEqual(["ps", "inspect"], [args[3] for args in calls])

    def test_same_deployment_wrong_stage_labels_are_rejected(self):
        def fake(argv, **kwargs):
            value = "container-id" if argv[3] == "ps" else json.dumps({"systems.samarie.deployment": self.c["deployment_id"], "systems.samarie.stage": "6"})
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


if __name__ == "__main__":
    unittest.main()
