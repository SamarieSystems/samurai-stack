"""Release publication inputs come from one audited local Git commit."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_release_builder', ROOT / 'scripts/build-release.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
GIT = shutil.which('git')


@unittest.skipUnless(GIT, 'Local Git is required for committed-source release tests')
class ReleaseBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='samurai-release-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'source checkout'
        self.repo.mkdir()
        names = {'LICENSE', 'setup', 'stack', 'scripts/bootstrap-host.sh',
            'scripts/launcher-common.sh', 'scripts/port_sync.py', 'scripts/release.sh',
            'scripts/build-release.py', 'scripts/check-release.py',
            'scripts/build-setup-bundle.py', 'scripts/build-hardlink-repair.py'}
        names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / 'samurai_stack').glob('*.py'))
        for name in names:
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes())
        (self.repo / 'samurai_stack/__init__.py').write_text('__version__ = "0.2.0rc1"\n')
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Release Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'commit.gpgsign', 'false')
        for name in builder.REQUIRED_EXECUTABLES:
            (self.repo / name).chmod(0o755)
        self.git('add', '--all')
        self.git('update-index', '--chmod=+x', *sorted(builder.REQUIRED_EXECUTABLES))
        self.commit()

    def git(self, *arguments):
        result = subprocess.run([GIT, '-C', str(self.repo), *arguments], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
        if result.returncode:
            self.fail('Fixture Git failed: ' + result.stderr.decode(errors='replace'))
        return result.stdout

    def commit(self):
        self.git('commit', '--quiet', '-m', 'Synthetic release fixture')

    def build(self, name='release', tag='v0.2.0-rc.1'):
        output = self.root / name
        result = builder.build_release(self.repo, output, tag)
        return output, result

    def test_artifacts_reproduce_git_bytes_modes_manifest_and_published_hashes(self):
        output, result = self.build()
        self.assertEqual(result['version'], '0.2.0rc1')
        self.assertEqual(result['commit'], self.git('rev-parse', 'HEAD').decode().strip())
        manifest = json.loads((output / 'SOURCE-MANIFEST.json').read_bytes())
        self.assertEqual(manifest['tag'], 'v0.2.0-rc.1')
        self.assertEqual(manifest['commit'], result['commit'])
        names = self.git('ls-files', '-z').decode().rstrip('\0').split('\0')
        self.assertEqual(set(manifest['files']), set(names))
        prefix = 'samurai-stack-v0.2.0-rc.1/'
        with zipfile.ZipFile(output / 'samurai-stack-v0.2.0-rc.1.zip') as archive, \
                tarfile.open(output / 'samurai-stack-v0.2.0-rc.1.tar.gz') as tar:
            self.assertEqual(set(archive.namelist()), {prefix + name for name in names + ['SOURCE-MANIFEST.json']})
            for name in names:
                expected = self.git('show', 'HEAD:' + name)
                self.assertEqual(archive.read(prefix + name), expected)
                self.assertEqual(tar.extractfile(prefix + name).read(), expected)
                self.assertEqual(manifest['files'][name]['sha256'], hashlib.sha256(expected).hexdigest())
            for name in builder.REQUIRED_EXECUTABLES:
                self.assertEqual(stat.S_IMODE(archive.getinfo(prefix + name).external_attr >> 16), 0o755)
                self.assertEqual(tar.getmember(prefix + name).mode, 0o755)
            self.assertEqual(archive.read(prefix + 'SOURCE-MANIFEST.json'), (output / 'SOURCE-MANIFEST.json').read_bytes())
        with zipfile.ZipFile(output / 'samurai-setup.pyz') as archive:
            payload_manifest = json.loads(archive.read('MANIFEST.json'))
            for name in payload_manifest['files']:
                self.assertEqual(archive.read(name), self.git('show', 'HEAD:' + name))
        with zipfile.ZipFile(output / 'samurai-hardlinks.pyz') as archive:
            for name in archive.namelist():
                if name != '__main__.py':
                    self.assertEqual(archive.read(name), self.git('show', 'HEAD:' + name))
        checksums = {}
        for line in (output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            checksums[name] = digest
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(set(checksums), {path.name for path in output.iterdir()} - {'SHA256SUMS'})
        self.assertEqual(len(checksums), 6)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE((output / 'samurai-setup.sh').stat().st_mode), 0o700)

    def test_packaged_entry_points_run_without_checkout_imports(self):
        output, _ = self.build()
        empty = self.root / 'empty invocation'
        empty.mkdir()
        for name, argument, expected in (
                ('samurai-setup.pyz', '--version', '0.2.0rc1'),
                ('samurai-hardlinks.pyz', '--help', '--apply')):
            with self.subTest(asset=name):
                result = subprocess.run([sys.executable, '-I', str(output / name), argument],
                    cwd=empty, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
        self.assertEqual(list(empty.iterdir()), [])

    def test_same_commit_build_is_deterministic_across_output_directories(self):
        first, _ = self.build('first')
        second, _ = self.build('second')
        self.assertEqual(sorted(path.name for path in first.iterdir()), sorted(path.name for path in second.iterdir()))
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)

    def test_dirty_tracked_staged_and_unstaged_source_is_refused_before_output(self):
        path = self.repo / 'LICENSE'
        path.write_bytes(path.read_bytes() + b'\nfixture modification\n')
        for staged in (False, True):
            if staged:
                self.git('add', 'LICENSE')
            with self.subTest(staged=staged), self.assertRaisesRegex(builder.ReleaseError, 'dirty'):
                self.build()
            self.assertFalse((self.root / 'release').exists())

    def test_untracked_local_configuration_is_not_in_release_or_changed(self):
        private = self.repo / 'site-private.json'
        private.write_text('caller-owned private configuration')
        output, _ = self.build()
        with zipfile.ZipFile(output / 'samurai-stack-v0.2.0-rc.1.zip') as archive:
            self.assertFalse(any(name.endswith('site-private.json') for name in archive.namelist()))
        self.assertEqual(private.read_text(), 'caller-owned private configuration')

    def test_tag_and_version_mismatch_and_other_commit_tag_are_refused(self):
        for label in ('v0.2.0', 'v0.2.0-rc.2', 'v0.2.0-rc.01', '../v0.2.0', 'v0.2.0rc1'):
            with self.subTest(label=label), self.assertRaises(builder.ReleaseError):
                self.build(tag=label)
            self.assertFalse((self.root / 'release').exists())
        self.git('tag', 'v0.2.0-rc.1')
        (self.repo / 'LICENSE').write_bytes((self.repo / 'LICENSE').read_bytes() + b'\nfixture extra\n')
        self.git('add', 'LICENSE')
        self.commit()
        with self.assertRaisesRegex(builder.ReleaseError, 'another commit'):
            self.build()
        self.assertFalse((self.root / 'release').exists())

    def test_existing_output_is_preserved_and_in_source_output_refused(self):
        output = self.root / 'release'
        output.mkdir()
        sentinel = output / 'personal-data.txt'
        sentinel.write_text('preserve me')
        with self.assertRaisesRegex(builder.ReleaseError, 'already exists'):
            self.build()
        self.assertEqual(sentinel.read_text(), 'preserve me')
        with self.assertRaisesRegex(builder.ReleaseError, 'outside'):
            builder.build_release(self.repo, self.repo / 'new-release', 'v0.2.0-rc.1')
        self.assertFalse((self.repo / 'new-release').exists())

    def test_committed_private_file_fails_redacted_audit_before_output(self):
        (self.repo / '.env').write_text('synthetic release audit fixture\n')
        self.git('add', '--force', '.env')
        self.commit()
        with self.assertRaisesRegex(builder.ReleaseError, 'redacted release audit') as error:
            self.build()
        self.assertNotIn('synthetic release audit fixture', str(error.exception))
        self.assertFalse((self.root / 'release').exists())

    def test_optional_private_deny_list_refuses_operator_literal_without_printing_it(self):
        term = 'synthetic-private-release-deny-fixture'
        path = self.repo / 'LICENSE'
        path.write_bytes(path.read_bytes() + ('\n' + term + '\n').encode())
        self.git('add', 'LICENSE')
        self.commit()
        deny = self.root / 'private-deny.json'
        deny.write_text(json.dumps([term]))
        deny.chmod(0o600)
        with self.assertRaisesRegex(builder.ReleaseError, 'operator-denied-term') as error:
            builder.build_release(self.repo, self.root / 'release', 'v0.2.0-rc.1', deny_file=deny)
        self.assertNotIn(term, str(error.exception))
        self.assertFalse((self.root / 'release').exists())
        self.assertEqual(json.loads(deny.read_text()), [term])

    def test_missing_executable_git_mode_is_refused(self):
        self.git('update-index', '--chmod=-x', 'stack')
        if os.name == 'posix':
            (self.repo / 'stack').chmod(0o644)
        self.commit()
        with self.assertRaisesRegex(builder.ReleaseError, 'executable'):
            self.build()
        self.assertFalse((self.root / 'release').exists())

    def test_final_version_label_and_existing_matching_tag_work(self):
        (self.repo / 'samurai_stack/__init__.py').write_text('__version__ = "0.2.0"\n')
        self.git('add', 'samurai_stack/__init__.py')
        self.commit()
        self.git('tag', 'v0.2.0')
        _, result = self.build(tag='v0.2.0')
        self.assertEqual(result['version'], '0.2.0')

    @unittest.skipUnless(os.name == 'posix', 'POSIX source and output symlink semantics')
    def test_source_symlink_and_output_symlink_are_refused(self):
        (self.repo / 'unsupported-link').symlink_to('LICENSE')
        self.git('add', 'unsupported-link')
        self.commit()
        with self.assertRaisesRegex(builder.ReleaseError, 'Symlinks'):
            self.build()
        destination = self.root / 'linked-output'
        destination.symlink_to(self.root / 'missing-output')
        with self.assertRaisesRegex(builder.ReleaseError, 'symbolic links'):
            builder.build_release(self.repo, destination, 'v0.2.0-rc.1')
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / 'missing-output').exists())


if __name__ == '__main__':
    unittest.main()
