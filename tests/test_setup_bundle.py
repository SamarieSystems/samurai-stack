"""Portable setup bundle contains public code and validates before extraction."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("setup_bundle_builder", ROOT / "scripts/build-setup-bundle.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class SetupBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = builder.build(self.root / "samurai-setup.pyz")
        with zipfile.ZipFile(self.bundle) as archive:
            self.launcher = archive.read("__main__.py")
            self.entries = [(copy.copy(info), archive.read(info)) for info in archive.infolist()]
        self.loader = {"__name__": "setup_bundle_loader"}
        exec(compile(self.launcher, "bundle/__main__.py", "exec"), self.loader)

    def rewrite(self, transform):
        path = self.root / "altered.pyz"
        entries = transform([(copy.copy(info), value) for info, value in self.entries])
        with warnings.catch_warnings(), zipfile.ZipFile(path, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for info, value in entries:
                archive.writestr(info, value)
        return path

    def public_root(self):
        source = self.root / "source"
        source.mkdir()
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            for name in manifest["files"]:
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
        return source

    def assert_refused(self, path):
        with self.assertRaises(self.loader["BundleError"]):
            self.loader["run_bundle"](path, ["--version"], invoke=lambda repo, args: self.fail("Unverified payload executed"))

    def test_manifest_matches_public_sources_and_exact_production_allowlist(self):
        with zipfile.ZipFile(self.bundle) as archive:
            encoded = archive.read("MANIFEST.json")
            manifest = json.loads(encoded)
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), self.loader["MANIFEST_SHA256"])
            expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / "samurai_stack").glob("*.py")}
            expected.update({"LICENSE", "stack", "setup", *builder.SCRIPTS})
            self.assertEqual(set(manifest["files"]), expected)
            self.assertEqual(set(archive.namelist()), expected | {"__main__.py", "MANIFEST.json"})
            for name, record in manifest["files"].items():
                value = archive.read(name)
                self.assertEqual(value, (ROOT / name).read_bytes())
                self.assertEqual(record["size"], len(value))
                self.assertEqual(record["sha256"], hashlib.sha256(value).hexdigest())
                self.assertNotIn("tests/", name)
                self.assertNotIn("secrets/", name)
                self.assertFalse(name.endswith((".json", ".env", ".yaml", ".db")))
        self.assertEqual(set(self.loader["payload"](self.bundle)), expected)

    def test_version_and_help_work_without_linux_root_tty_or_site_config(self):
        for arguments in (["--version"], ["--help"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(self.bundle), *arguments], cwd=self.root,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.strip())
        self.assertFalse((self.root / "site.json").exists())
        self.assertFalse((self.root / ".setup-state").exists())

    def test_normal_setup_still_refuses_noninteractive_or_wrong_host(self):
        original = self.root / "site.json"
        original.write_text("original-private-site-file")
        result = subprocess.run([sys.executable, str(self.bundle), "3"], cwd=self.root,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue("interactive terminal" in result.stderr or "intended Linux VM" in result.stderr)
        self.assertEqual(original.read_text(), "original-private-site-file")
        self.assertFalse((self.root / ".setup-state").exists())

    def test_original_cwd_argv_config_and_extracted_subprocess_lifetime_preserved(self):
        original_dir = self.root / "old-checkout"
        original_dir.mkdir()
        site = original_dir / "private-site.json"
        site.write_text("preserve-original-configuration")
        old_script = original_dir / "stack"
        old_script.write_text("unrelated old CLI must never execute")
        arguments = ["3", "--config", "private-site.json"]
        captured = []
        def invoke(repo, received):
            captured.append(repo)
            self.assertTrue(repo.is_dir())
            self.assertEqual(Path.cwd(), original_dir)
            self.assertEqual(received, arguments)
            self.assertEqual(Path(received[2]).absolute(), site)
            self.assertNotEqual(repo, original_dir)
            self.assertEqual((repo / "stack").read_bytes(), (ROOT / "stack").read_bytes())
            self.assertFalse((repo / "site.json").exists())
            if os.name == "posix":
                self.assertEqual(repo.stat().st_mode & 0o777, 0o700)
                self.assertEqual((repo / "samurai_stack/setup.py").stat().st_mode & 0o777, 0o600)
            child = subprocess.run([sys.executable, "-I", "-c",
                "import sys; sys.path.insert(0, sys.argv.pop(1)); from samurai_stack.cli import main; raise SystemExit(main())",
                str(repo), "--help"], cwd=repo,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("usage", child.stdout.lower())
            return 23
        cwd = Path.cwd()
        try:
            os.chdir(original_dir)
            self.assertEqual(self.loader["run_bundle"](self.bundle, arguments, invoke=invoke), 23)
            self.assertEqual(Path.cwd(), original_dir)
        finally:
            os.chdir(cwd)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())
        self.assertEqual(site.read_text(), "preserve-original-configuration")
        self.assertEqual(old_script.read_text(), "unrelated old CLI must never execute")

    def test_only_owned_extraction_is_cleaned_after_installer_exception(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "personal-data.txt"
        sentinel.write_text("preserve")
        captured = []
        def invoke(repo, args):
            captured.append(repo)
            raise RuntimeError("synthetic installer interruption")
        with self.assertRaisesRegex(RuntimeError, "synthetic installer interruption"):
            self.loader["run_bundle"](self.bundle, [], invoke=invoke)
        self.assertFalse(captured[0].exists())
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_builder_refuses_overwrite_without_changing_existing_asset(self):
        before = self.bundle.read_bytes()
        with self.assertRaises(FileExistsError):
            builder.build(self.bundle)
        self.assertEqual(self.bundle.read_bytes(), before)

    def test_builder_rejects_oversized_public_source_before_publishing(self):
        source = self.public_root()
        (source / "samurai_stack/setup.py").write_bytes(b"x" * (builder.MAX_MEMBER_BYTES + 1))
        target = self.root / "oversized.pyz"
        with self.assertRaisesRegex(ValueError, "bounded regular public file"):
            builder.build(target, root=source)
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX source symlink safety")
    def test_builder_rejects_source_symlink_and_existing_broken_destination_link(self):
        source = self.public_root()
        path = source / "samurai_stack/setup.py"
        original = path.read_bytes()
        path.unlink()
        outside = self.root / "outside-public-source.py"
        outside.write_bytes(original)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            builder.build(self.root / "linked.pyz", root=source)
        destination = self.root / "broken.pyz"
        destination.symlink_to(self.root / "not-created")
        with self.assertRaises(FileExistsError):
            builder.build(destination)
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / "not-created").exists())

    def test_duplicate_traversal_absolute_backslash_and_unreviewed_paths_refused(self):
        for name in ("../outside", "/outside", "samurai_stack/../outside.py", "samurai_stack\\evil.py", "scripts/commission-apps.py", "site.json"):
            with self.subTest(path=name):
                def mutate(entries):
                    info = copy.copy(entries[-1][0])
                    info.filename = name
                    return entries + [(info, b"untrusted")]
                self.assert_refused(self.rewrite(mutate))
        self.assert_refused(self.rewrite(lambda entries: entries + [entries[-1]]))

    def test_symlink_and_nonregular_zip_members_refused(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR):
            with self.subTest(kind=kind):
                def mutate(entries):
                    info, value = entries[-1]
                    info.external_attr = (kind | 0o600) << 16
                    entries[-1] = (info, value)
                    return entries
                self.assert_refused(self.rewrite(mutate))

    def test_oversized_decompressed_payload_refused_before_execution(self):
        def mutate(entries):
            info, _ = entries[-1]
            entries[-1] = (info, b"x" * (builder.MAX_MEMBER_BYTES + 1))
            return entries
        self.assert_refused(self.rewrite(mutate))

    def test_tampered_payload_manifest_or_missing_member_refused(self):
        for member in ("samurai_stack/setup.py", "MANIFEST.json"):
            with self.subTest(member=member):
                def mutate(entries):
                    return [(info, (b"X" + value[1:]) if info.filename == member else value) for info, value in entries]
                self.assert_refused(self.rewrite(mutate))
        self.assert_refused(self.rewrite(lambda entries: entries[:-1]))

    def test_private_extra_files_are_not_packaged_from_source_tree(self):
        source = self.public_root()
        for name in ("site.json", ".env", "secrets/token", "tests/private_fixture.py", "scripts/unreviewed.py", "samurai_stack/__pycache__/old.pyc"):
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("synthetic-private-data")
        other = builder.build(self.root / "public-only.pyz", root=source)
        with zipfile.ZipFile(self.bundle) as original, zipfile.ZipFile(other) as archive:
            self.assertEqual(archive.namelist(), original.namelist())
            for name in archive.namelist():
                self.assertEqual(archive.read(name), original.read(name))


if __name__ == "__main__":
    unittest.main()
