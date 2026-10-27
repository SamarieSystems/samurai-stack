"""Companion installation, ownership, baseline and engine integration tests."""
from contextlib import ExitStack, nullcontext
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack import cli, engine, integrations, monitor_search, monitor_setup
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest


class MonitorInstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / 'apps'; self.app.mkdir(mode=0o700)
        for folder in ('state', 'backups'):
            (self.app / folder).mkdir(mode=0o700)
        self.units = self.root / 'units'; self.units.mkdir()
        self.c = default_config()
        self.c['install_root'] = str(self.app)
        self.policy = {'schema_version': 1, 'kind': 'sonarr-monitor-search', 'enabled': True,
                       'config': {'install_root': str(self.app)}, 'target': {'fixture': True}}
        self.calls, self.baselines = [], 0
        self.live_override = None
        self.baseline_error = False
        self.enterContext(patch.object(monitor_setup, '_guard'))
        self.enterContext(patch.object(monitor_setup, '_owner'))  # Unit fixtures may be owned by an unprivileged CI user.
        self.enterContext(patch.object(engine, 'host_identity', return_value='fixture-host'))
        self.builder = self.enterContext(patch.object(monitor_search, 'build_policy', side_effect=lambda *a, **k: copy.deepcopy(self.policy)))
        self.enterContext(patch.object(monitor_search, 'load_state', side_effect=self.read_state))

    def read_state(self, path, policy):
        value = json.loads(Path(path).read_text())
        monitor_search.validate_state(value, monitor_search.digest(policy))
        return value

    def runner(self, argv, **kwargs):
        argv = list(map(str, argv)); self.calls.append((argv, kwargs))
        if argv[0] == '/usr/bin/python3':
            self.assertEqual(argv[-1], '--baseline')
            self.assertFalse(monitor_setup.paths(self.c)['state'].exists())
            self.baselines += 1
            if self.baseline_error:
                raise SafetyError('Baseline fixture failed; no search was submitted')
            value = monitor_search.new_state(monitor_search.digest(self.policy), 'fixture-boot', 1000.0, {})
            target = monitor_setup.paths(self.c)['state']
            target.write_text(json.dumps(value)); target.chmod(0o600)
        if argv[:2] == ['systemctl', 'show']:
            path = self.units / argv[2]
            values = {'LoadState': 'loaded' if path.exists() else 'not-found',
                      'FragmentPath': str(path) if path.exists() else '',
                      'DropInPaths': '', 'Transient': 'no', 'NeedDaemonReload': 'no',
                      'Result': 'success', 'ExecMainStatus': '0', 'ExecMainStartTimestampMonotonic': '100'}
            if self.live_override:
                values.update(self.live_override)
            return subprocess.CompletedProcess(argv, 0, '\n'.join(k + '=' + v for k, v in values.items()), '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def install(self):
        return monitor_setup.install(self.c, runner=self.runner, unit_root=self.units)

    def verify(self):
        return monitor_setup.verify(self.c, runner=self.runner, unit_root=self.units)

    def test_first_activation_baselines_before_timer_enable_and_survives_checkout_removal(self):
        report = self.install()
        self.assertTrue(report['ok']); self.assertTrue(report['first_activation_baseline'])
        self.assertEqual(1, self.baselines)
        baseline = next(i for i, (argv, _) in enumerate(self.calls) if argv[0] == '/usr/bin/python3')
        enabled = next(i for i, (argv, _) in enumerate(self.calls) if argv[:3] == ['systemctl', 'enable', '--now'])
        unit_poll = next(i for i, (argv, _) in enumerate(self.calls) if argv[:2] == ['systemctl', 'start'])
        self.assertLess(baseline, enabled)
        self.assertLess(baseline, unit_poll); self.assertLess(unit_poll, enabled)
        documents = monitor_setup.units(self.c, self.units)
        source = monitor_setup.paths(self.c)['script']
        self.assertEqual(source.read_text(), Path(monitor_search.__file__).read_text())
        for path, body in documents.items():
            self.assertEqual(path.read_text(), body)
            self.assertNotIn(str(Path(__file__).parents[1]), body)
        service = next(body for path, body in documents.items() if path.suffix == '.service')
        timer = next(body for path, body in documents.items() if path.suffix == '.timer')
        self.assertIn('OnUnitInactiveSec=30s\n', timer)
        self.assertIn('WantedBy=timers.target\n', timer)
        self.assertIn('TimeoutStartSec=120\n', service)
        self.assertIn('ProtectSystem=full\n', service)
        self.assertNotIn('ProtectSystem=strict', service)
        self.assertNotIn('Requires=docker', service)
        self.assertNotIn('Wants=samurai-', service)
        self.assertNotIn('restart', ' '.join(' '.join(argv) for argv, _ in self.calls))
        self.assertNotIn('password', service.lower())
        self.assertNotIn('api_key', service.lower())
        self.assertTrue(self.verify()['ok'])

    def test_reapply_and_verify_preserve_durable_state_and_never_repeat_baseline(self):
        self.install()
        state = monitor_setup.paths(self.c)['state']
        value = json.loads(state.read_text())
        value['generation'] = 4
        state.write_text(json.dumps(value)); state.chmod(0o600)
        before = state.read_bytes()
        self.calls.clear()
        report = self.install()
        self.assertFalse(report['first_activation_baseline'])
        self.assertEqual(0, report['changed_artifacts'])
        self.assertEqual(1, self.baselines)
        self.assertEqual(before, state.read_bytes())
        self.verify()
        self.assertEqual(before, state.read_bytes())
        self.assertFalse(any(argv[0] == '/usr/bin/python3' for argv, _ in self.calls))
        self.assertEqual([], list((self.app / 'backups').iterdir()))

    def test_foreign_unit_is_retained_before_runtime_or_policy_write(self):
        target = self.units / 'samurai-homelab-sonarr-monitor-search.service'
        target.write_text('[Service]\nExecStart=/usr/bin/true\n'); target.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, 'unowned or locally edited'):
            self.install()
        self.assertEqual(target.read_text(), '[Service]\nExecStart=/usr/bin/true\n')
        for key in ('script', 'policy', 'state', 'receipt'):
            self.assertFalse(monitor_setup.paths(self.c)[key].exists())
        self.assertFalse(self.calls)

    def test_locally_modified_owned_source_and_unit_are_not_overwritten(self):
        for kind in ('script', 'unit'):
            with self.subTest(kind=kind):
                self.install()
                target = (monitor_setup.paths(self.c)['script'] if kind == 'script'
                          else self.units / 'samurai-homelab-sonarr-monitor-search.service')
                old = target.read_text(); changed = old + '# local change\n'
                target.write_text(changed)
                before = monitor_setup.paths(self.c)['state'].read_bytes()
                self.calls.clear()
                with self.assertRaisesRegex(SafetyError, 'locally edited'):
                    self.install()
                self.assertEqual(changed, target.read_text())
                self.assertEqual(before, monitor_setup.paths(self.c)['state'].read_bytes())
                self.assertFalse(self.calls)
                target.write_text(old)

    def test_owned_source_upgrade_backs_up_old_source_and_retains_event_state(self):
        self.install()
        old = monitor_setup.paths(self.c)['script'].read_text()
        before = monitor_setup.paths(self.c)['state'].read_bytes()
        original_documents = monitor_setup._documents
        def next_documents(c, policy, unit_root):
            result = original_documents(c, policy, unit_root)
            result[monitor_setup.paths(c)['script']] += '# synthetic next released source\n'
            return result
        with patch.object(monitor_setup, '_documents', side_effect=next_documents):
            report = self.install()
        self.assertEqual(old, (Path(report['private_backup']) / 'sonarr_monitor_search.py').read_text())
        self.assertEqual(before, monitor_setup.paths(self.c)['state'].read_bytes())
        self.assertEqual(1, self.baselines)

    def test_failed_first_baseline_does_not_enable_timer_and_retry_recovers(self):
        self.baseline_error = True
        with self.assertRaisesRegex(SafetyError, 'Baseline fixture failed'):
            self.install()
        self.assertFalse(any(argv[:2] == ['systemctl', 'enable'] for argv, _ in self.calls))
        self.assertEqual('installing', json.loads(monitor_setup.paths(self.c)['receipt'].read_text())['state'])
        self.baseline_error = False
        self.assertTrue(self.install()['ok'])
        self.assertEqual(2, self.baselines)

    def test_malformed_existing_state_is_never_baselined_or_reset(self):
        self.install()
        state = monitor_setup.paths(self.c)['state']; state.write_text('{}')
        before = {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()}
        self.calls.clear()
        with self.assertRaisesRegex(SafetyError, 'event state'):
            self.install()
        self.assertEqual('{}', state.read_text())
        self.assertEqual(before, {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()})
        self.assertFalse(any(argv[0] == '/usr/bin/python3' for argv, _ in self.calls))

    def test_changed_policy_binding_stops_before_replacing_owned_runtime(self):
        self.install()
        before = {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()}
        self.policy['target']['fixture'] = 'different-target'
        self.calls.clear()
        with self.assertRaisesRegex(SafetyError, 'event state'):
            self.install()
        self.assertEqual(before, {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()})
        self.assertFalse(self.calls)

    def test_foreign_live_fragment_or_dropin_blocks_before_any_managed_write(self):
        for override in ({'LoadState': 'loaded', 'FragmentPath': '/run/systemd/transient/unrelated.service'},
                         {'LoadState': 'loaded', 'FragmentPath': str(self.units / 'samurai-homelab-sonarr-monitor-search.service'),
                          'DropInPaths': '/etc/systemd/system/custom.conf'}):
            with self.subTest(override=override):
                self.live_override = override
                with self.assertRaisesRegex(SafetyError, 'unexpected live fragment or override'):
                    self.install()
                self.assertFalse(monitor_setup.paths(self.c)['script'].exists())
                self.assertFalse(monitor_setup.paths(self.c)['receipt'].exists())

    def test_verify_is_readonly_and_requires_enabled_active_timer(self):
        self.install(); self.calls.clear()
        before = {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()}
        self.assertTrue(self.verify()['ok'])
        self.assertTrue(any(argv[1] == 'is-enabled' for argv, _ in self.calls))
        self.assertTrue(any(argv[1] == 'is-active' for argv, _ in self.calls))
        self.assertTrue(all(argv[0] == 'systemctl' and argv[1] in {'show', 'is-enabled', 'is-active'} for argv, _ in self.calls))
        self.assertEqual(before, {p: p.read_bytes() for p in self.app.rglob('*') if p.is_file()})
        def stopped(argv, **kwargs):
            if argv[:2] == ['systemctl', 'is-active']:
                raise SafetyError('timer is stopped')
            return self.runner(argv, **kwargs)
        with self.assertRaisesRegex(SafetyError, 'timer is stopped'):
            monitor_setup.verify(self.c, runner=stopped, unit_root=self.units)

    def test_baseline_success_does_not_hide_failed_systemd_sandbox_poll(self):
        def failed_unit(argv, **kwargs):
            if argv[:2] == ['systemctl', 'start']:
                raise SafetyError('owned oneshot failed inside its sandbox')
            return self.runner(argv, **kwargs)
        with self.assertRaisesRegex(SafetyError, 'sandbox'):
            monitor_setup.install(self.c, runner=failed_unit, unit_root=self.units)
        self.assertEqual(1, self.baselines)
        self.assertFalse(any(argv[:2] == ['systemctl', 'enable'] for argv, _ in self.calls))

    def test_verify_refuses_failed_or_never_run_service_without_dispatching(self):
        self.install()
        for override in ({'Result': 'exit-code', 'ExecMainStatus': '1'},
                         {'ExecMainStartTimestampMonotonic': '0'}):
            with self.subTest(override=override):
                self.live_override = override; self.calls.clear()
                with self.assertRaisesRegex(SafetyError, 'successful guarded poll'):
                    self.verify()
                self.assertFalse(any(argv[:2] == ['systemctl', 'start'] for argv, _ in self.calls))

    def test_unowned_or_host_changed_receipt_is_not_adopted(self):
        self.install()
        receipt = monitor_setup.paths(self.c)['receipt']
        value = json.loads(receipt.read_text()); value['host_id'] = 'other-host'
        receipt.write_text(json.dumps(value)); receipt.chmod(0o600)
        self.calls.clear()
        with self.assertRaisesRegex(SafetyError, 'another installation or host'):
            self.install()
        self.assertFalse(self.calls)

    @unittest.skipUnless(os.name == 'posix', 'Symlinks require Linux fixture permissions')
    def test_symlink_unit_cannot_redirect_an_installation_write(self):
        unrelated = self.root / 'unrelated'; unrelated.write_text('keep\n')
        (self.units / 'samurai-homelab-sonarr-monitor-search.service').symlink_to(unrelated)
        with self.assertRaisesRegex(SafetyError, 'symbolic link'):
            self.install()
        self.assertEqual('keep\n', unrelated.read_text())


class EngineMonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.c = default_config()
        self.stack = self.enterContext(ExitStack())
        for name in ('linux_only', 'preflight', 'service_health', 'check_container_ownership', 'write_json'):
            self.stack.enter_context(patch.object(engine, name))
        self.stack.enter_context(patch.object(engine, 'deployment_lock', return_value=nullcontext()))
        self.stack.enter_context(patch.object(engine, 'secret_resolver', return_value=lambda _: 'fixture-key'))
        self.stack.enter_context(patch.object(engine, 'stage_path', return_value=Path('/fixture/stage')))

    def test_successful_media_wiring_installs_companion_without_another_app_apply(self):
        with patch.object(integrations, 'configure_stage', return_value={'ok': True}) as wiring, \
             patch.object(monitor_setup, 'install', return_value={'ok': True}) as install, \
             patch.object(engine, 'install_unit') as application_unit:
            result = engine.configure(self.c, 2)
        install.assert_called_once_with(self.c)
        application_unit.assert_not_called()
        self.assertTrue(result['monitor_search']['ok'])
        self.assertEqual(2, wiring.call_args.args[1])

    def test_pending_media_wiring_does_not_enable_monitor_search(self):
        for report in ({'ok': False, 'status': 'pending'}, {'ok': False, 'status': 'failed'}):
            with self.subTest(report=report), patch.object(integrations, 'configure_stage', return_value=report), \
                 patch.object(monitor_setup, 'install') as install:
                engine.configure(self.c, 2)
            install.assert_not_called()

    def test_media_verify_proves_companion_without_installing_or_searching(self):
        with patch.object(integrations, 'verify_stage', return_value={'ok': True, 'status': 'verified'}), \
             patch.object(monitor_setup, 'verify', return_value={'ok': True}) as verify, \
             patch.object(monitor_setup, 'install') as install:
            result = engine.verify(self.c, 2)
        verify.assert_called_once_with(self.c, runner=engine.run)
        install.assert_not_called()
        self.assertTrue(result['monitor_search']['ok'])


