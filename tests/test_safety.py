import copy
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack.cli import default_config
from samurai_stack import safety


class ConfigTests(unittest.TestCase):
    def test_default_configuration_is_valid_and_contains_no_credentials(self):
        safety.validate_config(default_config())

    def test_refuses_public_wildcard_and_multicast_bindings(self):
        for value in ['0.0.0.0', '8.8.8.8', '224.0.0.1', '169.254.1.1']:
            c = default_config()
            c['bind_address'] = value
            with self.subTest(value=value), self.assertRaises(safety.SafetyError):
                safety.validate_config(c)

    def test_no_system_roots_traversal_or_compose_interpolation(self):
        for value in ['/', '/home', '/etc/config', '/srv/a/../b', '/srv/$HOME', '/srv/has space']:
            c = default_config()
            c['install_root'] = value
            with self.subTest(value=value), self.assertRaises(safety.SafetyError):
                safety.validate_config(c)

    def test_media_and_database_roots_cannot_overlap(self):
        c = default_config()
        c['install_root'] = '/mnt/media/samurai/appdata'
        with self.assertRaises(safety.SafetyError):
            safety.validate_config(c)

    def test_credentials_must_not_live_in_configuration(self):
        c = default_config()
        c['vpn']['password'] = 'synthetic-only-fixture'  # release-audit: synthetic - intentionally invalid configuration test
        with self.assertRaises(safety.SafetyError):
            safety.validate_config(c)

    def test_local_mode_cannot_claim_network_filesystem(self):
        c = default_config()
        c['storage']['mode'] = 'local'
        with self.assertRaises(safety.SafetyError):
            safety.validate_config(c)

    def test_string_false_does_not_enable_storage_adoption_or_forwarding(self):
        for section, field in [('storage','allow_existing_contents'),('vpn','port_forwarding')]:
            c = default_config()
            c[section][field] = 'false'
            with self.subTest(field=field), self.assertRaises(safety.SafetyError):
                safety.validate_config(c)


class FileTests(unittest.TestCase):
    def test_interrupted_replacement_preserves_existing_file_and_cleans_temporary_input(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'receipt.json'
            p.write_text('previous verified receipt\n')
            before = p.read_bytes()
            with mock.patch.object(safety.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'synthetic full filesystem')):
                with self.assertRaises(OSError):
                    safety.atomic_write(p, 'incomplete replacement\n', replace=True)
            self.assertEqual(p.read_bytes(), before)
            self.assertEqual({item.name for item in Path(folder).iterdir()}, {'receipt.json'})

    def test_atomic_write_cannot_clobber_existing_document(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'precious.txt'
            p.write_text('family photos metadata')
            with self.assertRaises(safety.SafetyError):
                safety.atomic_write(p, 'replacement')
            self.assertEqual(p.read_text(), 'family photos metadata')

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(safety.SafetyError):
                safety.contained(Path(folder).parent / 'outside', folder)

    def test_symlink_parent_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p / 'real').mkdir()
            try:
                (p / 'link').symlink_to(p / 'real', target_is_directory=True)
            except OSError:
                self.skipTest('symlink privilege unavailable')
            with self.assertRaises(safety.SafetyError):
                safety.atomic_write(p / 'link' / 'file', 'unwanted')

    @unittest.skipUnless(os.name == 'posix', 'Unix file permission contract')
    def test_private_file_rejects_group_read(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'private'
            p.write_text('synthetic fixture')
            p.chmod(0o640)
            with self.assertRaises(safety.SafetyError):
                safety.private_read(p)


if __name__ == '__main__':
    unittest.main()
