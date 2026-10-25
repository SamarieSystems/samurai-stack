"""Bash launchers share one guarded backend and preserve the caller's context."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shell_bundle_builder", ROOT / "scripts/build-setup-bundle.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
BASH = shutil.which("bash") if os.name == "posix" else None
PAYLOAD_START = b"<<'__SAMURAI_SETUP_PAYLOAD__'\n"
PAYLOAD_END = b"\n__SAMURAI_SETUP_PAYLOAD__\n"


class LauncherBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_shell_is_offline_executable_and_contains_exact_shared_helper_and_pyz(self):
        shell = builder.build_shell(self.root / "samurai-setup.sh")
        raw = shell.read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        common = (ROOT / "scripts/launcher-common.sh").read_bytes()
        self.assertEqual(raw.count(common), 1)
        payload = base64.b64decode(raw.split(PAYLOAD_START, 1)[1].split(PAYLOAD_END, 1)[0])
        pyz = builder.build(self.root / "samurai-setup.pyz")
        self.assertEqual(payload, pyz.read_bytes())
        self.assertIn(hashlib.sha256(payload).hexdigest().encode("ascii"), raw)
        self.assertIn(str(len(payload)).encode("ascii"), raw)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("scripts/launcher-common.sh"), common)
        if os.name == "posix":
            self.assertEqual(shell.stat().st_mode & 0o777, 0o700)

    def test_shell_build_is_deterministic_and_refuses_overwrite(self):
        first = builder.build_shell(self.root / "first.sh")
        second = builder.build_shell(self.root / "second.sh")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        before = first.read_bytes()
        with self.assertRaises(FileExistsError):
            builder.build_shell(first)
        self.assertEqual(first.read_bytes(), before)

    def test_builder_cli_accepts_shell_format_without_changing_compatibility_default(self):
        shell = self.root / "installer.sh"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/build-setup-bundle.py"),
            "--format", "shell", str(shell)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(shell.read_bytes().startswith(b"#!/usr/bin/env bash\n"))


@unittest.skipUnless(BASH, "Bash execution is verified on POSIX release runners")
class BashLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="samurai-launcher-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "caller with spaces"
        self.cwd.mkdir()
        self.scratch = self.root / "owned temporary payloads"
        self.scratch.mkdir()
        self.result = self.root / "result.json"
        self.env = dict(os.environ, TMPDIR=str(self.scratch), LAUNCHER_RESULT=str(self.result))
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env.get("PATH", "")

    def run_shell(self, script, *arguments, env=None):
        return subprocess.run([BASH, str(script), *arguments], cwd=self.cwd,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, env=self.env if env is None else env)

    def fixture_source(self):
        """Stub only the backend and preflight to test transport without installing."""
        source = self.root / "trusted source with spaces"
        source.mkdir()
        pyz = builder.build(self.root / "fixture-sources.pyz")
        with zipfile.ZipFile(pyz) as archive:
            for name in json.loads(archive.read("MANIFEST.json"))["files"]:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
        common = source / "scripts/launcher-common.sh"
        common.write_text(common.read_text() + "\n# Test fixture: never install or escalate.\nsamurai_guest_preflight() { :; }\n")
        code = '''import json, os, sys
from pathlib import Path
def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    result = {"cwd": str(Path.cwd()), "argv": arguments}
    if "--config" in arguments:
        result["config"] = Path(arguments[arguments.index("--config") + 1]).read_text()
    Path(os.environ["LAUNCHER_RESULT"]).write_text(json.dumps(result))
    return int(os.environ.get("LAUNCHER_STATUS", "23"))
'''
        (source / "samurai_stack/setup.py").write_text(code)
        (source / "samurai_stack/cli.py").write_text(code)
        return source

    def hostile_caller_package(self):
        package = self.cwd / "samurai_stack"
        package.mkdir()
        (package / "__init__.py").write_text("raise RuntimeError('untrusted caller package executed')\n")
        (package / "setup.py").write_text("raise RuntimeError('untrusted caller setup executed')\n")
        (package / "cli.py").write_text("raise RuntimeError('untrusted caller CLI executed')\n")
        self.env["PYTHONPATH"] = str(self.cwd)
        # -I must also ignore startup hooks in this environment path.
        (self.cwd / "sitecustomize.py").write_text("raise RuntimeError('untrusted startup hook executed')\n")

    def test_help_and_version_need_no_python_and_match_source_and_portable(self):
        shell = builder.build_shell(self.root / "samurai-setup.sh")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        marker = self.root / "python-unexpectedly-called"
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nprintf called > \"$LAUNCHER_RESULT\"\nexit 99\n")
        fake_python.chmod(0o700)
        env = dict(self.env, LAUNCHER_RESULT=str(marker), PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        for arguments in (("--help",), ("--version",)):
            with self.subTest(arguments=arguments):
                source = self.run_shell(ROOT / "setup", *arguments, env=env)
                portable = self.run_shell(shell, *arguments, env=env)
                self.assertEqual(source.returncode, 0, source.stderr)
                self.assertEqual(portable.returncode, 0, portable.stderr)
                self.assertEqual(source.stdout, portable.stdout.replace("./samurai-setup.sh", "./setup"))
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_source_and_portable_preserve_cwd_argv_config_and_exit_status(self):
        source = self.fixture_source()
        shell = builder.build_shell(self.root / "samurai-setup.sh", root=source)
        self.hostile_caller_package()
        site = self.cwd / "private site.json"
        site.write_text("original caller configuration")
        cases = ((), ("--fresh",), ("3",), ("3", "--config", "private site.json"),
                 ("--recover",), ("--quality-profiles",))
        for script in (source / "setup", shell):
            for arguments in cases:
                with self.subTest(script=script.name, arguments=arguments):
                    result = self.run_shell(script, *arguments)
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertNotIn('untrusted', result.stderr)
                    received = json.loads(self.result.read_text())
                    self.assertEqual(received["cwd"], str(self.cwd))
                    self.assertEqual(received["argv"], list(arguments))
                    if "--config" in arguments:
                        self.assertEqual(received["config"], site.read_text())
                    self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(site.read_text(), "original caller configuration")
        for status in (0, 2, 130):
            env = dict(self.env, LAUNCHER_STATUS=str(status))
            with self.subTest(status=status):
                result = self.run_shell(shell, "3", env=env)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_advanced_stack_is_bash_and_ignores_hostile_cwd_and_pythonpath(self):
        source = self.fixture_source()
        self.hostile_caller_package()
        # The fixture reads --config to prove that the original cwd is retained.
        config = self.cwd / "not-needed"
        config.write_text("caller-owned configuration")
        result = self.run_shell(source / "stack", "--config", "not-needed", "plan", "--stage", "1")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn('untrusted', result.stderr)
        self.assertEqual(json.loads(self.result.read_text()), {
            "cwd": str(self.cwd), "argv": ["--config", "not-needed", "plan", "--stage", "1"],
            "config": "caller-owned configuration"})

    def test_release_bash_help_ignores_hostile_startup_hooks(self):
        self.hostile_caller_package()
        result = self.run_shell(ROOT / 'scripts/release.sh', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--tag', result.stdout)
        self.assertIn('--output', result.stdout)
        self.assertNotIn('untrusted', result.stderr)

    def test_portable_rejects_corrupt_payload_before_execution_and_cleans_owned_temp(self):
        source = self.fixture_source()
        shell = builder.build_shell(self.root / "samurai-setup.sh", root=source)
        raw = shell.read_bytes()
        offset = raw.index(PAYLOAD_START) + len(PAYLOAD_START)
        shell.write_bytes(raw[:offset] + (b"A" if raw[offset:offset + 1] != b"A" else b"B") + raw[offset + 1:])
        result = self.run_shell(shell, "--fresh")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("integrity check failed", result.stderr)
        self.assertFalse(self.result.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_real_guard_rejects_noninteractive_mutation_but_allows_read_only_recovery_to_host_check(self):
        # Force an unsupported OS at the host guard, without depending on the
        # CI host having root or exposing any real service inventory.
        fake_bin = self.root / "guard-bin"
        fake_bin.mkdir()
        for name, text in (("sudo", "exit 73"), ("uname", "printf TestUnsupportedOS")):
            path = fake_bin / name
            path.write_text("#!/bin/sh\n" + text + "\n")
            path.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])
        for build in (False, True):
            script = builder.build_shell(self.root / "guarded.sh") if build else ROOT / "setup"
            mutation = self.run_shell(script, "3", env=env)
            self.assertEqual(mutation.returncode, 1, mutation.stderr)
            self.assertIn("interactive terminal", mutation.stderr)
            recovery = self.run_shell(script, "--recover", env=env)
            self.assertNotEqual(recovery.returncode, 0)
            self.assertNotIn("interactive terminal", recovery.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'Elevation is tested as an ordinary Linux user')
    def test_nonexecutable_source_and_portable_elevate_through_bash_with_exact_arguments(self):
        source = self.fixture_source()
        # Restore the real preflight; sudo below only records, never escalates.
        (source / 'scripts/launcher-common.sh').write_bytes((ROOT / 'scripts/launcher-common.sh').read_bytes())
        shell = builder.build_shell(self.root / 'nonexecutable installer.sh', root=source)
        fake_bin = self.root / 'sudo-bin'
        fake_bin.mkdir()
        sudo = fake_bin / 'sudo'
        sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$LAUNCHER_RESULT"\nexit 73\n')
        sudo.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env['PATH'])
        arguments = ('--recover', '--config', 'private site.json')
        for script in (source / 'setup', shell):
            script.chmod(0o600)
            result = self.run_shell(script, *arguments, env=env)
            self.assertEqual(result.returncode, 73, result.stderr)
            self.assertEqual(self.result.read_text().splitlines(), ['--', 'bash', str(script), *arguments])
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
