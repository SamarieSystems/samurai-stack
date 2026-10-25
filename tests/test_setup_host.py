"""Guided bootstrap gates, recovery evidence and real private subprocess output."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from samurai_stack import setup_host
from samurai_stack.safety import digest


class BootstrapFixture:
    def __init__(self):
        self.calls = []
        self.packages = ['curl', 'qemu-guest-agent', 'docker-ce', 'docker-compose-plugin']
        self.installed = {}
        self.ready = False
        self.stale = False
        self.fail_plan = False
        self.false_ready = False
        self.revision = 0
        self.last_plan = None

    def __call__(self, argv, attempt, phase, emit):
        self.calls.append((phase, list(argv), attempt))
        code = 0
        if phase == 'plan':
            if self.fail_plan:
                return subprocess.CompletedProcess(argv, 1, '', 'synthetic private vendor payload')
            payload = {'kind': 'guest-bootstrap', 'os': 'debian', 'version': '13', 'architecture': 'amd64',
                       'machine_id': 'a' * 32, 'packages': self.packages,
                       'installed_packages': copy.deepcopy(self.installed), 'fixture_revision': self.revision}
            payload['review_hash'] = digest(payload)
            self.last_plan = copy.deepcopy(payload)
        elif phase == 'apply':
            saved = json.loads(Path(argv[argv.index('--plan-file') + 1]).read_text())
            accepted = argv[argv.index('--accept-plan') + 1]
            assert saved == self.last_plan and accepted == saved['review_hash']
            assert '--ack-new-guest' in argv
            if self.stale:
                code, payload = 1, {'state': 'refused-stale-plan'}
            else:
                self.installed = {name: 'fixture-version' for name in self.packages}
                self.ready = True
                payload = {'state': 'started' if self.false_ready else 'ready'}
        else:
            assert phase in {'verify-before', 'verify-after'}
            assert '--verify' in argv
            code = 0 if self.ready else 1
            payload = {'state': 'ready', 'docker_server': '29.0.0', 'compose': '2.30.0', 'guest_agent': 'active'}
        return subprocess.CompletedProcess(argv, code, json.dumps(payload), 'synthetic private vendor payload' if code else '')


class SetupHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout'
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'scripts/bootstrap-host.sh').write_text('# synthetic wrapper target\n')
        self.channel = self.root / 'dev/virtio-ports/org.qemu.guest_agent.0'
        self.channel.parent.mkdir(parents=True)
        self.channel.write_text('synthetic channel presence')
        self.fixture = BootstrapFixture()
        self.messages = []
        self.confirm = mock.Mock(return_value=True)

    def prepare(self):
        return setup_host._prepare_host(self.repo, self.root, self.fixture, self.confirm, self.messages.append)

    def attempts(self):
        return sorted((self.root / 'var/lib/samurai-stack/setup-host').glob('attempt-*'))

    def test_apply_preserves_exact_plan_and_hash_then_runs_live_verification(self):
        self.assertTrue(self.prepare())
        self.assertEqual(['plan', 'apply', 'verify-after'], [call[0] for call in self.fixture.calls])
        self.confirm.assert_called_once()
        attempt = self.attempts()[0]
        report = json.loads((attempt / 'result.json').read_text())
        self.assertEqual('ready', report['status'])
        self.assertEqual('applied_and_verified', report['method'])
        self.assertEqual(self.fixture.last_plan, json.loads((attempt / 'plan.json').read_text()))
        self.assertTrue(any('Existing packages are not upgraded or removed' in m for m in self.messages))
        self.assertNotIn('review_hash', '\n'.join(self.messages))
        if os.name == 'posix':
            self.assertEqual(attempt.stat().st_mode & 0o777, 0o700)
            self.assertEqual((attempt / 'plan.json').stat().st_mode & 0o777, 0o600)

    def test_decline_retains_review_but_never_applies(self):
        self.confirm.return_value = False
        self.assertFalse(self.prepare())
        self.assertEqual(['plan'], [call[0] for call in self.fixture.calls])
        self.assertEqual('declined', json.loads((self.attempts()[0] / 'result.json').read_text())['status'])

    def test_missing_guest_channel_is_pending_before_confirmation_or_apply(self):
        self.channel.unlink()
        self.assertFalse(self.prepare())
        self.confirm.assert_not_called()
        self.assertEqual(['plan'], [call[0] for call in self.fixture.calls])
        report = json.loads((self.attempts()[0] / 'result.json').read_text())
        self.assertEqual('pending', report['status'])
        self.assertEqual('qemu_guest_agent_channel', report['prerequisite'])
        self.assertTrue(any('Options > QEMU Guest Agent' in m and 'reboot alone is not enough' in m for m in self.messages))

    def test_already_ready_resumes_without_apply_or_confirmation(self):
        self.fixture.installed = {name: 'fixture-version' for name in self.fixture.packages}
        self.fixture.ready = True
        self.assertTrue(self.prepare())
        self.confirm.assert_not_called()
        self.assertEqual(['plan', 'verify-before'], [call[0] for call in self.fixture.calls])
        self.assertEqual('existing_runtime_verified', json.loads((self.attempts()[0] / 'result.json').read_text())['method'])

    def test_installed_but_stopped_runtime_is_prepared_after_confirmation(self):
        self.fixture.installed = {name: 'fixture-version' for name in self.fixture.packages}
        self.assertTrue(self.prepare())
        self.confirm.assert_called_once()
        self.assertEqual(['plan', 'verify-before', 'apply', 'verify-after'], [call[0] for call in self.fixture.calls])

    def test_stale_apply_failure_preserves_evidence_and_retry_uses_fresh_plan(self):
        self.fixture.stale = True
        with self.assertRaisesRegex(setup_host.SetupHostError, 'Private evidence'):
            self.prepare()
        previous = self.attempts()[0]
        before = {p.name: p.read_bytes() for p in previous.iterdir()}
        self.assertEqual('failed', json.loads((previous / 'result.json').read_text())['status'])
        self.fixture.stale = False
        self.fixture.revision += 1
        self.assertTrue(self.prepare())
        self.assertEqual(2, len(self.attempts()))
        self.assertEqual(before, {p.name: p.read_bytes() for p in previous.iterdir()})
        plans = [json.loads((p / 'plan.json').read_text()) for p in self.attempts()]
        self.assertNotEqual(plans[0]['review_hash'], plans[1]['review_hash'])

    def test_failed_or_unconfirmed_bootstrap_never_returns_true(self):
        self.fixture.false_ready = True
        with self.assertRaisesRegex(setup_host.SetupHostError, 'did not confirm'):
            self.prepare()
        self.assertEqual(['plan', 'apply'], [call[0] for call in self.fixture.calls])

    def test_core_refusal_does_not_echo_private_payload(self):
        self.fixture.fail_plan = True
        with self.assertRaises(setup_host.SetupHostError) as captured:
            self.prepare()
        self.assertNotIn('synthetic private vendor payload', str(captured.exception))
        self.assertNotIn('synthetic private vendor payload', '\n'.join(self.messages))
        self.confirm.assert_not_called()

    def test_nonroot_or_noninteractive_public_entry_refuses_before_execution(self):
        with mock.patch.object(setup_host.platform, 'system', return_value='Linux'), \
             mock.patch.object(setup_host.os, 'geteuid', return_value=1000, create=True), \
             mock.patch.object(setup_host, '_prepare_host') as internal:
            with self.assertRaisesRegex(setup_host.SetupHostError, 'as root'):
                setup_host.prepare_host(self.repo)
        internal.assert_not_called()
        with mock.patch.object(setup_host.platform, 'system', return_value='Linux'), \
             mock.patch.object(setup_host.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(setup_host.sys.stdin, 'isatty', return_value=False), \
             mock.patch.object(setup_host, '_prepare_host') as internal:
            with self.assertRaisesRegex(setup_host.SetupHostError, 'interactive terminal'):
                setup_host.prepare_host(self.repo)
        internal.assert_not_called()

    def test_typo_reprompts_instead_of_accepting(self):
        with mock.patch('builtins.input', side_effect=['maybe', 'YES']), mock.patch('builtins.print'):
            self.assertTrue(setup_host._confirm('Prepare? '))
        with mock.patch('builtins.input', side_effect=EOFError):
            self.assertFalse(setup_host._confirm('Prepare? '))

    def test_real_child_output_stays_private_while_progress_is_visible(self):
        attempt = self.root / 'process-attempt'; attempt.mkdir()
        code = 'import sys,time; print("synthetic-private-stdout"); sys.stderr.write("synthetic-private-stderr"); time.sleep(.1)'
        result = setup_host._run_command([sys.executable, '-c', code], attempt, 'fixture', self.messages.append, heartbeat=.02)
        self.assertEqual(0, result.returncode)
        self.assertIn('synthetic-private-stdout', result.stdout)
        self.assertIn('synthetic-private-stderr', result.stderr)
        self.assertTrue(any('still running' in message for message in self.messages))
        self.assertFalse(any('synthetic-private' in message for message in self.messages))
        self.assertEqual(result.stdout, (attempt / 'fixture.stdout').read_text())
        if os.name == 'posix':
            self.assertEqual((attempt / 'fixture.stderr').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
