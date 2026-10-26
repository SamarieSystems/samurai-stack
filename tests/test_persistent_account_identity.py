"""A reboot may renumber block devices, never the owned filesystem or inode."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import onboarding, storage


class PersistentAccountIdentityTests(unittest.TestCase):
    def setUp(self):
        self.saved = {"device": 2049, "inode": 1234, "filesystem": {
            "target": "/", "source": "UUID=fixture-app-filesystem", "fstype": "ext4"}}

    def test_same_filesystem_and_inode_survive_device_renumbering(self):
        current = {**self.saved, "device": 2065}
        self.assertTrue(onboarding.same_identity(self.saved, current))

    def test_filesystem_or_inode_changes_remain_rejected(self):
        for key, value in (("target", "/other"), ("source", "UUID=foreign-filesystem"), ("fstype", "xfs")):
            with self.subTest(key=key):
                current = copy.deepcopy(self.saved); current["filesystem"][key] = value
                self.assertFalse(onboarding.same_identity(self.saved, current))
        self.assertFalse(onboarding.same_identity(self.saved, {**self.saved, "inode": 4567}))

    def test_legacy_receipt_does_not_silently_rebind_a_renumbered_device(self):
        legacy = {"device": 2049, "inode": 1234}
        self.assertTrue(onboarding.same_identity(legacy, self.saved))
        self.assertFalse(onboarding.same_identity(legacy, {**self.saved, "device": 2065}))
        self.assertEqual(legacy, {"device": 2049, "inode": 1234})

    def test_unknown_or_missing_stable_identity_is_rejected(self):
        for current in (None, {}, {"device": 2049, "inode": 1234},
                        {**self.saved, "filesystem": {}},
                        {**self.saved, "filesystem": {**self.saved["filesystem"], "unexpected": "value"}},
                        {**self.saved, "filesystem": {"target": "/", "source": "overlay", "fstype": "overlay"}},
                        {**self.saved, "inode": True}):
            with self.subTest(current=current):
                self.assertFalse(onboarding.same_identity(self.saved, current))

    @unittest.skipUnless(os.name == "posix", "Linux filesystem identity")
    def test_local_identity_records_uuid_and_anonymous_mount_keeps_legacy_device_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            row = {"target": "/", "source": "/dev/sdb1", "fstype": "ext4", "options": "rw", "uuid": "fixture-app-filesystem"}
            with patch.object(storage, "mount_identity", return_value=row):
                identity = onboarding._identity(Path(folder))
            self.assertEqual(identity["filesystem"]["source"], "UUID=fixture-app-filesystem")
            with patch.object(storage, "mount_identity", return_value={"target": "/", "source": "overlay", "fstype": "overlay"}):
                anonymous = onboarding._identity(Path(folder))
            self.assertNotIn("filesystem", anonymous)
            self.assertFalse(onboarding.same_identity(anonymous, {**anonymous, "device": anonymous["device"] + 1}))


if __name__ == "__main__":
    unittest.main()
