import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import storage
from samurai_stack.safety import SafetyError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'media'
        self.c = {'deployment_id': '4f4136b5-cb25-4b03-9883-7135fd1ef081',
                  'puid': os.getuid() if hasattr(os, 'getuid') else 1000,
                  'pgid': os.getgid() if hasattr(os, 'getgid') else 1000,
                  'storage': {'data_root': str(self.root), 'mode': 'nfs',
                              'expected_mount': {'target': str(self.parent), 'source': 'nas:/media', 'fstype': 'nfs4'}}}

    def runner(self, argv):
        return SimpleNamespace(stdout=json.dumps({'filesystems': [{**self.c['storage']['expected_mount'], 'options': 'rw,hard'}]}))

    def test_missing_nas_does_not_create_a_local_directory(self):
        def wrong(argv):
            return SimpleNamespace(stdout=json.dumps({'filesystems': [{'target': '/', 'source': '/dev/root', 'fstype': 'ext4', 'options': 'rw'}]}))
        with self.assertRaises(SafetyError):
            storage.prepare(self.c, wrong)
        self.assertFalse(self.root.exists())

    def test_preparation_is_repeatable_and_preserves_existing_file_and_mode(self):
        storage.prepare(self.c, self.runner)
        precious = self.root / 'family-document.txt'
        precious.write_text('do not touch')
        old_mode = precious.stat().st_mode
        storage.prepare(self.c, self.runner)
        storage.verify(self.c, self.runner)
        self.assertEqual(precious.read_text(), 'do not touch')
        self.assertEqual(precious.stat().st_mode, old_mode)
        self.assertFalse(list(self.root.rglob('.samurai-probe-*')))

    def test_unmanaged_existing_files_refuse_adoption(self):
        self.root.mkdir()
        (self.root / 'baby-photo.jpg').write_bytes(b'precious')
        with self.assertRaises(SafetyError):
            storage.prepare(self.c, self.runner)
        self.assertEqual((self.root / 'baby-photo.jpg').read_bytes(), b'precious')
        self.assertFalse(storage.marker(self.c).exists())

    def test_partial_directory_creation_is_recoverable(self):
        original = storage._mkdir_owned
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError('synthetic interrupted write')
            return original(*args)
        with patch.object(storage, '_mkdir_owned', interrupted), self.assertRaises(OSError):
            storage.prepare(self.c, self.runner)
        self.assertTrue(storage.marker(self.c).exists())
        storage.prepare(self.c, self.runner)
        storage.verify(self.c, self.runner)

    def test_marker_for_another_deployment_is_not_adopted(self):
        storage.prepare(self.c, self.runner)
        self.c['deployment_id'] = 'd2d2359a-5ea2-4d6e-a104-6e14bc008d05'
        with self.assertRaises(SafetyError):
            storage.prepare(self.c, self.runner)

    def test_verify_rejects_replaced_subdirectory_symlink(self):
        storage.prepare(self.c, self.runner)
        path = self.root / 'library/movies'
        path.rmdir()
        other = self.parent / 'unrelated'
        other.mkdir()
        try:
            path.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(SafetyError):
            storage.verify(self.c, self.runner)


if __name__ == '__main__':
    unittest.main()
