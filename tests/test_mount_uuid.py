"""A reboot may rename block devices, but must never rebind storage ownership."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import capacity_guard, runtime_guard, storage
from samurai_stack.safety import SafetyError


UUID = '11111111-2222-4333-8444-555555555555'


class PersistentMountTests(unittest.TestCase):
    def row(self, source='/dev/sdb', identifier=UUID):
        return {'target': '/mnt/media', 'source': source, 'fstype': 'ext4',
                'options': 'rw,relatime', 'uuid': identifier}

    def test_both_standalone_guards_keep_the_same_uuid_across_disk_renumbering(self):
        for helper in (runtime_guard.stable_mount, capacity_guard.stable_mount):
            with self.subTest(helper=helper.__module__):
                self.assertEqual(helper(self.row('/dev/sda')), helper(self.row('/dev/sdb')))
                self.assertEqual(helper(self.row())['source'], 'UUID=' + UUID)

    def test_missing_or_malformed_uuid_cannot_approve_a_local_block_device(self):
        for identifier in (None, '', 'bad\nvalue', '../../other'):
            for helper in (runtime_guard.stable_mount, capacity_guard.stable_mount):
                with self.subTest(identifier=identifier, helper=helper.__module__), self.assertRaises(ValueError):
                    helper(self.row(identifier=identifier))

    def test_nfs_exports_and_zfs_datasets_retain_exact_source_identity(self):
        for fstype, source in [('nfs4', 'nas:/exports/media'), ('zfs', 'tank/media')]:
            row = {**self.row(), 'source': source, 'fstype': fstype, 'uuid': None}
            for helper in (runtime_guard.stable_mount, capacity_guard.stable_mount):
                self.assertEqual(helper(row)['source'], source)

    def test_btrfs_subvolume_is_not_discarded(self):
        for helper in (runtime_guard.stable_mount, capacity_guard.stable_mount):
            row = {**self.row('/dev/sdc[/@media]'), 'fstype': 'btrfs'}
            self.assertEqual(helper(row)['source'], 'UUID=' + UUID + '[/@media]')
            self.assertNotEqual(helper(row), helper({**row, 'source': '/dev/sdc[/@private]'}))

    def test_storage_guard_accepts_uuid_after_reboot_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'storage': {'mode': 'local', 'data_root': directory,
                'expected_mount': {'target': directory, 'source': 'UUID=' + UUID, 'fstype': 'ext4'}}}
            def runner(argv):
                self.assertIn('UUID', argv[-1])
                return SimpleNamespace(stdout=json.dumps({'filesystems': [{**self.row('/dev/sda'), 'target': directory}]}))
            self.assertEqual(storage.check_mount(config, runner)['source'], 'UUID=' + UUID)
            wrong = lambda argv: SimpleNamespace(stdout=json.dumps({'filesystems': [
                {**self.row('/dev/sda', 'wrong-disk'), 'target': directory}]}))
            with self.assertRaises(SafetyError):
                storage.check_mount(config, wrong)

    def test_legacy_config_is_not_silently_rebound_to_a_different_device(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'storage': {'mode': 'local', 'data_root': directory,
                'expected_mount': {'target': directory, 'source': '/dev/sdb', 'fstype': 'ext4'}}}
            runner = lambda argv: SimpleNamespace(stdout=json.dumps({'filesystems': [
                {**self.row('/dev/sda'), 'target': directory}]}))
            with self.assertRaises(SafetyError):
                storage.check_mount(config, runner)


class NetworkReadinessTests(unittest.TestCase):
    def response(self, value):
        return SimpleNamespace(stdout=json.dumps(value))

    def addresses(self, *, assigned=True, up=True):
        return [{'ifname':'ens18','flags':['UP'] if up else [],
                 'addr_info':[{'local':'192.168.50.21'}] if assigned else []}]

    def test_bound_lan_address_and_default_route_are_required_before_compose(self):
        with patch.object(runtime_guard.subprocess,'run',side_effect=[
            self.response(self.addresses()),self.response([{'dst':'default','dev':'ens18','gateway':'192.168.50.1'}])]) as run:
            runtime_guard.network_ready({'bind_address':'192.168.50.21'}, require_default=True)
        self.assertEqual(run.call_args_list[-1].args[0],['ip','-j','-4','route','show','default'])

    def test_target_reached_without_assigned_address_still_delays_startup(self):
        for rows in (self.addresses(assigned=False),self.addresses(up=False)):
            with patch.object(runtime_guard.subprocess,'run',return_value=self.response(rows)),self.assertRaises(runtime_guard.NetworkPending):
                runtime_guard.network_ready({'bind_address':'192.168.50.21'}, require_default=True)

    def test_media_and_management_need_no_wan_route_for_lan_use(self):
        with patch.object(runtime_guard.subprocess,'run',return_value=self.response(self.addresses())) as run:
            runtime_guard.network_ready({'bind_address':'192.168.50.21'})
        self.assertEqual(run.call_count,1)

    def test_vpn_accepts_a_separate_up_wan_interface_default(self):
        interfaces=self.addresses()+[{'ifname':'wan0','flags':['UP'],'addr_info':[{'local':'198.51.100.2'}]}]
        with patch.object(runtime_guard.subprocess,'run',side_effect=[self.response(interfaces),
            self.response([{'dst':'default','dev':'wan0','gateway':'198.51.100.1'}])]):
            runtime_guard.network_ready({'bind_address':'192.168.50.21'},require_default=True)

    def test_missing_or_other_interface_default_route_delays_vpn_startup(self):
        for routes in ([],[{'dst':'default','dev':'tailscale0'}],[{'dst':'default','dev':'ens18','type':'blackhole'}]):
            with patch.object(runtime_guard.subprocess,'run',side_effect=[
                self.response(self.addresses()),self.response(routes)]),self.assertRaises(runtime_guard.NetworkPending):
                runtime_guard.network_ready({'bind_address':'192.168.50.21'},require_default=True)


if __name__ == '__main__':
    unittest.main()
