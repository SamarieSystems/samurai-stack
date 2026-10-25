"""Reviewed APT-media repair against a disposable filesystem, never real APT."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import apt_sources
from samurai_stack.safety import SafetyError


NETWORK = ('deb https://deb.debian.org/debian trixie main non-free-firmware\n'
           'deb https://deb.debian.org/debian trixie-updates main\n'
           'deb https://security.debian.org/debian-security trixie-security main\n')
DVD = 'deb cdrom:[Debian GNU/Linux 13 DVD]/ trixie main non-free-firmware\n'


class AptSourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.apt = self.root / 'etc/apt'
        self.fragments = self.apt / 'sources.list.d'
        self.fragments.mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n')
        self.source = self.apt / 'sources.list'
        self.source.write_text(DVD + NETWORK)
        self.source.chmod(0o644)
        self.backup = self.root / 'private-backup'

    def inspect(self):
        return apt_sources.inspect_install_media(self.root)

    def apply(self, plan=None):
        return apt_sources.apply_install_media(plan or self.inspect(), self.backup, root=self.root)

    def contents(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_inspection_and_decline_are_readonly(self):
        before = self.contents()
        plan = self.inspect()
        self.assertEqual(1, len(plan['changes']))
        self.assertEqual(3, plan['network_sources'])
        self.assertEqual(before, self.contents())
        self.assertFalse(self.backup.exists())

    def test_stock_os_release_symlink_supported(self):
        original = self.root / 'etc/os-release'
        target = self.root / 'usr/lib/os-release'
        target.parent.mkdir(parents=True)
        target.write_bytes(original.read_bytes())
        original.unlink()
        try:
            original.symlink_to(Path('..') / 'usr/lib/os-release')
        except OSError:
            self.skipTest('Symbolic-link privilege unavailable')
        self.assertEqual(1, len(self.inspect()['changes']))

    def test_legacy_repair_comments_only_active_dvd_lines_keeps_network_and_comments(self):
        original = ('# existing operator comment\n' + DVD +
                    '  deb-src [arch=amd64] cdrom:[Sources DVD]/ trixie main\n' +
                    '# deb cdrom:[already disabled]/ trixie main\n' + NETWORK +
                    'deb https://download.docker.com/linux/debian trixie stable\n')
        self.source.write_text(original)
        result = self.apply()
        expected = original.replace(DVD, '# ' + DVD).replace(
            '  deb-src [arch=amd64] cdrom:', '#   deb-src [arch=amd64] cdrom:')
        self.assertEqual(expected, self.source.read_text())
        self.assertEqual(['etc/apt/sources.list'], result['changed'])
        self.assertEqual(original, (self.backup / 'source-0001.txt').read_text())

    def test_crlf_source_bytes_and_network_lines_are_preserved(self):
        original = (DVD + NETWORK).replace('\n', '\r\n').encode()
        self.source.write_bytes(original)
        self.apply()
        self.assertEqual(b'# ' + original, self.source.read_bytes())
        self.assertEqual(original, (self.backup / 'source-0001.txt').read_bytes())

    def test_media_only_without_online_os_mirror_refuses_without_mutation(self):
        for source in (DVD, DVD + 'deb https://download.docker.com/linux/debian trixie stable\n'):
            with self.subTest(source=source):
                self.source.write_text(source)
                before = self.contents()
                with self.assertRaisesRegex(SafetyError, 'No active HTTP'):
                    self.inspect()
                self.assertEqual(before, self.contents())

    def test_deb822_only_media_stanza_disabled_network_stanza_byte_exact(self):
        self.source.write_text(NETWORK)
        media = ('# media comment\nTypes: deb\n deb-src\nURIs:\n cdrom:[Debian DVD]/\n'
                 'Suites: trixie\nComponents: main\nEnabled:\n Yes\n')
        network = ('# network comment\nTypes: deb\nURIs: https://deb.debian.org/debian\n'
                   'Suites: trixie trixie-updates\nComponents: main\n')
        target = self.fragments / 'debian.sources'
        target.write_text(media + '\n' + network)
        self.apply()
        expected = media.replace('Enabled:\n Yes\n', 'Enabled: no\n') + '\n' + network
        self.assertEqual(expected, target.read_text())
        self.assertEqual(NETWORK, self.source.read_text())

    def test_deb822_absent_enabled_field_appended_without_touching_other_fields(self):
        self.source.write_text(NETWORK)
        target = self.fragments / 'media.sources'
        original = 'Types: deb\nURIs: cdrom:[DVD]/\nSuites: trixie\nComponents: main'
        target.write_text(original)
        self.apply()
        self.assertEqual(original + '\nEnabled: no\n', target.read_text())

    def test_disabled_aliases_commented_lines_and_hidden_fragments_ignored(self):
        self.source.write_text('# ' + DVD + NETWORK)
        for index, value in enumerate(sorted(apt_sources.FALSE_VALUES)):
            (self.fragments / f'disabled-{index}.sources').write_text(
                'Types: deb\nURIs: cdrom:[DVD]/\nEnabled: ' + value.upper() + '\n')
        (self.fragments / '.old.sources').write_text('Types: deb\nURIs: cdrom:[DVD]/\n')
        (self.fragments / 'old backup.list').write_text(DVD)
        before = self.contents()
        self.assertEqual([], self.inspect()['changes'])
        self.assertEqual({'changed': [], 'backup_directory': None}, self.apply())
        self.assertEqual(before, self.contents())

    def test_mixed_network_and_cdrom_stanza_refuses_instead_of_disabling_network(self):
        for uri in ('https://deb.debian.org/debian', 'ftp://example.invalid/debian'):
            with self.subTest(uri=uri):
                target = self.fragments / 'mixed.sources'
                target.write_text('Types: deb\nURIs: cdrom:[DVD]/\n ' + uri + '\nSuites: trixie\nComponents: main\n')
                before = self.contents()
                with self.assertRaisesRegex(SafetyError, 'mixes installation media'):
                    self.inspect()
                self.assertEqual(before, self.contents())

    def test_ambiguous_duplicate_fields_refuse(self):
        target = self.fragments / 'ambiguous.sources'
        target.write_text('Types: deb\nURIs: cdrom:[DVD]/\nEnabled: yes\nEnabled: yes\n')
        with self.assertRaisesRegex(SafetyError, 'ambiguous'):
            self.inspect()

    def test_stale_network_edit_and_added_source_refuse_before_backup(self):
        plan = self.inspect()
        self.source.write_text(DVD + NETWORK + '# concurrent edit\n')
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.apply(plan)
        self.assertFalse(self.backup.exists())
        plan = self.inspect()
        (self.fragments / 'new.list').write_text('# newly added source fragment\n')
        with self.assertRaisesRegex(SafetyError, 'changed after review'):
            self.apply(plan)
        self.assertFalse(self.backup.exists())

    def test_tampered_plan_rejected(self):
        plan = copy.deepcopy(self.inspect())
        plan['changes'][0]['after_text'] = '# unrelated injected source change\n'
        with self.assertRaisesRegex(SafetyError, 'integrity check'):
            self.apply(plan)
        self.assertFalse(self.backup.exists())

    def test_wrong_root_rejected(self):
        plan = self.inspect()
        with self.assertRaisesRegex(SafetyError, 'integrity check'):
            apt_sources.apply_install_media(plan, self.backup, root=self.root / 'other-root')

    def test_originals_private_and_source_permissions_preserved(self):
        before = self.source.stat()
        self.apply()
        after = self.source.stat()
        manifest = json.loads((self.backup / 'manifest.json').read_text())
        self.assertEqual('complete', manifest['state'])
        self.assertEqual(['etc/apt/sources.list'], manifest['changed'])
        if os.name == 'posix':
            self.assertEqual((before.st_uid, before.st_gid, before.st_mode & 0o777),
                             (after.st_uid, after.st_gid, after.st_mode & 0o777))
            self.assertEqual(0o700, self.backup.stat().st_mode & 0o777)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.backup.iterdir()))
        plan = self.inspect()
        self.assertEqual([], plan['changes'])
        self.assertEqual([], self.apply(plan)['changed'])

    def test_fresh_private_backup_required(self):
        self.backup.mkdir()
        with self.assertRaisesRegex(SafetyError, 'fresh backup'):
            self.apply()
        self.assertEqual(DVD + NETWORK, self.source.read_text())

    def test_backup_failure_prevents_any_source_replacement(self):
        with mock.patch.object(apt_sources, 'atomic_write', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(DVD + NETWORK, self.source.read_text())

    def test_partial_write_failure_retains_all_originals_and_progress(self):
        second = self.fragments / 'other-media.list'
        second.write_text(DVD)
        original = apt_sources._replace_source
        written = []

        def fail_second(root, path, snapshot, content):
            if written:
                raise OSError('synthetic replacement failure')
            original(root, path, snapshot, content)
            written.append(path)

        with mock.patch.object(apt_sources, '_replace_source', side_effect=fail_second):
            with self.assertRaisesRegex(SafetyError, 'private originals and progress'):
                self.apply()
        manifest = json.loads((self.backup / 'manifest.json').read_text())
        self.assertEqual('failed', manifest['state'])
        self.assertEqual(1, len(manifest['changed']))
        self.assertEqual(2, len(manifest['files']))
        backups = [(self.backup / item['backup']).read_text() for item in manifest['files']]
        self.assertIn(DVD + NETWORK, backups)
        self.assertIn(DVD, backups)

    def test_source_hardlinks_and_symlinks_refused(self):
        alias = self.fragments / 'alias.list'
        os.link(self.source, alias)
        with self.assertRaisesRegex(SafetyError, 'hard links'):
            self.inspect()
        alias.unlink()
        try:
            alias.symlink_to(self.source)
        except OSError:
            return  # Windows may not grant symbolic-link privilege.
        with self.assertRaisesRegex(SafetyError, 'symbolic link'):
            self.inspect()

    @unittest.skipUnless(os.name == 'posix', 'POSIX source ownership and modes')
    def test_nonadministrator_writable_sources_and_directories_refused(self):
        self.source.chmod(0o664)
        with self.assertRaisesRegex(SafetyError, 'not writable'):
            self.inspect()
        self.source.chmod(0o644)
        self.fragments.chmod(0o777)
        with self.assertRaisesRegex(SafetyError, 'not writable'):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
