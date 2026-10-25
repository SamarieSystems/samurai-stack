"""Sandboxed bootstrap tests: extracted Python, fake guest root, fake commands.

No package manager, service manager, network request or real host write occurs.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-host.sh"


def module():
    text = SCRIPT.read_text()
    python = text.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {"__name__": "bootstrap_sandbox"}
    exec(compile(python, str(SCRIPT), "exec"), namespace)
    return types.SimpleNamespace(**namespace), namespace


class FakeGuest:
    def __init__(self):
        self.calls = []
        self.installed = {}
        self.key = b"fake verified public key artifact\n"
        self.vm = "kvm"
        self.container = "none"
        self.arch = "amd64"
        self.compose = "2.30.0"
        self.simulation = "Inst example (1.0 Debian:stable [amd64])"
        self.fail_command = None

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_command and self.fail_command(argv):
            return subprocess.CompletedProcess(argv, 1, "", "failure")
        if argv[:2] == ["systemd-detect-virt", "--vm"]:
            result = self.vm
        elif argv[:2] == ["systemd-detect-virt", "--container"]:
            return subprocess.CompletedProcess(argv, 1 if self.container == "none" else 0, self.container, "")
        elif argv[0] == "dpkg":
            result = self.arch
        elif argv[0] == "dpkg-query":
            result = "\n".join(name + "\tinstalled\t" + version for name, version in self.installed.items())
        elif argv[0] == "apt-get":
            if "--simulate" in argv:
                result = self.simulation
            elif "install" in argv:
                for name in argv[argv.index("install") + 1:]:
                    self.installed[name] = "1.0"
                result = ""
            else:
                result = ""
        elif argv[0] == "curl":
            Path(argv[argv.index("--output") + 1]).write_bytes(self.key)
            result = ""
        elif argv[0] == "systemctl":
            result = ""
        elif argv[:2] == ["docker", "info"]:
            result = "29.0.0"
        elif argv[:3] == ["docker", "compose", "version"]:
            result = self.compose
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, result, "")

    @property
    def effects(self):
        return [call for call in self.calls if call[0] in {"apt-get", "curl", "systemctl"}]


class HostBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for dirname in ["etc/apt/sources.list.d", "proc/1"]:
            (self.root / dirname).mkdir(parents=True)
        (self.root / "etc/os-release").write_text('ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n')
        (self.root / "etc/machine-id").write_text("a" * 32)
        (self.root / "proc/1/comm").write_text("systemd\n")
        (self.root / "etc/apt/sources.list.d/debian.sources").write_text("Types: deb\nURIs: https://deb.debian.org/debian\n")
        self.run = FakeGuest()
        self.mod, self.ns = module()
        self.ns["KEY_SHA256"] = hashlib.sha256(self.run.key).hexdigest()
        self.platform_patch = mock.patch.object(self.mod.platform, "system", return_value="Linux")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        fake_fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_: None)
        self.lock_patch = mock.patch.dict(sys.modules, {"fcntl": fake_fcntl})
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    def plan(self):
        return self.mod.inspect(self.root, self.run, SCRIPT)

    def apply(self, document=None):
        document = document or self.plan()
        return self.mod.apply(document, document["review_hash"], self.root, self.run, SCRIPT)

    def test_default_plan_has_no_mutations_or_new_files(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        plan = self.plan()
        self.assertEqual("trixie", plan["codename"])
        self.assertEqual([], self.run.effects)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_apply_requires_all_acknowledgements_and_root(self):
        with self.assertRaisesRegex(self.mod.BootstrapError, "Apply requires"):
            self.mod.main(["--apply"], root=self.root, run=self.run, script_path=SCRIPT)
        self.assertEqual([], self.run.effects)

    def test_main_without_arguments_plans_only(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.main([], root=self.root, run=self.run, script_path=SCRIPT)
        self.assertEqual([], self.run.effects)

    def test_verify_mode_checks_runtime_without_changing_packages_or_services(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.mod.main(['--verify'], root=self.root, run=self.run, script_path=SCRIPT)
        self.assertEqual('ready', result['state'])
        self.assertFalse(any(call[0] in {'apt-get', 'curl'} or (call[0] == 'systemctl' and call[1] != 'is-active') for call in self.run.calls))
        self.assertFalse((self.root / self.mod.STATE).exists())

    def test_proxmox_host_is_refused(self):
        (self.root / "etc/pve").mkdir()
        with self.assertRaisesRegex(self.mod.BootstrapError, "Proxmox host"):
            self.plan()

    def test_bare_metal_and_containers_are_refused(self):
        self.run.vm = "none"
        with self.assertRaisesRegex(self.mod.BootstrapError, "KVM/QEMU"):
            self.plan()
        self.run.vm = "kvm"
        self.run.container = "docker"
        with self.assertRaisesRegex(self.mod.BootstrapError, "inside a container"):
            self.plan()

    def test_non_systemd_guest_is_refused(self):
        (self.root / "proc/1/comm").write_text("init")
        with self.assertRaisesRegex(self.mod.BootstrapError, "PID 1"):
            self.plan()

    def test_supported_ubuntu_versions(self):
        for version, codename in [("24.04", "noble"), ("26.04", "resolute")]:
            (self.root / "etc/os-release").write_text(f'ID=ubuntu\nVERSION_ID="{version}"\nVERSION_CODENAME={codename}\n')
            self.assertEqual(codename, self.plan()["codename"])

    def test_unrecognized_os_release_is_refused(self):
        (self.root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="99.04"\nVERSION_CODENAME=other\n')
        with self.assertRaisesRegex(self.mod.BootstrapError, "Supported guests"):
            self.plan()

    def test_conflicting_container_packages_are_not_removed(self):
        for package in ["docker.io", "podman", "runc", "containerd"]:
            self.run.installed = {package: "1.0"}
            with self.assertRaisesRegex(self.mod.BootstrapError, "never uninstalls"):
                self.plan()
        self.assertEqual([], self.run.effects)

    def test_existing_unowned_managed_filename_is_not_overwritten(self):
        target = self.root / self.mod.SOURCE
        target.write_text("important existing source\n")
        with self.assertRaisesRegex(self.mod.BootstrapError, "unowned or changed"):
            self.plan()
        self.assertEqual("important existing source\n", target.read_text())
        self.assertEqual([], self.run.effects)

    def test_existing_docker_repository_does_not_get_a_duplicate(self):
        (self.root / "etc/apt/sources.list.d/docker.list").write_text("deb https://download.docker.com/linux/debian trixie stable\n")
        with self.assertRaisesRegex(self.mod.BootstrapError, "already configured"):
            self.plan()

    def test_plan_is_bound_to_machine_id(self):
        plan = self.plan()
        (self.root / "etc/machine-id").write_text("b" * 32)
        with self.assertRaisesRegex(self.mod.BootstrapError, "changed"):
            self.apply(plan)
        self.assertEqual([], self.run.effects)

    def test_plan_is_bound_to_apt_sources(self):
        plan = self.plan()
        (self.root / "etc/apt/sources.list.d/debian.sources").write_text("changed source")
        with self.assertRaisesRegex(self.mod.BootstrapError, "changed"):
            self.apply(plan)
        self.assertEqual([], self.run.effects)

    def test_plan_hash_cannot_be_silently_tampered(self):
        plan = self.plan()
        plan["os"] = "malicious"
        with self.assertRaisesRegex(self.mod.BootstrapError, "integrity"):
            self.apply(plan)
        self.assertEqual([], self.run.effects)

    def test_valid_apply_pins_key_keeps_backups_and_is_repeatable(self):
        self.assertEqual("ready", self.apply()["state"])
        self.assertEqual(self.run.key, (self.root / self.mod.KEY).read_bytes())
        self.assertTrue(list((self.root / self.mod.STATE).glob("backup-*/manifest.json")))
        receipt = json.loads((self.root / self.mod.STATE / "receipt.json").read_text())
        self.assertEqual("ready", receipt["state"])
        apt_before = len([c for c in self.run.calls if c[0] == "apt-get"])
        self.apply()
        self.assertEqual(apt_before, len([c for c in self.run.calls if c[0] == "apt-get"]))
        self.assertFalse(any(c[0] in {"mkfs", "mount", "umount", "reboot"} or "purge" in c or "remove" in c for c in self.run.calls))

    def test_changed_docker_key_fails_without_trusting_it(self):
        plan = self.plan()
        self.run.key = b"wrong public key\n"
        with self.assertRaisesRegex(self.mod.BootstrapError, "pinned official artifact"):
            self.apply(plan)
        self.assertFalse((self.root / self.mod.KEY).exists())
        self.assertFalse((self.root / self.mod.SOURCE).exists())

    def test_apt_dependency_upgrade_is_rejected_before_install(self):
        self.run.simulation = "Inst libc6 [2.36] (2.40 Debian:unstable [amd64])"
        with self.assertRaisesRegex(self.mod.BootstrapError, "remove or upgrade"):
            self.apply()
        self.assertFalse(any(c[0] == "apt-get" and "--yes" in c for c in self.run.calls))

    def test_apt_removal_is_rejected_before_install(self):
        self.run.simulation = "Remv podman [4.0]"
        with self.assertRaisesRegex(self.mod.BootstrapError, "remove or upgrade"):
            self.apply()

    def test_old_compose_is_not_declared_ready(self):
        self.run.compose = "2.23.0"
        with self.assertRaisesRegex(self.mod.BootstrapError, "2.24.0"):
            self.apply()
        receipt = json.loads((self.root / self.mod.STATE / "receipt.json").read_text())
        self.assertEqual("failed", receipt["state"])

    def test_repository_must_remain_unchanged_on_retry(self):
        self.apply()
        (self.root / self.mod.SOURCE).write_text("externally changed source")
        before = len(self.run.effects)
        with self.assertRaisesRegex(self.mod.BootstrapError, "unowned or changed"):
            self.plan()
        self.assertEqual(before, len(self.run.effects))


if __name__ == "__main__":
    unittest.main()
