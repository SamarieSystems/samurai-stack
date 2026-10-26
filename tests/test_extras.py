import copy
import json
from types import SimpleNamespace
import unittest
import tempfile
from pathlib import Path

from samurai_stack import catalog, extras, extras_capacity_guard, runtime_guard
from samurai_stack.safety import SafetyError
from tests.test_catalog import config


class ExtrasTests(unittest.TestCase):


    def test_photo_ml_opt_out_and_shared_cpu_limits(self):
        c = config()
        c['extras'] = {'photos': {'machine_learning': False}}
        services = catalog.render_stage(c, 5)['services']
        self.assertNotIn('immich-machine-learning', services)
        self.assertEqual(services['database']['mem_limit'], '2g')
        self.assertEqual(services['immich-server']['cpus'], '1')
        for service in services.values():
            self.assertEqual(service['mem_limit'], service['memswap_limit'])
            self.assertNotIn('devices', service)
        policy = extras.photo_policy(c)
        self.assertFalse(policy['machineLearning']['enabled'])
        self.assertTrue(all(x['concurrency'] == 1 for x in policy['job'].values()))


    def test_invalid_optional_settings_and_infinite_quota_are_refused(self):
        for invalid in [{'photos': {'unexpected_option': True}},
                        {'photos': {'user_quota_gib': 0}}, {'photos': {'machine_learning': 'false'}},
                        {'music': {'data_reserve_gib': False}}]:
            c = config()
            c['extras'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(SafetyError):
                extras.validate(c)

    def test_floor_check_is_inclusive_and_read_only(self):
        c = config()
        before = copy.deepcopy(c)
        with self.assertRaises(SafetyError):
            extras.free_space(c, 'photos', usage=lambda _: SimpleNamespace(free=20*1024**3))
        self.assertEqual(c, before)

    def test_capacity_stops_only_extras_and_writes_hold_before_stop(self):
        c = config()
        c['deployment_id'] = 'a-test-id'
        c['storage']['expected_mount'] = {'target': '/mnt/media', 'source': 'UUID=test', 'fstype': 'ext4'}
        calls = []
        def runner(argv):
            calls.append(argv)
            return SimpleNamespace(stdout=json.dumps({'filesystems': [{'target': '/mnt/media', 'source': '/dev/sdb',
                                                                        'uuid': 'test', 'fstype': 'ext4'}]}))
        def hold(value):
            calls.append(['hold', value['stages']])
        result = extras_capacity_guard.enforce({'config': c, 'stages': [5, 6]},
             usage=lambda _: SimpleNamespace(free=19*1024**3), runner=runner, write_hold=hold)
        self.assertEqual(result['stopped_stages'], [5, 6])
        self.assertEqual(calls[1], ['hold', [5, 6]])
        self.assertEqual(calls[2:], [['systemctl', 'stop', 'samurai-example-5.service'],
                                   ['systemctl', 'stop', 'samurai-example-6.service']])

    def test_mount_failure_stops_extras_even_when_fallback_disk_has_space(self):
        c = config()
        c['storage']['expected_mount'] = {'target': '/mnt/media', 'source': 'UUID=right', 'fstype': 'ext4'}
        def runner(argv):
            return SimpleNamespace(stdout=json.dumps({'filesystems': [{'target': '/', 'source': '/dev/sda',
                                                                        'uuid': 'wrong', 'fstype': 'ext4'}]}))
        affected, proof = extras_capacity_guard.check({'config': c, 'stages': [5]},
            usage=lambda _: SimpleNamespace(free=999*1024**3), runner=runner)
        self.assertEqual(affected, [5])
        self.assertFalse(proof['mount_verified'])

    def test_failed_mount_query_latches_and_stops_owned_extras(self):
        c = config()
        c['deployment_id'] = 'test-site'
        c['storage']['expected_mount'] = {'target': '/mnt/media', 'source': 'UUID=right', 'fstype': 'ext4'}
        for bad in [RuntimeError('findmnt failed'), SimpleNamespace(stdout='malformed'), SimpleNamespace(stdout='{}')]:
            calls = []
            def runner(argv):
                calls.append(argv)
                if argv[0] == 'findmnt':
                    if isinstance(bad, Exception):
                        raise bad
                    return bad
                return SimpleNamespace(stdout='')
            hold = []
            result = extras_capacity_guard.enforce({'config': c, 'stages': [5, 6]},
                usage=lambda _: SimpleNamespace(free=999*1024**3), runner=runner, write_hold=hold.append)
            self.assertEqual(result['stopped_stages'], [5, 6])
            self.assertEqual(hold[0]['stages'], [5, 6])
            self.assertEqual(len([x for x in calls if x[0] == 'systemctl']), 2)

    def test_capacity_hold_survives_space_recovery_and_blocks_extra_restart_only(self):
        c = config()
        c['deployment_id'] = 'test-site'
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app/'state').mkdir()
            (app/'state/extras-capacity-hold.json').write_text(json.dumps({'deployment_id': 'test-site', 'stages': [5, 6]}))
            for stage in [5, 6]:
                with self.assertRaisesRegex(ValueError, 'hold requires review'):
                    runtime_guard.extras_ready(c, app, app, stage,
                        usage=lambda _: SimpleNamespace(free=999*1024**3))
            for stage in [2, 3, 4]:
                runtime_guard.extras_ready(c, app, app, stage,
                    usage=lambda _: SimpleNamespace(free=0))


if __name__ == '__main__':
    unittest.main()