class MonitorCLITests(unittest.TestCase):
    def test_install_dispatches_only_companion_without_container_deploy(self):
        c = default_config()
        with patch.object(cli, 'load_config', return_value=c), \
             patch.object(monitor_setup, 'install', return_value={'ok': True}) as install, \
             patch.object(engine, 'apply') as deploy, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(['monitor-search', 'install']))
        install.assert_called_once_with(c); deploy.assert_not_called()

    def test_verify_does_not_install_or_dispatch_pending_search(self):
        c = default_config()
        with patch.object(cli, 'load_config', return_value=c), \
             patch.object(monitor_setup, 'verify', return_value={'ok': True}) as verify, \
             patch.object(monitor_setup, 'install') as install, \
             patch.object(monitor_search, 'run_policy') as worker, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(['monitor-search', 'verify']))
        verify.assert_called_once_with(c); install.assert_not_called(); worker.assert_not_called()

    def test_blocked_install_has_nonzero_exit_and_no_traceback(self):
        with patch.object(cli, 'load_config', return_value=default_config()), \
             patch.object(monitor_setup, 'install', side_effect=SafetyError('Existing unit differs; it was retained')), \
             redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
            self.assertEqual(1, cli.main(['monitor-search', 'install']))
        self.assertIn('Existing unit differs', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
