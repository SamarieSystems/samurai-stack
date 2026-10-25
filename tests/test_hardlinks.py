"""Narrow repair authorization and side-effect boundaries; no real Docker calls."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import io
import json
import subprocess
import unittest
from unittest import mock

from samurai_stack import engine, hardlinks
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError
from test_integrations import VendorModel, secrets


class OwnedTargets:
    def __init__(self, config):
        self.config = config
        self.project = config['deployment_name'] + '-2'
        self.calls = []
        self.interfaces = [{'addr_info': [{'local': config['bind_address']}]}]
        self.endpoint = 'unix:///var/run/docker.sock'
        self.ids = {service: ['owned-' + service] for service in ('sonarr', 'radarr')}
        self.labels = {service: {'systems.samarie.deployment': config['deployment_id'],
                                'systems.samarie.stage': '2', 'com.docker.compose.service': service}
                       for service in self.ids}
        self.ports = {service: {str(hardlinks.catalog.PORTS[service]) + '/tcp':
                              [{'HostIp': config['bind_address'], 'HostPort': str(hardlinks.catalog.PORTS[service])}]}
                      for service in self.ids}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv == ['ip', '-j', 'address', 'show']:
            value = json.dumps(self.interfaces)
        else:
            if argv[:3] != ['docker', '--context', 'default']:
                raise AssertionError('Unexpected command or remote Docker context')
            args = argv[3:]
            if args[:2] == ['context', 'inspect']:
                value = self.endpoint
            elif args[:2] == ['compose', 'version']:
                value = '2.30.0'
            elif args[:1] == ['info']:
                value = 'linux'
            elif args[:1] == ['ps']:
                if 'status=running' not in args or 'label=com.docker.compose.project=' + self.project not in args:
                    raise AssertionError('Repair did not constrain its container query')
                service = next(s for s in self.ids if 'label=com.docker.compose.service=' + s in args)
                value = '\n'.join(self.ids[service])
            elif args[:1] == ['inspect']:
                service = next(s for s in self.ids if args[1] == 'owned-' + s)
                if args[-1] == '{{json .Config.Labels}}':
                    value = json.dumps(self.labels[service])
                elif args[-1] == '{{json .NetworkSettings.Ports}}':
                    value = json.dumps(self.ports[service])
                else:
                    raise AssertionError('Unexpected container inspection')
            else:
                raise AssertionError('Repair attempted a Docker mutation or unrelated command')
        return subprocess.CompletedProcess(argv, 0, value, '')


class NarrowHardlinkRepairTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config('192.168.50.20')
        self.targets = OwnedTargets(self.c)
        self.marker = {'deployment_id': self.c['deployment_id']}
        self.vendor = VendorModel()
        self.linux = mock.patch.object(engine, 'linux_only')
        self.linux.start(); self.addCleanup(self.linux.stop)
        self.marker_patch = mock.patch.object(hardlinks, 'private_read', side_effect=lambda path: json.dumps(self.marker))
        self.marker_patch.start(); self.addCleanup(self.marker_patch.stop)
        self.secret_patch = mock.patch.object(engine, 'secret_resolver', return_value=secrets)
        self.secret_patch.start(); self.addCleanup(self.secret_patch.stop)

    def run_repair(self, **kwargs):
        return hardlinks.run(self.c, runner=self.targets, request=self.vendor, **kwargs)

    def test_custom_media_project_keeps_narrow_owner_and_port_checks(self):
        self.c['compose_projects'] = {'2': 'house-media'}
        self.targets.project = 'house-media'
        self.assertEqual(self.run_repair()['status'], 'verified')
        queries = [argv for argv in self.targets.calls if argv[3:4] == ['ps']]
        self.assertEqual(len(queries), 2)
        self.assertTrue(all('label=com.docker.compose.project=house-media' in argv for argv in queries))

    def test_readonly_checks_only_two_running_owned_apps_and_never_locks_or_writes(self):
        with mock.patch.object(engine, 'deployment_lock') as lock:
            result = self.run_repair()
        self.assertEqual('verified', result['status'])
        lock.assert_not_called()
        self.assertEqual(['GET', 'GET'], [method for method, *_ in self.vendor.calls])
        self.assertTrue(all(url.endswith('/api/v3/config/mediamanagement') for _, url, *_ in self.vendor.calls))
        queried = [argv for argv in self.targets.calls if argv[3:4] == ['ps']]
        self.assertEqual(2, len(queried))
        self.assertTrue(all(any('label=com.docker.compose.service=' + s in argv for s in ('sonarr', 'radarr')) for argv in queried))

    def test_apply_writes_only_two_flags_inside_lock_without_deployment_or_media_operations(self):
        for service in ('sonarr', 'radarr'):
            self.vendor.media_management[service]['copyUsingHardlinks'] = False
        before = copy.deepcopy(self.vendor.media_management)
        locked = []

        @contextmanager
        def lock(config):
            locked.append(True)
            yield
            locked.pop()

        def authorized_http(*args, **kwargs):
            self.assertEqual([True], locked)
            return self.vendor(*args, **kwargs)

        with mock.patch.object(engine, 'deployment_lock', side_effect=lock) as acquire, \
             mock.patch.object(engine, 'apply', side_effect=AssertionError('No deployment allowed')), \
             mock.patch.object(engine, 'configure', side_effect=AssertionError('No broad configuration allowed')), \
             mock.patch.object(engine, 'initialize_install', side_effect=AssertionError('No installation initialization allowed')), \
             mock.patch.object(engine.storage, 'prepare', side_effect=AssertionError('No media preparation allowed')), \
             mock.patch.object(engine.storage, 'permission_probe', side_effect=AssertionError('No media probe allowed')):
            result = hardlinks.run(self.c, apply=True, runner=self.targets, request=authorized_http)
        self.assertTrue(result['ok'], result)
        acquire.assert_called_once_with(self.c)
        for service, original in before.items():
            self.assertEqual(dict(original, copyUsingHardlinks=True), self.vendor.media_management[service])
        writes = [(method, url) for method, url, *_ in self.vendor.calls if method != 'GET']
        self.assertEqual(2, len(writes))
        self.assertTrue(all(method == 'PUT' and url.endswith('/api/v3/config/mediamanagement/1') for method, url in writes))
        self.assertEqual(2, sum(argv == ['ip', '-j', 'address', 'show'] for argv in self.targets.calls))

    def test_apply_rechecks_ownership_after_acquiring_lock_before_any_http(self):
        @contextmanager
        def replaced_during_lock(config):
            self.targets.labels['radarr']['systems.samarie.deployment'] = 'other-owner'
            yield

        with mock.patch.object(engine, 'deployment_lock', side_effect=replaced_during_lock), self.assertRaisesRegex(SafetyError, 'owned by another'):
            self.run_repair(apply=True)
        self.assertEqual([], self.vendor.calls)

    def test_wrong_vm_address_owner_or_docker_endpoint_refuse_before_http(self):
        scenarios = ('linux', 'marker', 'address', 'remote-docker', 'container-owner', 'stage', 'service')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.targets = OwnedTargets(self.c)
                self.marker = {'deployment_id': self.c['deployment_id']}
                if scenario == 'marker': self.marker['deployment_id'] = 'other-owner'
                if scenario == 'address': self.targets.interfaces = [{'addr_info': [{'local': '192.168.50.21'}]}]
                if scenario == 'remote-docker': self.targets.endpoint = 'tcp://192.168.50.21:2375'
                if scenario == 'container-owner': self.targets.labels['sonarr']['systems.samarie.deployment'] = 'other-owner'
                if scenario == 'stage': self.targets.labels['sonarr']['systems.samarie.stage'] = '3'
                if scenario == 'service': self.targets.labels['sonarr']['com.docker.compose.service'] = 'another-app'
                with mock.patch.object(engine, 'linux_only', side_effect=SafetyError('Wrong Linux target') if scenario == 'linux' else None), \
                     self.assertRaises(SafetyError):
                    self.run_repair()
                self.assertEqual([], self.vendor.calls)

    def test_missing_or_ambiguous_running_container_refuses_before_http(self):
        for ids in ([], ['one', 'two']):
            with self.subTest(ids=ids), self.assertRaisesRegex(SafetyError, 'Start this installation'):
                self.targets.ids['sonarr'] = ids
                self.run_repair()
        self.assertEqual([], self.vendor.calls)

    def test_wrong_or_crossed_api_bindings_and_wildcard_bindings_are_refused(self):
        for binding in ({'HostIp': '192.168.50.21', 'HostPort': '8989'},
                        {'HostIp': self.c['bind_address'], 'HostPort': '7878'},
                        {'HostIp': '0.0.0.0', 'HostPort': '8989'}):
            with self.subTest(binding=binding), self.assertRaisesRegex(SafetyError, 'published application port'):
                self.targets.ports['sonarr'] = {'8989/tcp': [binding]}
                self.run_repair()
        self.assertEqual([], self.vendor.calls)

    def test_wrong_json_shapes_refuse_cleanly_before_http(self):
        for kind, value in [('marker', []), ('interfaces', {}), ('interfaces', ['bad']),
                            ('interfaces', [{'addr_info': 'bad'}]), ('interfaces', [{'addr_info': ['bad']}]),
                            ('labels', []), ('ports', []), ('ports', {'8989/tcp': None}),
                            ('ports', {'8989/tcp': ['bad']})]:
            with self.subTest(kind=kind, value=value):
                self.targets = OwnedTargets(self.c)
                self.marker = {'deployment_id': self.c['deployment_id']}
                if kind == 'marker': self.marker = value
                elif kind == 'interfaces': self.targets.interfaces = value
                elif kind == 'labels': self.targets.labels['sonarr'] = value
                else: self.targets.ports['sonarr'] = value
                with self.assertRaises(SafetyError):
                    self.run_repair()
                self.assertEqual([], self.vendor.calls)

    def test_vendor_secret_echo_never_appears_in_result_or_cli_output(self):
        self.vendor.raise_secret = True
        result = self.run_repair()
        self.assertEqual('failed', result['status'])
        self.assertNotIn('never-print-this', json.dumps(result))
        output = io.StringIO()
        with mock.patch.object(hardlinks, 'load_config', return_value=self.c), \
             mock.patch.object(hardlinks, 'run', return_value=result), redirect_stdout(output), redirect_stderr(output):
            code = hardlinks.main([])
        self.assertEqual(2, code)
        self.assertNotIn('never-print-this', output.getvalue())
        self.assertNotIn('test-only-placeholder', output.getvalue())

    def test_cli_parser_defaults_readonly_and_requires_explicit_apply_flag(self):
        for arguments, enabled in (([], False), (['--apply'], True)):
            with self.subTest(arguments=arguments), mock.patch.object(hardlinks, 'load_config', return_value=self.c), \
                 mock.patch.object(hardlinks, 'run', return_value={'ok': True}) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(0, hardlinks.main(arguments))
            run.assert_called_once_with(self.c, apply=enabled)

    def test_private_config_parser_errors_have_generic_terminal_output(self):
        output = io.StringIO()
        with mock.patch.object(hardlinks, 'load_config', side_effect=ValueError('synthetic-private-config-value')), \
             redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(1, hardlinks.main([]))
        self.assertNotIn('synthetic-private-config-value', output.getvalue())
        self.assertIn('No deployment was run', output.getvalue())


if __name__ == '__main__':
    unittest.main()
