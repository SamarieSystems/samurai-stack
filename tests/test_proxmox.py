import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid

from samurai_stack import proxmox


class FakePVE:
    """A stateful command boundary, never calls a real executable."""
    def __init__(self):
        self.calls = []
        self.configs = {}
        self.states = {}
        self.storage = {"storage": "local-lvm", "active": 1, "content": "images,rootdir", "avail": 500 * 2**30}
        self.node = "test-pve"
        self.machine = "a" * 32
        self.image = {"format": "qcow2", "virtual-size": 3 * 2**30}
        self.fail = None
        self.agent = False
        self.memory = 32 * 2**30
        self.pending = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail and self.fail(argv):
            return subprocess.CompletedProcess(argv, 1, "", "fake command failed")
        if argv[0] == "cat":
            result = self.machine
        elif argv[0] == "qemu-img":
            result = self.image
        elif argv[0] == "pvesh":
            endpoint = argv[2]
            if endpoint == "/cluster/status":
                result = [{"type": "node", "name": self.node, "local": 1}]
            elif endpoint == "/cluster/resources":
                result = [{"vmid": key, "type": value.get("resource_type", "qemu"), "node": self.node, "maxmem": int(value["memory"]) * 2**20} for key, value in self.configs.items()]
            elif endpoint.endswith("/storage"):
                result = [self.storage]
            elif endpoint.endswith("/network"):
                result = [{"iface": "vmbr0", "type": "bridge", "active": 1}]
            elif endpoint.endswith("/hardware/pci"):
                result = []
            elif "/qemu/" in endpoint:
                vmid = int(endpoint.split("/")[4])
                if endpoint.endswith("/config"):
                    result = self.configs[vmid]
                elif endpoint.endswith("/pending"):
                    result = self.pending
                else:
                    result = {"status": self.states.get(vmid, "stopped")}
            elif endpoint.endswith("/status"):
                result = {"cpuinfo": {"cpus": 8}, "memory": {"total": self.memory}}
            else:
                raise AssertionError(argv)
        elif argv[:2] == ["qm", "create"]:
            vmid = int(argv[2])
            assert vmid not in self.configs
            fields = dict(zip((key[2:] for key in argv[3::2]), argv[4::2]))
            fields["sshkeys"] = Path(fields.pop("sshkeys")).read_text().strip()
            fields["net0"] = fields["net0"].replace("virtio,", "virtio=AA:BB:CC:DD:EE:FF,")
            self.configs[vmid] = fields
            result = ""
        elif argv[:2] == ["qm", "set"]:
            vmid, field, value = int(argv[2]), argv[3][2:], argv[4]
            if field == "scsi0":
                value = f"local-lvm:vm-{vmid}-disk-0,discard=on,size=3G"
            elif field == "ide2":
                value = f"local-lvm:vm-{vmid}-cloudinit,media=cdrom"
            self.configs[vmid][field] = value
            result = ""
        elif argv[:2] == ["qm", "resize"]:
            vmid = int(argv[2])
            old = self.configs[vmid]["scsi0"].split(",size=")[0]
            self.configs[vmid]["scsi0"] = old + ",size=" + argv[4]
            result = ""
        elif argv[:2] == ["qm", "start"]:
            self.states[int(argv[2])] = "running"
            result = ""
        elif argv[:2] == ["qm", "agent"]:
            return subprocess.CompletedProcess(argv, 0 if self.agent else 1, "", "")
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result) if isinstance(result, (dict, list)) else result, "")

    @property
    def mutations(self):
        return [call for call in self.calls if call[0] == "qm" and call[1] not in {"agent"}]


class ProxmoxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        image = self.root / "debian.qcow2"
        image.write_bytes(b"QFI\xfb" + b"\0" * 128)
        public = self.root / "operator.pub"
        algorithm = b"ssh-ed25519"
        blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + b"k" * 32
        public.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " operator@example\n")
        self.config = {"deployment_id": str(uuid.uuid4()), "node": "test-pve", "storage": "local-lvm", "bridge": "vmbr0", "image_path": str(image), "image_sha512": hashlib.sha512(image.read_bytes()).hexdigest(), "ssh_public_key": str(public), "state_dir": str(self.root / "private" / "proxmox"), "vms": [{"id": 120, "name": "media", "cores": 2, "memory_mib": 2048, "disk_gib": 32}]}
        self.run = FakePVE()

    def plan(self):
        return proxmox.plan(self.config, self.run)

    def deploy(self):
        plan = self.plan()
        return plan, proxmox.apply(self.config, plan, self.run)

    def test_plan_is_read_only_and_binds_image_key_and_host(self):
        plan = self.plan()
        self.assertEqual([], self.run.mutations)
        self.assertFalse(Path(self.config["state_dir"]).exists())
        self.assertEqual("a" * 32, plan["host"]["machine_id"])
        self.assertEqual(self.config["image_sha512"], plan["image_sha512"])
        self.assertTrue(plan["ssh_key_fingerprint"].startswith("SHA256:"))

    def test_new_vm_then_repeat_produces_no_additional_mutations(self):
        plan, result = self.deploy()
        self.assertEqual("defined", result["state"])
        self.assertEqual("running", self.run.states[120])
        self.assertIn("size=32G", self.run.configs[120]["scsi0"])
        before = len(self.run.mutations)
        proxmox.apply(self.config, plan, self.run)
        self.assertEqual(before, len(self.run.mutations))

    def test_foreign_vm_is_never_adopted(self):
        self.run.configs[120] = {"memory": "2048"}
        with self.assertRaisesRegex(proxmox.ProxmoxError, "refusing adoption"):
            self.plan()
        self.assertEqual([], self.run.mutations)

    def test_container_vm_id_collision_is_rejected(self):
        self.run.configs[120] = {"memory": "2048", "resource_type": "lxc"}
        with self.assertRaisesRegex(proxmox.ProxmoxError, "container"):
            self.plan()

    def test_plan_tampering_even_with_recomputed_hash_is_rejected(self):
        plan = self.plan()
        plan["vms"][0]["commands"]["create"] = ["qm", "destroy", "120"]
        plan["review_hash"] = proxmox._hash({k: v for k, v in plan.items() if k != "review_hash"})
        with self.assertRaisesRegex(proxmox.ProxmoxError, "differs"):
            proxmox.apply(self.config, plan, self.run)
        self.assertFalse(self.run.mutations)

    def test_plan_without_valid_hash_is_rejected(self):
        plan = self.plan()
        plan["review_hash"] = "wrong"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "integrity"):
            proxmox.apply(self.config, plan, self.run)

    def test_stale_or_live_apply_lock_is_never_removed_automatically(self):
        plan = self.plan()
        lock = Path(self.config["state_dir"]) / self.config["deployment_id"] / "apply.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text('{"pid": 999999, "token": "another-invocation"}')
        with self.assertRaisesRegex(proxmox.ProxmoxError, "stale locks are never removed"):
            proxmox.apply(self.config, plan, self.run)
        self.assertTrue(lock.exists())
        self.assertFalse(self.run.mutations)

    def test_normal_completion_releases_only_its_transient_lock(self):
        self.deploy()
        lock = Path(self.config["state_dir"]) / self.config["deployment_id"] / "apply.lock"
        self.assertFalse(lock.exists())

    def test_lock_is_released_after_recorded_failure(self):
        plan = self.plan()
        self.run.fail = lambda args: args[:2] == ["qm", "set"] and "--boot" in args
        with self.assertRaises(proxmox.ProxmoxError):
            proxmox.apply(self.config, plan, self.run)
        lock = Path(self.config["state_dir"]) / self.config["deployment_id"] / "apply.lock"
        self.assertFalse(lock.exists())

    def test_different_physical_host_invalidates_plan(self):
        plan = self.plan()
        self.run.machine = "b" * 32
        with self.assertRaisesRegex(proxmox.ProxmoxError, "differs"):
            proxmox.apply(self.config, plan, self.run)
        self.assertFalse(self.run.mutations)

    def test_changed_image_rejected_before_mutation(self):
        plan = self.plan()
        Path(self.config["image_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(proxmox.ProxmoxError, "SHA512"):
            proxmox.apply(self.config, plan, self.run)
        self.assertFalse(self.run.mutations)

    def test_backing_image_rejected(self):
        self.run.image["backing-filename"] = "/dev/sda"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "backing"):
            self.plan()

    def test_source_disk_never_shrunk(self):
        self.run.image["virtual-size"] = 64 * 2**30
        with self.assertRaisesRegex(proxmox.ProxmoxError, "shrink"):
            self.plan()

    def test_qcow2_external_data_file_is_rejected(self):
        self.run.image["format-specific"] = {"data": {"data-file": "/dev/sda"}}
        with self.assertRaisesRegex(proxmox.ProxmoxError, "external data"):
            self.plan()

    def test_pending_proxmox_changes_stop_repeat_before_mutation(self):
        plan, _ = self.deploy()
        self.run.pending = [{"key": "scsi0", "pending": "local-lvm:vm-999-disk-0"}]
        before = len(self.run.mutations)
        with self.assertRaisesRegex(proxmox.ProxmoxError, "pending Proxmox changes"):
            proxmox.apply(self.config, plan, self.run)
        self.assertEqual(before, len(self.run.mutations))

    def test_insufficient_storage_is_rejected(self):
        self.run.storage["avail"] = 4 * 2**30
        with self.assertRaisesRegex(proxmox.ProxmoxError, "free storage"):
            self.plan()

    def test_host_ram_reserve_is_enforced(self):
        self.run.memory = 3 * 2**30
        with self.assertRaisesRegex(proxmox.ProxmoxError, "host reserve"):
            self.plan()

    def test_inactive_or_wrong_storage_refused(self):
        self.run.storage["active"] = 0
        with self.assertRaisesRegex(proxmox.ProxmoxError, "active Proxmox storage"):
            self.plan()

    def test_failure_preserves_vm_and_receipt_and_retry_resumes(self):
        plan = self.plan()
        self.run.fail = lambda args: args[:2] == ["qm", "set"] and "--boot" in args
        with self.assertRaisesRegex(proxmox.ProxmoxError, "Partial VM/disks were preserved"):
            proxmox.apply(self.config, plan, self.run)
        self.assertIn("scsi0", self.run.configs[120])
        receipt = next(Path(self.config["state_dir"]).rglob("vm-120.json"))
        self.assertEqual("failed", json.loads(receipt.read_text())["state"])
        self.run.fail = None
        proxmox.apply(self.config, plan, self.run)
        self.assertEqual(1, len([a for a in self.run.mutations if a[:2] == ["qm", "create"]]))
        self.assertEqual("defined", json.loads(receipt.read_text())["state"])
        self.assertFalse(any("destroy" in a or "free" in a for a in self.run.calls))

    def test_unused_disk_stops_retry_without_deleting_it(self):
        plan, _ = self.deploy()
        self.run.configs[120]["unused0"] = "local-lvm:vm-120-disk-7"
        before = len(self.run.mutations)
        with self.assertRaisesRegex(proxmox.ProxmoxError, "unused disks"):
            proxmox.apply(self.config, plan, self.run)
        self.assertEqual(before, len(self.run.mutations))
        self.assertIn("unused0", self.run.configs[120])

    def test_existing_network_drift_is_not_overwritten(self):
        plan, _ = self.deploy()
        self.run.configs[120]["net0"] = "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr9,firewall=1"
        before = len(self.run.mutations)
        with self.assertRaisesRegex(proxmox.ProxmoxError, "network differs"):
            proxmox.apply(self.config, plan, self.run)
        self.assertEqual(before, len(self.run.mutations))

    def test_existing_boot_order_drift_is_not_overwritten(self):
        plan, _ = self.deploy()
        self.run.configs[120]["boot"] = "order=net0"
        before = len(self.run.mutations)
        with self.assertRaisesRegex(proxmox.ProxmoxError, "boot order changed"):
            proxmox.apply(self.config, plan, self.run)
        self.assertEqual(before, len(self.run.mutations))

    def test_root_disk_from_another_vm_is_not_reused(self):
        plan, _ = self.deploy()
        self.run.configs[120]["scsi0"] = "local-lvm:vm-999-disk-0,size=32G"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "not an owned volume"):
            proxmox.apply(self.config, plan, self.run)

    def test_ssh_key_drift_is_detected(self):
        plan, _ = self.deploy()
        self.run.configs[120]["sshkeys"] = ""
        with self.assertRaises(proxmox.ProxmoxError):
            proxmox.apply(self.config, plan, self.run)

    def test_cpu_only_configuration_works_and_gpu_request_is_explicitly_blocked(self):
        self.plan()
        self.config["vms"][0]["gpu_pci"] = "0000:01:00.0"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "manual reviewed prerequisite"):
            self.plan()

    def test_guest_agent_is_not_misreported_as_completed_bootstrap(self):
        self.deploy()
        self.run.agent = True
        result = proxmox.verify(self.config, self.run)
        self.assertTrue(result["vms"][0]["guest_agent_reachable"])
        self.assertFalse(result["vms"][0]["guest_bootstrap_verified"])
        self.assertFalse(result["ready"])

    def test_no_automatic_start_when_opted_out(self):
        self.config["vms"][0]["start"] = False
        self.deploy()
        self.assertNotIn(120, self.run.states)

    def test_new_server_vms_start_at_host_boot_unless_explicitly_disabled(self):
        self.deploy()
        self.assertEqual("1", self.run.configs[120]["onboot"])

    def test_unreviewed_cloud_init_package_upgrade_is_explicitly_disabled(self):
        self.deploy()
        self.assertEqual("0", self.run.configs[120]["ciupgrade"])

    def test_cloud_init_upgrade_drift_is_not_silently_accepted(self):
        plan, _ = self.deploy()
        self.run.configs[120]["ciupgrade"] = "1"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "ciupgrade"):
            proxmox.apply(self.config, plan, self.run)

    def test_invalid_gateway_and_duplicate_ids_are_rejected(self):
        self.config["vms"][0]["ipconfig"] = "ip=192.0.2.30/24,gw=198.51.100.1"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "gateway"):
            self.plan()
        self.config["vms"][0]["ipconfig"] = "ip=dhcp"
        self.config["vms"].append(copy.deepcopy(self.config["vms"][0]))
        with self.assertRaisesRegex(proxmox.ProxmoxError, "distinct"):
            self.plan()

    def test_command_runner_failure_is_useful_and_does_not_print_stderr(self):
        self.run.fail = lambda args: args[0] == "pvesh"
        with self.assertRaisesRegex(proxmox.ProxmoxError, "pvesh get failed"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
