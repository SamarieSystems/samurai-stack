"""Execute the actual focused repair payload without checkout import fallbacks."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

from samurai_stack.cli import default_config
from samurai_stack.safety import write_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hardlink_bundle_builder', ROOT / 'scripts/build-hardlink-repair.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class HardlinkBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='samurai-hardlink-bundle-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.empty = self.root / 'empty-cwd'
        self.empty.mkdir()
        self.bundle = builder.build(self.root / 'samurai-hardlinks.pyz')

    def test_actual_asset_help_runs_with_isolated_python_from_empty_cwd(self):
        result = subprocess.run([sys.executable, '-I', str(self.bundle), '--help'], cwd=self.empty,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--config', result.stdout)
        self.assertIn('--apply', result.stdout)
        self.assertEqual(list(self.empty.iterdir()), [])

    def test_actual_payload_verifies_named_projects_with_only_bundled_modules(self):
        config = default_config('192.168.50.21')
        application = self.root / 'application'
        application.mkdir(mode=0o700)
        config['install_root'] = application.as_posix().removeprefix(application.drive)
        config['compose_projects'] = {'2': 'house-media'}
        write_json(application / '.samurai-install.json', {'deployment_id': config['deployment_id']})
        private = application / 'secrets'
        private.mkdir(mode=0o700)
        for service in ('sonarr', 'radarr'):
            key = private / (service + '_api_key')
            key.write_text('a' * 32)
            key.chmod(0o600)
        site = self.root / 'site.json'
        write_json(site, config)
        script = textwrap.dedent('''
            import json, os, subprocess, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from samurai_stack import catalog, engine, hardlinks, naming
            from samurai_stack.safety import load_config
            for module in (catalog, engine, hardlinks, naming):
                assert module.__file__.startswith(sys.argv[1]), module.__file__
            config = load_config(sys.argv[2])
            if not sys.platform.startswith('linux'):
                engine.linux_only = lambda: None  # Only the platform boundary is simulated on Windows.
            queries, requests = [], []
            def runner(argv, **kwargs):
                if argv == ['ip', '-j', 'address', 'show']:
                    value = json.dumps([{'addr_info': [{'local': config['bind_address']}]}])
                else:
                    assert argv[:3] == ['docker', '--context', 'default'], argv
                    args = argv[3:]
                    if args[:2] == ['context', 'inspect']:
                        value = 'unix:///var/run/docker.sock'
                    elif args[:2] == ['compose', 'version']:
                        value = '2.30.0'
                    elif args[:1] == ['info']:
                        value = 'linux'
                    elif args[:1] == ['ps']:
                        assert 'label=com.docker.compose.project=house-media' in args, args
                        assert 'status=running' in args, args
                        service = next(s for s in ('sonarr', 'radarr') if 'label=com.docker.compose.service=' + s in args)
                        queries.append(service)
                        value = 'owned-' + service
                    elif args[:1] == ['inspect']:
                        service = args[1].removeprefix('owned-')
                        assert service in ('sonarr', 'radarr'), args
                        if args[-1] == '{{json .Config.Labels}}':
                            value = json.dumps({'systems.samarie.deployment': config['deployment_id'],
                                'systems.samarie.stage': '2', 'com.docker.compose.service': service,
                                'com.docker.compose.project': 'house-media'})
                        else:
                            assert args[-1] == '{{json .NetworkSettings.Ports}}', args
                            port = str(catalog.PORTS[service])
                            value = json.dumps({port + '/tcp': [{'HostIp': config['bind_address'], 'HostPort': port}]})
                    else:
                        raise AssertionError('No Docker mutations are permitted: ' + repr(args))
                return subprocess.CompletedProcess(argv, 0, value, '')
            def request(method, url, **kwargs):
                assert method == 'GET' and url.endswith('/api/v3/config/mediamanagement'), (method, url)
                assert kwargs['headers']['X-Api-Key'] == 'a' * 32
                requests.append(method)
                return {'id': 1, 'copyUsingHardlinks': True}
            result = hardlinks.run(config, runner=runner, request=request)
            assert result['status'] == 'verified' and result['ok'] is True, result
            assert queries == ['sonarr', 'radarr'] and requests == ['GET', 'GET'], (queries, requests)
            print(json.dumps({'status': result['status'], 'project': naming.project_name(config, 2)}))
        ''')
        result = subprocess.run([sys.executable, '-I', '-c', script, str(self.bundle), str(site)], cwd=self.empty,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'verified', 'project': 'house-media'})
        self.assertEqual(list(self.empty.iterdir()), [])
        self.assertEqual(json.loads(site.read_text()), config)
        self.assertFalse((application / 'state').exists())

    def test_payload_includes_naming_source_but_no_configuration_or_tests(self):
        with zipfile.ZipFile(self.bundle) as archive:
            names = set(archive.namelist())
            self.assertIn('samurai_stack/naming.py', names)
            self.assertEqual(archive.read('samurai_stack/naming.py'), (ROOT / 'samurai_stack/naming.py').read_bytes())
            self.assertTrue(all(name == 'LICENSE' or name.endswith('.py') for name in names))
            self.assertFalse(any(name.startswith('tests/') or '/secrets/' in name for name in names))


if __name__ == '__main__':
    unittest.main()
