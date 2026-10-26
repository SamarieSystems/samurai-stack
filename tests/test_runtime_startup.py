"""A live Compose supervisor is insufficient proof that its containers started."""
import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import runtime_startup as startup
from samurai_stack.cli import default_config


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config()
        self.c['install_root'] = str(self.root)
        self.c['compose_projects'] = {'3': 'house-torrents'}
        self.folder = self.root / 'stages/house-torrents'
        self.folder.mkdir(parents=True)
        self.compose = self.folder / 'compose.yaml'
        self.compose.write_bytes(b'services:\n  gluetun: {}\n  qbittorrent: {}\n')
        self.site = self.folder / 'site.json'
        self.proof = {'schema_version': 1, 'deployment_id': self.c['deployment_id'], 'stage': 3,
            'project': 'house-torrents', 'config_digest': startup._digest(self.c),
            'compose_sha256': hashlib.sha256(self.compose.read_bytes()).hexdigest(),
            'services': ['gluetun', 'qbittorrent']}
        for path, value in [(self.site, self.c), (self.folder / 'startup.json', self.proof),
                (self.root / '.samurai-install.json', {'deployment_id': self.c['deployment_id']})]:
            path.write_bytes(json.dumps(value).encode()); path.chmod(0o600)
        self.enterContext(patch.object(startup, '_owner'))  # Only fake-root ownership, not file/link/mode checks.
        self.rows = []
        for name, char in [('gluetun', 'a'), ('qbittorrent', 'b')]:
            self.rows.append({'id': char * 64, 'labels': {'systems.samarie.deployment': self.c['deployment_id'],
                'systems.samarie.stage': '3', 'com.docker.compose.project': 'house-torrents',
                'com.docker.compose.service': name}, 'running': True, 'status': 'running',
                'health': 'healthy' if name == 'gluetun' else None,
                'network': 'bridge' if name == 'gluetun' else 'container:' + 'a' * 64})
        self.calls, self.now = [], 0

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.assertEqual(argv[:3], ['docker', '--context', 'default'])
        self.assertIn(argv[3], {'ps', 'inspect'})
        self.assertTrue(kwargs['check'])
        self.assertLessEqual(kwargs['timeout'], 10)
        value = '\n'.join(row['id'][:12] for row in self.rows) if argv[3] == 'ps' else '\n'.join(json.dumps(row) for row in self.rows)
        return subprocess.CompletedProcess(argv, 0, value, '')

    def wait(self):
        def sleep(seconds):
            self.now += seconds
        return startup.wait_ready(self.c, self.proof, runner=self.runner, clock=lambda: self.now, sleep=sleep)

    def test_private_proof_supports_raw_yaml_without_rendering_environment_or_reading_secrets(self):
        c, proof = startup.load(self.site, 3)
        self.assertEqual((c, proof), (self.c, self.proof))
        self.assertTrue(self.wait())
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any('config' in call or 'exec' in call for call in self.calls))

    def test_changed_compose_or_configuration_cannot_reuse_startup_proof(self):
        self.compose.write_bytes(self.compose.read_bytes() + b'# changed after preparation\n')
        with self.assertRaisesRegex(ValueError, 'proof differs'):
            startup.load(self.site, 3)
        self.proof['compose_sha256'] = hashlib.sha256(self.compose.read_bytes()).hexdigest()
        (self.folder / 'startup.json').write_bytes(json.dumps(self.proof).encode())
        self.c['bind_address'] = '192.168.50.99'
        self.site.write_bytes(json.dumps(self.c).encode())
        with self.assertRaisesRegex(ValueError, 'proof differs'):
            startup.load(self.site, 3)

    def test_all_exited_containers_fail_within_the_bound_even_if_compose_process_hangs(self):
        for row in self.rows:
            row.update(running=False, status='exited')
        self.assertFalse(self.wait())
        self.assertEqual(self.now, 120)
        self.assertGreater(len(self.calls), 6)
        self.assertTrue(all(call[3] in {'ps', 'inspect'} for call in self.calls))

    def test_delayed_health_can_complete_without_changing_or_restarting_a_container(self):
        self.rows[0]['health'] = 'starting'
        original = self.runner
        def runner(argv, **kwargs):
            if self.now >= 12:
                self.rows[0]['health'] = 'healthy'
            return original(argv, **kwargs)
        def sleep(seconds):
            self.now += seconds
        self.assertTrue(startup.wait_ready(self.c, self.proof, runner=runner,
            clock=lambda: self.now, sleep=sleep))
        self.assertEqual(self.now, 12)

    def test_foreign_duplicate_or_unknown_service_is_never_counted_as_ready(self):
        original = copy.deepcopy(self.rows)
        for key, value in [('systems.samarie.deployment', 'foreign'),
                           ('systems.samarie.stage', '2'),
                           ('com.docker.compose.service', 'gluetun')]:
            self.rows = copy.deepcopy(original)
            self.rows[1]['labels'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.wait()

    def test_qbit_requires_the_exact_running_gluetun_network_namespace(self):
        self.rows[1]['network'] = 'bridge'
        with self.assertRaisesRegex(ValueError, 'namespace'):
            self.wait()

    def test_missing_expected_service_stays_pending_and_never_expands_the_inventory(self):
        self.rows = self.rows[:1]
        self.assertFalse(self.wait())
        self.assertEqual(self.now, 120)

    def test_vendor_errors_are_not_printed_in_the_service_diagnostic(self):
        with patch.object(sys, 'argv', ['runtime_startup.py', str(self.site), '3']), \
             patch.object(startup, 'wait_ready', side_effect=ValueError('synthetic-private-provider-credential')), \
             redirect_stderr(io.StringIO()) as output:
            self.assertEqual(startup.main(), 1)
        self.assertIn('Startup guard refused readiness', output.getvalue())
        self.assertNotIn('synthetic-private', output.getvalue())

    def test_unknown_or_nonunique_health_state_fails_closed(self):
        self.rows[0]['health'] = 'vendor-unknown-state'
        with self.assertRaises(ValueError):
            self.wait()

    def test_installed_helper_has_no_checkout_dependency_under_isolated_python(self):
        script = self.root / 'runtime_startup.py'
        script.write_bytes(Path(startup.__file__).read_bytes())
        result = subprocess.run([sys.executable, '-I', '-B', str(script)],
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Startup guard refused readiness', result.stderr)
        self.assertNotIn('ModuleNotFoundError', result.stderr)


if __name__ == '__main__':
    unittest.main()
