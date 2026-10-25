"""Guided workflow tests retain the actual reviewed engine boundaries."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from samurai_stack import engine, setup
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, digest, write_json


class GuidedSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout'; self.repo.mkdir()
        self.messages = []
        self.read = mock.Mock(return_value='no')
        self.ui = setup.GuidedSetup(self.repo, self.root / 'site.json', input_fn=self.read, output_fn=self.messages.append)
        self.c = default_config('192.168.50.20')
        self.model = {'services': {'jellyfin': {'image': 'synthetic-image'}}}
        self.compose = self.root / 'compose.json'; self.compose.write_text(json.dumps(self.model))
        self.document = {'host': {'host_id': 'synthetic-host'}, 'stage': 2, 'actions': ['controlled fixture']}
        self.document['review_hash'] = digest(self.document)

    def install_fixture(self, stage=2, guarded=True):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ('preflight', 'check_container_ownership', 'service_health'):
            stack.enter_context(mock.patch.object(engine, name))
        stack.enter_context(mock.patch.object(engine, 'compose_path', return_value=self.compose))
        stack.enter_context(mock.patch.object(engine, 'recipe', return_value=self.model))
        stack.enter_context(mock.patch.object(self.ui, 'guarded_runtime', return_value=guarded))
        planned = stack.enter_context(mock.patch.object(engine, 'plan', return_value=self.document))
        return planned

    def test_healthy_guarded_install_resumes_without_apply_or_confirmation(self):
        planned = self.install_fixture()
        with mock.patch.object(self.ui, 'invoke') as invoke:
            self.assertTrue(self.ui.install(self.c, 2))
        planned.assert_not_called(); invoke.assert_not_called(); self.read.assert_not_called()

    def test_healthy_containers_without_current_guard_require_reviewed_apply(self):
        planned = self.install_fixture(guarded=False)
        self.read.return_value = 'yes'

        def real_review(arguments, activity):
            path = Path(arguments[arguments.index('--plan') + 1])
            accepted = arguments[arguments.index('--accept-plan') + 1]
            reviewed = json.loads(path.read_text())
            engine.verify_plan(self.c, 2, reviewed, accepted)
            return 0, {'status': 'started'}

        with mock.patch.object(self.ui, 'invoke', side_effect=real_review) as invoke:
            self.assertTrue(self.ui.install(self.c, 2))
        invoke.assert_called_once(); self.read.assert_called_once()
        self.assertEqual(2, planned.call_count)  # plan generation plus actual engine verification
        self.assertNotIn(self.document['review_hash'], '\n'.join(self.messages))

    def test_guarded_resume_requires_current_owned_unit_config_and_live_service_state(self):
        c = dict(self.c, install_root=str(self.root / 'install'))
        root = Path(c['install_root']); root.mkdir()
        stage = root / 'stages' / 'homelab-2'; stage.mkdir(parents=True)
        saved = stage / 'site.json'; saved.write_text(json.dumps(c))
        guard = root / 'runtime_guard.py'
        source = Path(engine.__file__).with_name('runtime_guard.py').read_bytes()
        guard.write_bytes(source)
        units = self.root / 'units'; units.mkdir()
        unit = units / 'samurai-homelab-2.service'
        command = ' '.join(['/usr/bin/docker', '--context', 'default', *engine.compose_args(c, 2)])
        text = '# Managed by Samurai Stack: ' + c['deployment_id'] + '\n'
        text += f'ExecStartPre=/usr/bin/python3 {guard} {saved} 2\n'
        text += 'ExecStart=' + command + ' up --no-color --abort-on-container-failure --pull never\n'
        unit.write_text(text)

        def read_saved(path):
            return json.loads(Path(path).read_text())

        def active(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, 'no\n' if 'show' in argv else '', '')

        with mock.patch.object(setup, 'UNIT_ROOT', units), mock.patch.object(setup, 'load_config', side_effect=read_saved), \
             mock.patch.object(engine, 'run', side_effect=active) as live:
            self.assertTrue(self.ui.guarded_runtime(c, 2))
            self.assertEqual(3, live.call_count)
            for cause in ('wrong-config', 'foreign-unit', 'legacy-global-config', 'modified-guard', 'reload-required', 'inactive'):
                with self.subTest(cause=cause):
                    saved.write_text(json.dumps(c)); guard.write_bytes(source); unit.write_text(text)
                    if cause == 'wrong-config':
                        saved.write_text(json.dumps(dict(c, bind_address='192.168.50.21')))
                    elif cause == 'foreign-unit':
                        unit.write_text(text.replace(c['deployment_id'], 'foreign-fixture'))
                    elif cause == 'legacy-global-config':
                        unit.write_text(text.replace(str(saved), str(root / 'site.json')))
                    elif cause == 'modified-guard':
                        guard.write_bytes(b'changed synthetic guard')
                    if cause == 'reload-required':
                        with mock.patch.object(engine, 'run', return_value=subprocess.CompletedProcess([], 0, 'yes\n', '')):
                            self.assertFalse(self.ui.guarded_runtime(c, 2))
                    elif cause == 'inactive':
                        with mock.patch.object(engine, 'run', side_effect=SafetyError('inactive fixture')):
                            self.assertFalse(self.ui.guarded_runtime(c, 2))
                    else:
                        self.assertFalse(self.ui.guarded_runtime(c, 2))

    def test_declined_apply_retains_private_plan_without_running_child(self):
        self.install_fixture(guarded=False)
        with mock.patch.object(self.ui, 'invoke') as invoke:
            self.assertFalse(self.ui.install(self.c, 2))
        invoke.assert_not_called()
        plans = list(self.ui.state_root.glob('review-*/plan.json'))
        self.assertEqual(1, len(plans))
        self.assertEqual(self.document, json.loads(plans[0].read_text()))
        if os.name == 'posix':
            self.assertEqual(plans[0].stat().st_mode & 0o777, 0o600)

    def test_guided_summary_cannot_bypass_actual_core_tamper_rejection(self):
        self.install_fixture(guarded=False)
        self.read.return_value = 'yes'

        def corrupt_before_core(arguments, activity):
            path = Path(arguments[arguments.index('--plan') + 1])
            value = json.loads(path.read_text()); value['actions'].append('unreviewed fixture mutation')
            path.write_text(json.dumps(value))
            accepted = arguments[arguments.index('--accept-plan') + 1]
            engine.verify_plan(self.c, 2, value, accepted)
            self.fail('Core accepted a modified plan')

        with mock.patch.object(self.ui, 'invoke', side_effect=corrupt_before_core), self.assertRaisesRegex(SafetyError, 'hash'):
            self.ui.install(self.c, 2)

    def test_manual_pending_can_finish_only_after_actual_verify(self):
        pending = {'status': 'pending', 'pending': [{'name': 'management:enrollment', 'action': 'Finish enrollment'}]}
        for stage in (4, 6):
            with self.subTest(stage=stage), mock.patch.object(self.ui, 'invoke', side_effect=[(2, pending), (0, {'status': 'verified_with_manual_checks'})]) as invoke:
                self.assertTrue(self.ui.wire(stage))
                self.assertEqual(['configure', 'verify'], [call.args[0][0] for call in invoke.call_args_list])
        self.read.assert_not_called()

    def test_manual_pending_does_not_override_failed_verification(self):
        pending = {'status': 'pending', 'pending': [{'name': 'management:enrollment', 'action': 'Finish enrollment'}]}
        with mock.patch.object(self.ui, 'invoke', side_effect=[(2, pending), (1, {})]):
            self.assertFalse(self.ui.wire(4))
        self.read.assert_called_once()

    def test_media_pending_never_becomes_verified_from_container_state(self):
        pending = {'status': 'pending', 'pending': [{'name': 'seerr:jellyfin', 'action': 'Finish Seerr onboarding'}]}
        with mock.patch.object(self.ui, 'invoke', return_value=(2, pending)) as invoke:
            self.assertFalse(self.ui.wire(2))
        invoke.assert_called_once()
        self.assertTrue(any('Finish Seerr onboarding' in item for item in self.messages))

    def test_paused_step_is_saved_and_successful_retry_updates_only_its_status(self):
        self.ui.mark(2, 'verified')
        with mock.patch.object(self.ui, 'prepare', return_value=False):
            self.assertFalse(self.ui.step(1))
        self.assertEqual('paused', self.ui.state()['1']['status'])
        self.assertEqual('verified', self.ui.state()['2']['status'])
        with mock.patch.object(self.ui, 'prepare', return_value=True):
            self.assertTrue(self.ui.step(1))
        self.assertEqual('verified', self.ui.state()['1']['status'])

    def test_distinct_site_files_never_share_verified_progress(self):
        self.ui.mark(1, 'verified')
        other = setup.GuidedSetup(self.repo, self.root / 'other-site.json', input_fn=self.read, output_fn=self.messages.append)
        self.assertNotEqual(self.ui.state_root, other.state_root)
        self.assertEqual({}, other.state())
        other.mark(2, 'paused')
        self.assertEqual({'1'}, set(self.ui.state()))
        self.assertEqual({'2'}, set(other.state()))

    def test_interrupting_previously_verified_step_persists_paused_state(self):
        self.ui.mark(1, 'verified')
        with mock.patch.object(self.ui, 'prepare', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.ui.step(1)
        self.assertEqual('paused', self.ui.state()['1']['status'])

    def test_real_child_vendor_output_is_private_on_failure(self):
        self.repo.joinpath('stack').write_text('import sys\nprint("{}")\nsys.stderr.write("synthetic-private-vendor-error")\nraise SystemExit(1)\n')
        code, value = self.ui.invoke(['verify', '2'], 'Checking test workflow...')
        self.assertEqual((1, {}), (code, value))
        self.assertFalse(any('synthetic-private-vendor-error' in item for item in self.messages))
        logs = list(self.ui.state_root.glob('attempt-*/diagnostic.txt'))
        self.assertEqual(1, len(logs))
        self.assertEqual('synthetic-private-vendor-error', logs[0].read_text())
        if os.name == 'posix':
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)

    def test_interrupt_signals_owned_child_group_once_and_waits_for_cleanup(self):
        child = mock.Mock(pid=7654321)
        child.wait.side_effect = [KeyboardInterrupt, subprocess.TimeoutExpired('synthetic child', 30), 0]
        child.poll.return_value = None
        with mock.patch.object(setup.subprocess, 'Popen', return_value=child) as launch, \
             mock.patch.object(setup.os, 'killpg', create=True) as interrupt, self.assertRaises(KeyboardInterrupt):
            self.ui.invoke(['apply', '2'], 'Applying synthetic fixture')
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        interrupt.assert_called_once_with(child.pid, signal.SIGINT)
        child.terminate.assert_not_called(); child.kill.assert_not_called()
        self.assertEqual(3, child.wait.call_count)
        self.assertTrue(any('finish safely' in item for item in self.messages))
        self.assertEqual(1, len(list(self.ui.state_root.glob('attempt-*/diagnostic.txt'))))

    def test_new_mullvad_selection_reprompts_invalid_address_and_resumes_without_questions(self):
        write_json(self.ui.config_path, self.c)
        self.read.side_effect = ['2', 'not-an-address', '10.64.0.2/32']
        with mock.patch.object(engine, 'compose_path', return_value=self.root / 'absent-compose'):
            selected = self.ui.vpn_profile(self.c)
            self.assertEqual('mullvad', selected['vpn']['provider'])
            self.assertEqual('10.64.0.2/32', selected['vpn']['wireguard_addresses'])
            self.assertFalse(selected['vpn']['port_forwarding'])
            self.read.side_effect = AssertionError('saved VPN should not ask again')
            self.assertEqual(selected, self.ui.vpn_profile(selected))
        backups = list(self.ui.state_root.glob('site-before-vpn-*.json'))
        self.assertEqual(1, len(backups))
        self.assertEqual(self.c, json.loads(backups[0].read_text()))

    def test_custom_openvpn_correction_imports_new_private_profile_and_updates_requested_credentials(self):
        self.c['vpn'].update(provider='custom', type='openvpn')
        old = self.root / 'custom.conf'; old.write_text('synthetic old profile\n'); old.chmod(0o600)
        new = self.root / 'new.conf'; new.write_text('synthetic corrected profile\n'); new.chmod(0o600)
        self.read.return_value = str(new)
        with mock.patch.object(self.ui, 'config', return_value=self.c), \
             mock.patch.object(self.ui, 'vpn_profile', return_value=self.c), \
             mock.patch.object(engine, 'compose_path', return_value=self.compose), \
             mock.patch.object(engine, 'service_health', side_effect=SafetyError('VPN not healthy')), \
             mock.patch.object(self.ui, 'yes', return_value=True), \
             mock.patch.object(engine, 'secret_path', return_value=old), \
             mock.patch.object(engine, 'read_secret', return_value='synthetic old profile\n'), \
             mock.patch.object(engine, 'initialize_install'), \
             mock.patch.object(self.ui, 'secret', return_value=True) as credentials, \
             mock.patch.object(engine, 'run') as restart, \
             mock.patch.object(self.ui, 'install', return_value=False):
            self.assertFalse(self.ui.torrents())
        self.assertEqual('synthetic corrected profile\n', old.read_text())
        self.assertEqual('synthetic corrected profile\n', new.read_text())
        self.assertEqual('synthetic old profile\n', next(self.ui.state_root.glob('credential-before-*')).read_text())
        self.assertEqual(2, credentials.call_count)
        self.assertTrue(all(call.kwargs['replace'] for call in credentials.call_args_list))
        self.assertEqual(['systemctl', 'restart', 'samurai-homelab-3.service'], restart.call_args.args[0])

    def test_secret_is_not_saved_until_authenticated_validation_passes(self):
        target = self.root / 'new-key'
        self.ui.secret_fn = mock.Mock(return_value='synthetic-invalid-key')
        with mock.patch.object(engine, 'secret_resolver', return_value=mock.Mock(side_effect=SafetyError('not enrolled'))), \
             mock.patch.object(engine, 'secret_path', return_value=target), \
             mock.patch.object(self.ui, '_valid_secret', return_value=False), \
             mock.patch.object(engine, 'initialize_install') as initialize:
            self.assertFalse(self.ui.secret(self.c, 'jellyfin_api_key', 'Enter fixture key'))
        self.assertFalse(target.exists()); initialize.assert_not_called()
        self.assertFalse(any('synthetic-invalid-key' in item for item in self.messages))

    def test_replacing_invalid_saved_key_requires_consent_and_keeps_exact_private_backup(self):
        target = self.root / 'saved-key'; target.write_text('synthetic-old-key\n')
        self.ui.secret_fn = mock.Mock(return_value='synthetic-new-key')
        self.read.return_value = 'yes'
        with mock.patch.object(engine, 'secret_resolver', return_value=lambda name: 'synthetic-old-key'), \
             mock.patch.object(engine, 'secret_path', return_value=target), \
             mock.patch.object(engine, 'read_secret', return_value='synthetic-old-key\n'), \
             mock.patch.object(self.ui, '_valid_secret', side_effect=[False, True]), \
             mock.patch.object(engine, 'initialize_install'):
            self.assertTrue(self.ui.secret(self.c, 'seerr_api_key', 'Enter fixture key'))
        backups = list(self.ui.state_root.glob('credential-before-*'))
        self.assertEqual(1, len(backups))
        self.assertEqual(b'synthetic-old-key\n', backups[0].read_bytes())
        self.assertEqual('synthetic-new-key\n', target.read_text())
        self.assertFalse(any('synthetic-old-key' in item or 'synthetic-new-key' in item for item in self.messages))


if __name__ == '__main__':
    unittest.main()
