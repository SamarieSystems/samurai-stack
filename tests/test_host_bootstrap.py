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
        self.failure_code = 1
        self.failure_stdout = ''
        self.failure_stderr = 'failure'

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_command and self.fail_command(argv):
            return subprocess.CompletedProcess(argv, self.failure_code, self.failure_stdout, self.failure_stderr)
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

    def test_legacy_active_cdrom_sources_including_options_and_deb_src_are_detected(self):
        for source in [
            'deb cdrom:[Debian GNU/Linux install DVD]/ trixie main non-free-firmware\n',
            'deb-src cdrom:[Debian install DVD]/ trixie main\n',
            '  deb [ arch=amd64 trusted=yes ] cdrom:[Debian DVD]/ trixie main # installer\n',
            '\tdeb-src\t[arch=amd64]\tcdrom:[DVD]/ trixie main\n',
        ]:
            with self.subTest(source=source):
                self.assertTrue(self.mod.active_install_media_source(source))
        for source in [
            '# deb cdrom:[old DVD]/ trixie main\n',
            '  # deb-src [arch=amd64] cdrom:[old DVD]/ trixie main\n',
            'deb https://deb.debian.org/debian trixie main # cdrom: no longer used\n',
            'deb [signed-by=/etc/apt/keyrings/cdrom-key.gpg] https://deb.debian.org/debian trixie main\n',
        ]:
            with self.subTest(source=source):
                self.assertFalse(self.mod.active_install_media_source(source))

    def test_deb822_media_detection_honors_folded_fields_and_disabled_stanzas(self):
        for source in [
            'Types: deb\nURIs: cdrom:[Debian DVD]/\nSuites: trixie\nComponents: main\n',
            'Types: deb\n deb-src\nURIs: https://deb.debian.org/debian\n cdrom:[Debian DVD]/\nSuites: trixie\n',
            'Types: deb-src\nURIs:\n cdrom:[DVD]/\nEnabled: yes\n',
            'Types: deb\nURIs: cdrom:[inactive]/\nEnabled: no\n\nTypes: deb\nURIs: cdrom:[active]/\n',
        ]:
            with self.subTest(source=source):
                self.assertTrue(self.mod.active_install_media_source(source, deb822=True))
        for source in [
            'Types: deb\nURIs: cdrom:[disabled]/\nEnabled: no\n',
            'Types: deb-src\nURIs:\n cdrom:[disabled]/\nEnabled:\n No\n',
            '# Types: deb\n# URIs: cdrom:[commented]/\n',
            'Types: deb\n# URIs: cdrom:[commented]/\nURIs: https://deb.debian.org/debian\n',
            'Types: deb\nURIs: https://deb.debian.org/debian\n # cdrom:[commented continuation]\n',
            'Types: deb\nURIs: https://deb.debian.org/debian\nSigned-By: /etc/apt/keyrings/cdrom-key.gpg\n',
        ]:
            with self.subTest(source=source):
                self.assertFalse(self.mod.active_install_media_source(source, deb822=True))
        for disabled in ('false', 'off', '0', 'without', 'disable', 'FALSE', 'No'):
            with self.subTest(disabled=disabled):
                self.assertFalse(self.mod.active_install_media_source(
                    'Types: deb\nURIs: cdrom:[disabled DVD]/\nEnabled: ' + disabled + '\n', deb822=True))

    def test_active_install_media_refuses_inspection_without_mutating_sources_or_running_apt(self):
        source = self.root / 'etc/apt/sources.list'
        source.write_text('deb cdrom:[Debian install DVD]/ trixie main\n'
                          'deb https://deb.debian.org/debian trixie main\n')
        before = {str(path): path.read_bytes() for path in (self.root / 'etc/apt').rglob('*') if path.is_file()}
        with self.assertRaisesRegex(self.mod.BootstrapError, '^Active APT installation-media source detected;'):
            self.plan()
        self.assertEqual(before, {str(path): path.read_bytes() for path in (self.root / 'etc/apt').rglob('*') if path.is_file()})
        self.assertEqual([], self.run.effects)
        self.assertFalse((self.root / self.mod.STATE).exists())

    def test_commented_or_disabled_media_and_online_sources_are_retained_unchanged(self):
        (self.root / 'etc/apt/sources.list').write_text('# deb cdrom:[old DVD]/ trixie main\n'
            'deb https://deb.debian.org/debian trixie main\n')
        (self.root / 'etc/apt/sources.list.d/old-media.sources').write_text('Types: deb\nURIs: cdrom:[old DVD]/\nEnabled: no\n')
        (self.root / 'etc/apt/sources.list.d/ignored backup.list').write_text('deb cdrom:[ignored backup]/ trixie main\n')
        before = {str(path): path.read_bytes() for path in (self.root / 'etc/apt').rglob('*') if path.is_file()}
        self.plan()
        self.assertEqual(before, {str(path): path.read_bytes() for path in (self.root / 'etc/apt').rglob('*') if path.is_file()})
        self.assertEqual([], self.run.effects)
        self.assertFalse((self.root / self.mod.STATE).exists())

    def test_cdrom_source_added_after_review_cannot_reach_apply_mutations(self):
        reviewed = self.plan()
        (self.root / 'etc/apt/sources.list.d/new-media.sources').write_text('Types: deb-src\nURIs:\n cdrom:[DVD]/\n')
        with self.assertRaisesRegex(self.mod.BootstrapError, '^Active APT installation-media source detected;'):
            self.apply(reviewed)
        self.assertEqual([], self.run.effects)
        self.assertFalse((self.root / self.mod.STATE).exists())

    def test_fully_installed_healthy_runtime_is_not_blocked_by_unused_media_source(self):
        self.run.installed = {name: 'fixture-version' for name in self.mod.BASE + self.mod.DOCKER}
        source = self.root / 'etc/apt/sources.list'
        source.write_text('deb cdrom:[old install DVD]/ trixie main\n')
        before = source.read_bytes()
        self.plan()
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.mod.main(['--verify'], root=self.root, run=self.run, script_path=SCRIPT)
        self.assertEqual('ready', result['state'])
        self.assertEqual(before, source.read_bytes())
        self.assertFalse(any(call[0] in {'apt-get', 'curl'} or (call[0] == 'systemctl' and call[1] != 'is-active') for call in self.run.calls))
        self.assertFalse((self.root / self.mod.STATE).exists())

    def test_hidden_source_fragments_do_not_trigger_active_media_refusal(self):
        hidden = self.root / 'etc/apt/sources.list.d/.old-media.sources'
        hidden.write_text('Types: deb\nURIs: cdrom:[inactive hidden backup]/\n')
        before = hidden.read_bytes()
        reviewed = self.plan()
        self.assertEqual(before, hidden.read_bytes())
        self.assertTrue(any(Path(item['path']).name == '.old-media.sources' for item in reviewed['apt_sources']))
        self.assertEqual([], self.run.effects)
        self.assertFalse((self.root / self.mod.STATE).exists())

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

    @unittest.skipUnless(os.name == 'posix', 'Unix umask and signing-key permissions')
    def test_new_signing_key_directory_is_traversable_under_private_umask(self):
        self.assertFalse((self.root / self.mod.KEY).parent.exists())
        previous = os.umask(0o077)
        try:
            result = self.apply()
        finally:
            os.umask(previous)
        self.assertEqual('ready', result['state'])
        self.assertEqual((self.root / self.mod.KEY).parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / self.mod.KEY).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.run.key, (self.root / self.mod.KEY).read_bytes())

    @unittest.skipUnless(os.name == 'posix', 'Unix existing directory permission contract')
    def test_existing_signing_key_directory_permissions_are_never_rewritten(self):
        directory = self.root / self.mod.KEY.parent
        directory.mkdir(mode=0o711); directory.chmod(0o711)
        previous = os.umask(0o077)
        try:
            self.apply()
        finally:
            os.umask(previous)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o711)
        self.assertEqual((self.root / self.mod.KEY).stat().st_mode & 0o777, 0o644)

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

    def command_logs(self):
        return [(p, json.loads(p.read_text())) for p in sorted((self.root / self.mod.STATE).glob('backup-*/commands/*.json'))]

    def test_apt_exit100_keeps_both_streams_private_and_reports_static_operation(self):
        self.run.fail_command = lambda argv: argv[:2] == ['apt-get', 'update']
        self.run.failure_code = 100
        self.run.failure_stdout = 'synthetic-private-repository-output\n'
        self.run.failure_stderr = 'synthetic-private-package-error\n'
        with self.assertRaises(self.mod.BootstrapError) as captured:
            self.apply()
        message = str(captured.exception)
        self.assertTrue(message.startswith('apt-get failed with exit 100;'))
        self.assertIn('Last operation: apt-get update.', message)
        self.assertIn('Private command diagnostic:', message)
        self.assertTrue(message.endswith('.log'))
        self.assertNotIn('synthetic-private', message)
        logs = self.command_logs()
        self.assertEqual(1, len(logs))
        path, value = logs[0]
        self.assertEqual(('apt-get', 'update', 100), (value['program'], value['operation'], value['exit']))
        self.assertEqual(self.run.failure_stdout, value['stdout'])
        self.assertEqual(self.run.failure_stderr, value['stderr'])
        plain = path.with_suffix('.log')
        readable = plain.read_text()
        self.assertIn('Operation: apt-get update\nExit: 100\n', readable)
        self.assertIn(self.run.failure_stdout, readable)
        self.assertIn(self.run.failure_stderr, readable)
        self.assertNotIn('\\n', readable)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plain.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        receipt = json.loads((self.root / self.mod.STATE / 'receipt.json').read_text())
        self.assertEqual('failed', receipt['state'])
        self.assertEqual(str(path.parent), receipt['commands_directory'])

    def test_zero_exit_dependency_simulation_is_retained_when_policy_blocks_install(self):
        self.run.simulation = 'Inst libc6 [2.36] (2.40 synthetic-private-package-source)\n'
        with self.assertRaises(self.mod.BootstrapError) as captured:
            self.apply()
        self.assertIn('APT would remove or upgrade an existing package;', str(captured.exception))
        self.assertIn('Last operation: apt-get simulate-install.', str(captured.exception))
        simulations = [value for _, value in self.command_logs() if value['operation'] == 'simulate-install']
        self.assertEqual(1, len(simulations))
        self.assertEqual(0, simulations[0]['exit'])
        self.assertEqual(self.run.simulation, simulations[0]['stdout'])
        simulation_logs = list((self.root / self.mod.STATE).glob('backup-*/commands/*simulate-install.log'))
        self.assertEqual(1, len(simulation_logs))
        self.assertIn(self.run.simulation, simulation_logs[0].read_text())
        self.assertFalse(any(argv[0] == 'apt-get' and '--yes' in argv for argv in self.run.calls))

    def test_diagnostic_failure_does_not_replace_original_apt_error(self):
        self.run.fail_command = lambda argv: argv[:2] == ['apt-get', 'update']
        self.run.failure_code = 100
        original = self.ns['write_private']

        def broken_log(path, value):
            if path.parent.name == 'commands':
                raise OSError('synthetic log write failure')
            return original(path, value)

        self.ns['write_private'] = broken_log
        with self.assertRaises(self.mod.BootstrapError) as captured:
            self.apply()
        self.assertTrue(str(captured.exception).startswith('apt-get failed with exit 100;'))
        self.assertNotIn('synthetic log write failure', str(captured.exception))
        receipt = json.loads((self.root / self.mod.STATE / 'receipt.json').read_text())
        self.assertFalse(receipt['command_diagnostics_complete'])

    def test_diagnostic_outputs_are_bounded_and_full_command_arguments_are_not_recorded(self):
        folder = self.root / 'private-commands'
        output = 'x' * (self.mod.CommandCapture.LIMIT + 512) + 'synthetic-private-output-tail'
        def fake(argv):
            return subprocess.CompletedProcess(argv, 0, output, output)
        capture = self.mod.CommandCapture(fake, folder)
        # Construct disposable userinfo; no credential URL literal is shipped.
        address = 'https://' + 'synthetic-private-user' + ':' + 'synthetic-private-value' + '@example.invalid/key'
        capture(['curl', address])
        value = json.loads(capture.last_json_path.read_text())
        self.assertEqual(('curl', 'download-signing-key', 0), (value['program'], value['operation'], value['exit']))
        for field in ('stdout', 'stderr'):
            self.assertEqual(self.mod.CommandCapture.LIMIT, len(value[field].encode()))
            self.assertTrue(value[field + '_truncated'])
            self.assertEqual(len(output), value[field + '_bytes'])
            self.assertTrue(value[field].endswith('synthetic-private-output-tail'))
        self.assertNotIn('synthetic-private-user', capture.last_path.read_text())
        self.assertEqual('.log', capture.last_path.suffix)
        self.assertLessEqual(capture.last_path.stat().st_size, 2 * self.mod.CommandCapture.LIMIT + 1024)
        self.assertNotIn('argv', value)

    def test_plain_log_failure_keeps_json_and_original_command_error(self):
        self.run.fail_command = lambda argv: argv[:2] == ['apt-get', 'update']
        self.run.failure_code = 100
        self.ns['write_private_text'] = mock.Mock(side_effect=OSError('synthetic plain log failure'))
        with self.assertRaises(self.mod.BootstrapError) as captured:
            self.apply()
        self.assertTrue(str(captured.exception).startswith('apt-get failed with exit 100;'))
        self.assertNotIn('synthetic plain log failure', str(captured.exception))
        self.assertEqual(1, len(self.command_logs()))
        self.assertEqual(100, self.command_logs()[0][1]['exit'])
        receipt = json.loads((self.root / self.mod.STATE / 'receipt.json').read_text())
        self.assertFalse(receipt['command_diagnostics_complete'])

    def test_readonly_plan_and_verify_create_no_capture_files(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.plan()
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.main(['--verify'], root=self.root, run=self.run, script_path=SCRIPT)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))


if __name__ == "__main__":
    unittest.main()
