"""Guided workflow tests retain the actual reviewed engine boundaries."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import hashlib
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
        self.ui = setup.GuidedSetup(self.repo, self.root / 'site.json', input_fn=self.read, output_fn=self.messages.append,
                                    state_base=self.root / 'host-state', registry_root=self.root / 'registry')
        self.remember = self.enterContext(mock.patch.object(self.ui, '_remember_configuration',
                                                          return_value=self.root / 'registry/site.json'))
        self.c = default_config('192.168.50.20')
        if os.name == 'posix':
            # Unit tests must never inspect a real deployment on a used host.
            self.c['install_root'] = str(self.root / 'apps')
            self.c['storage']['data_root'] = str(self.root / 'media')
            self.c['storage']['expected_mount']['target'] = str(self.root)
        self.model = {'services': {'jellyfin': {'image': 'synthetic-image'}}}
        self.compose = self.root / 'compose.json'; self.compose.write_text(json.dumps(self.model))
        self.document = {'host': {'host_id': 'synthetic-host'}, 'stage': 2, 'actions': ['controlled fixture']}
        self.document['review_hash'] = digest(self.document)

    def install_fixture(self, stage=2, guarded=True):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ('preflight', 'check_docker', 'check_container_ownership', 'service_health'):
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

    def test_hardlink_maintenance_verifies_without_deployment_or_prompt_when_enabled(self):
        with mock.patch.object(self.ui, 'invoke', return_value=(0, {'ok': True})) as invoke, \
             mock.patch.object(self.ui, 'install') as install:
            self.assertTrue(self.ui.hardlinks())
        self.assertEqual(['hardlinks'], invoke.call_args.args[0])
        install.assert_not_called(); self.read.assert_not_called()

    def test_hardlink_maintenance_repairs_only_after_confirmation_then_verifies(self):
        self.read.return_value = 'yes'
        replies = [(2, {'ok': False}), (0, {'ok': True}), (0, {'ok': True})]
        with mock.patch.object(self.ui, 'invoke', side_effect=replies) as invoke, \
             mock.patch.object(self.ui, 'install') as install:
            self.assertTrue(self.ui.hardlinks())
        self.assertEqual([['hardlinks'], ['hardlinks', '--apply'], ['hardlinks']], [call.args[0] for call in invoke.call_args_list])
        install.assert_not_called()

    def test_hardlink_maintenance_decline_never_writes(self):
        with mock.patch.object(self.ui, 'invoke', return_value=(2, {'ok': False})) as invoke:
            self.assertFalse(self.ui.hardlinks())
        invoke.assert_called_once()

    def test_profile_decline_never_applies_or_reviews_global_limits(self):
        document = {'review_hash': 'a' * 64, 'services': {}}
        with mock.patch.object(self.ui, 'invoke', return_value=(0, document)) as invoke:
            self.assertTrue(self.ui.media_profiles())
        self.assertEqual(1, invoke.call_count)
        self.assertEqual(['profiles', 'plan'], invoke.call_args.args[0][:2])
        self.assertNotIn('--global-sizes', invoke.call_args.args[0])

    def test_restored_accounts_decline_never_updates_receipt(self):
        document = {'review_hash': 'b' * 64, 'changes': [{'app': 'sonarr'}]}
        with mock.patch.object(self.ui, 'invoke', return_value=(0, document)) as invoke:
            self.assertFalse(self.ui.restored_accounts())
        self.assertEqual(1, invoke.call_count)
        self.assertEqual(['restore-accounts', 'plan'], invoke.call_args.args[0][:2])

    def test_restored_accounts_apply_uses_reviewed_plan_and_independent_verify(self):
        document = {'review_hash': 'b' * 64, 'changes': [{'app': 'sonarr'}]}
        self.read.return_value = 'yes'
        with mock.patch.object(self.ui, 'invoke', side_effect=[(0, document), (0, {'ok': True}), (0, {'ok': True})]) as invoke:
            self.assertTrue(self.ui.restored_accounts())
        self.assertEqual(['restore-accounts', 'apply'], invoke.call_args_list[1].args[0][:2])
        self.assertEqual('b' * 64, invoke.call_args_list[1].args[0][-1])
        self.assertEqual(['restore-accounts', 'verify'], invoke.call_args_list[2].args[0])

    def test_profile_apply_does_not_change_global_limits_without_second_review(self):
        document = {'review_hash': 'a' * 64, 'services': {}}
        self.read.side_effect = ['yes', 'no']
        with mock.patch.object(self.ui, 'invoke', side_effect=[(0, document), (0, {'ok': True, 'profile_name': 'fixture'})]) as invoke:
            self.assertTrue(self.ui.media_profiles())
        self.assertEqual(2, invoke.call_count)
        self.assertEqual(['profiles', 'apply'], invoke.call_args_list[1].args[0][:2])
        self.assertFalse(any('--global-sizes' in call.args[0] for call in invoke.call_args_list))

    def test_fresh_starr_profile_review_runs_before_first_seerr_connection(self):
        events = []
        self.ui._offer_media_profiles = True
        with mock.patch.object(self.ui, 'config', return_value=self.c), \
             mock.patch.object(self.ui, 'install', return_value=True), \
             mock.patch.object(self.ui, 'enroll_accounts', return_value={'sonarr', 'radarr'}), \
             mock.patch.object(self.ui, 'usenet_provider', return_value=True), \
             mock.patch.object(self.ui, 'media_naming', return_value=True), \
             mock.patch.object(self.ui, 'capacity_policy', return_value=True), \
             mock.patch.object(self.ui, 'subtitles', return_value=True), \
             mock.patch.object(self.ui, 'indexer_sources', return_value=True), \
             mock.patch.object(self.ui, 'media_profiles', side_effect=lambda: events.append('profiles') or True), \
             mock.patch.object(self.ui, 'wire', side_effect=lambda stage: events.append('wire') or True):
            self.assertTrue(self.ui.media())
        self.assertEqual(['profiles', 'wire'], events)

    def test_verified_existing_accounts_do_not_offer_initial_profile_setup(self):
        with mock.patch.object(self.ui, 'config', return_value=self.c), \
             mock.patch.object(self.ui, 'install', return_value=True), \
             mock.patch.object(self.ui, 'enroll_accounts', return_value={'sonarr', 'radarr', 'jellyfin'}), \
             mock.patch.object(self.ui, 'usenet_provider', return_value=True), \
             mock.patch.object(self.ui, 'media_naming', return_value=True), \
             mock.patch.object(self.ui, 'capacity_policy', return_value=True), \
             mock.patch.object(self.ui, 'subtitles', return_value=True), \
             mock.patch.object(self.ui, 'indexer_sources', return_value=True), \
             mock.patch.object(self.ui, 'wire', return_value=True), \
             mock.patch.object(self.ui, 'media_profiles') as policy:
            self.assertTrue(self.ui.media())
        policy.assert_not_called()

    def test_pending_account_inventory_excludes_verified_reservations(self):
        from samurai_stack import onboarding
        receipt = {'apps': {'sonarr': {'eligible': True, 'state': 'verified'},
                            'radarr': {'eligible': True, 'state': 'intent'},
                            'jellyfin': {'eligible': False, 'state': 'manual'}}}
        with mock.patch.object(onboarding, '_guard'), mock.patch.object(onboarding, '_load', return_value=receipt):
            self.assertEqual(['radarr'], onboarding.pending_apps(self.c))

    def test_legacy_account_enrollment_never_prompts_for_credentials(self):
        from samurai_stack import onboarding
        self.account_fixture([], False)
        result = {'checks': [], 'pending': [], 'errors': []}
        with mock.patch.object(onboarding, 'onboarding', return_value=result) as enroll:
            self.assertEqual(set(), self.ui.enroll_accounts(self.c))
        enroll.assert_called_once(); self.read.assert_not_called()

    def account_fixture(self, eligible, needed=True):
        from samurai_stack import onboarding
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(onboarding, 'claim_unconfigured', return_value={'claimed': [], 'preserved': [], 'errors': []}))
        stack.enter_context(mock.patch.object(onboarding, 'eligible_apps', return_value=eligible))
        stack.enter_context(mock.patch.object(onboarding, 'pending_apps', return_value=eligible))
        stack.enter_context(mock.patch.object(onboarding, 'requires_credentials', return_value=needed))

    def run_cli(self, arguments):
        """Exercise real CLI exception handling without relaxing production guards."""
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        output = Terminal()
        original_path = setup.Path
        def path(value):
            return mock.Mock(exists=lambda: False) if value == '/etc/pve' else original_path(value)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with redirect_stdout(output), redirect_stderr(output), \
             mock.patch('samurai_stack.setup_recovery.resolve_configuration', return_value=self.ui.config_path), \
             mock.patch('samurai_stack.installation_registry.draft_directory', return_value=self.root / 'host-draft'), \
             mock.patch.object(setup.sys, 'platform', 'linux'), \
             mock.patch.object(setup.sys, 'stdin', stdin), \
             mock.patch.object(setup.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(setup, 'Path', side_effect=path), \
             mock.patch.object(setup, 'GuidedSetup', return_value=self.ui):
            code = setup.main(arguments)
        return code, output.getvalue()

    def test_fresh_account_enrollment_uses_hidden_inputs_and_does_not_launch_child(self):
        from samurai_stack import onboarding
        c = dict(self.c, install_root=str(self.root / 'install'))
        self.read.side_effect = ['fixture-admin', 'fixture-viewer']
        self.account_fixture(['jellyfin'])
        self.ui.secret_fn = mock.Mock(side_effect=['fixture-admin-password', 'fixture-admin-password',
                                                   'fixture-viewer-password', 'fixture-viewer-password'])
        result = {'checks': [{'name': 'jellyfin:accounts', 'status': 'passed'}],
                  'pending': [], 'errors': []}
        with mock.patch.object(onboarding, 'eligible_apps', return_value=['jellyfin']), \
             mock.patch.object(onboarding, 'onboarding', return_value=result) as enroll, \
             mock.patch.object(self.ui, 'invoke') as invoke:
            self.assertEqual({'jellyfin'}, self.ui.enroll_accounts(c))
        supplied = enroll.call_args.args[1]
        self.assertEqual('fixture-admin', supplied['admin_username'])
        self.assertEqual('fixture-viewer-password', supplied['viewer_password'])
        self.assertNotIn('fixture-admin-password', '\n'.join(self.messages))
        self.assertNotIn('fixture-viewer-password', '\n'.join(self.messages))
        invoke.assert_not_called()

    def test_interrupted_password_input_does_not_enroll_any_accounts(self):
        from samurai_stack import onboarding
        c = dict(self.c, install_root=str(self.root / 'install'))
        self.read.side_effect = ['fixture-admin']
        self.account_fixture(['jellyfin'])
        self.ui.secret_fn = mock.Mock(return_value='')
        with mock.patch.object(onboarding, 'eligible_apps', return_value=['jellyfin']), \
             mock.patch.object(onboarding, 'onboarding') as enroll:
            self.assertIsNone(self.ui.enroll_accounts(c))
        enroll.assert_not_called()

    def test_saved_account_enrollment_resumes_without_password_prompts(self):
        from samurai_stack import onboarding
        c = dict(self.c, install_root=str(self.root / 'install'))
        self.account_fixture(['jellyfin'])
        secret = engine.secret_path(c, 'onboarding_credentials.json')
        secret.parent.mkdir(parents=True); secret.write_text('{}')
        result = {'checks': [], 'pending': [{'name': 'jellyfin:accounts', 'action': 'Resolve changed account'}], 'errors': []}
        with mock.patch.object(onboarding, 'eligible_apps', return_value=['jellyfin']), \
             mock.patch.object(onboarding, 'onboarding', return_value=result) as enroll:
            self.assertIsNone(self.ui.enroll_accounts(c))
        self.assertIsNone(enroll.call_args.args[1]); self.read.assert_not_called()

    def test_media_connects_without_browser_or_api_key_prompts(self):
        with mock.patch.object(self.ui, 'config', return_value=self.c), \
             mock.patch.object(self.ui, 'install', return_value=True), \
             mock.patch.object(self.ui, 'enroll_accounts', return_value={'jellyfin', 'seerr', 'bazarr', 'sabnzbd'}), \
             mock.patch.object(self.ui, 'usenet_provider', return_value=True), \
             mock.patch.object(self.ui, 'media_naming', return_value=True), \
             mock.patch.object(self.ui, 'capacity_policy', return_value=True), \
             mock.patch.object(self.ui, 'subtitles', return_value=True), \
             mock.patch.object(self.ui, 'indexer_sources', return_value=True), \
             mock.patch.object(self.ui, 'wire', return_value=True) as wire, \
             mock.patch.object(self.ui, 'secret') as secret:
            self.assertTrue(self.ui.media())
        wire.assert_called_once_with(2); secret.assert_not_called(); self.read.assert_not_called()
        self.assertNotIn('wizard', '\n'.join(self.messages).lower())

    def test_existing_usenet_provider_is_tested_without_credentials_or_changes(self):
        from samurai_stack import usenet
        with mock.patch.object(usenet, 'providers', return_value=[{'host': 'fixture-nntp.example.invalid'}]), \
             mock.patch.object(usenet, 'verify', return_value={'ok': True, 'results': [{'host': 'fixture-nntp.example.invalid', 'status': 'passed'}]}) as verify, \
             mock.patch.object(usenet, 'configure') as configure:
            self.assertTrue(self.ui.usenet_provider(self.c))
        verify.assert_called_once(); configure.assert_not_called(); self.read.assert_not_called()

    def test_failed_existing_usenet_provider_does_not_get_replaced(self):
        from samurai_stack import usenet
        with mock.patch.object(usenet, 'providers', return_value=[{'host': 'fixture-nntp.example.invalid'}]), \
             mock.patch.object(usenet, 'verify', return_value={'ok': False, 'results': [{'host': 'fixture-nntp.example.invalid', 'status': 'failed'}]}), \
             mock.patch.object(usenet, 'configure') as configure:
            self.assertFalse(self.ui.usenet_provider(self.c))
        configure.assert_not_called(); self.read.assert_not_called()

    def test_cli_tls_failure_pauses_and_saved_provider_retry_can_complete_without_secrets_in_output(self):
        from samurai_stack import onboarding, usenet
        c = dict(self.c, install_root=str(self.root / 'install'))
        write_json(self.ui.config_path, c)
        private = engine.secret_path(c, 'usenet_provider.json')
        private.parent.mkdir(parents=True)
        write_json(private, {'host': 'fixture-nntp.example.invalid', 'port': 563,
            'username': 'fixture-nntp-user', 'password': 'fixture-nntp-password',  # release-audit: synthetic - saved NNTP retry credential
            'connections': 8, 'ssl': True})
        original_site, original_private = self.ui.config_path.read_bytes(), private.read_bytes()
        self.ui.mark(2, 'verified')
        self.read.return_value = ''
        saved = {'host': 'fixture-nntp.example.invalid', 'port': 563, 'safely_editable': True}
        with mock.patch.object(self.ui, 'config', return_value=c), \
             mock.patch.object(self.ui, 'install', return_value=True), \
             mock.patch.object(self.ui, 'enroll_accounts', return_value={'jellyfin', 'seerr', 'sabnzbd'}), \
             mock.patch.object(self.ui, 'wire', return_value=True) as wire, \
             mock.patch.object(self.ui, 'media_naming', return_value=True), \
             mock.patch.object(self.ui, 'capacity_policy', return_value=True), \
             mock.patch.object(self.ui, 'subtitles', return_value=True), \
             mock.patch.object(self.ui, 'indexer_sources', return_value=True), \
             mock.patch.object(usenet, 'providers', return_value=[]), \
             mock.patch.object(usenet, 'saved_provider', return_value=saved), \
             mock.patch.object(usenet, 'needs_login', return_value=False), \
             mock.patch.object(usenet, 'configure', side_effect=[
                 onboarding._Manual('NNTP TLS connection or authentication test failed; existing providers were preserved'),
                 {'connection_verified': True, 'wizard_complete': True}]) as configure:
            code, output = self.run_cli(['3', '--config', str(self.ui.config_path)])
            self.assertEqual(code, 1)
            self.assertEqual(self.ui.state()['3']['status'], 'paused')
            wire.assert_not_called()
            self.assertEqual(self.ui.config_path.read_bytes(), original_site)
            self.assertEqual(private.read_bytes(), original_private)
            code, retry_output = self.run_cli(['3', '--config', str(self.ui.config_path)])
            self.assertEqual(code, 0)
            self.assertEqual(self.ui.state()['3']['status'], 'verified')
        self.assertEqual(self.ui.state()['2']['status'], 'verified')
        self.assertEqual(self.ui.config_path.read_bytes(), original_site)
        self.assertEqual(private.read_bytes(), original_private)
        self.assertEqual(configure.call_count, 2)
        self.assertTrue(all(call.args[1] is None for call in configure.call_args_list))
        rendered = output + retry_output + '\n'.join(self.messages)
        self.assertIn('NNTP TLS connection or authentication test failed', rendered)
        self.assertNotIn('Traceback', rendered)
        self.assertNotIn('fixture-nntp-password', rendered)
        self.assertNotIn('fixture-nntp-user', rendered)

    def test_failure_handler_preserves_bad_progress_without_a_secondary_traceback(self):
        self.ui.mark(2, 'verified')
        progress = self.ui.state_root / 'progress.json'
        progress.write_text('invalid saved progress')
        before = progress.read_bytes()
        with mock.patch.object(self.ui, 'media') as media:
            code, output = self.run_cli(['3'])
        self.assertEqual(code, 1)
        media.assert_not_called()
        self.assertEqual(progress.read_bytes(), before)
        self.assertIn('Saved progress could not be updated', '\n'.join(self.messages))
        self.assertNotIn('Traceback', output + '\n'.join(self.messages))

    def test_menu_saved_state_error_is_sanitized_without_removing_the_file(self):
        self.ui.mark(2, 'verified')
        progress = self.ui.state_root / 'progress.json'
        progress.write_text('invalid synthetic-private-state')
        before = progress.read_bytes()
        code, output = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertEqual(progress.read_bytes(), before)
        self.assertIn('saved state could not be read or updated', output)
        self.assertNotIn('synthetic-private-state', output)
        self.assertNotIn('Traceback', output)

    def test_historical_eligible_jellyfin_prompt_explains_existing_credentials(self):
        from samurai_stack import onboarding
        self.account_fixture(['jellyfin'])
        self.read.return_value = ''
        with mock.patch.object(onboarding, 'onboarding') as enroll:
            self.assertIsNone(self.ui.enroll_accounts(self.c))
        enroll.assert_not_called()
        self.assertIn('enter its EXISTING administrator login', '\n'.join(self.messages))

    def test_password_confirmation_and_spaces_are_preserved(self):
        self.ui.secret_fn = mock.Mock(side_effect=['fixture-password', 'different-password',
                                                   ' fixture password ', ' fixture password '])
        self.assertEqual(' fixture password ', self.ui.account_password('Administrator'))

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
        other = setup.GuidedSetup(self.repo, self.root / 'other-site.json', input_fn=self.read, output_fn=self.messages.append,
                                  state_base=self.root / 'host-state', registry_root=self.root / 'registry')
        self.assertNotEqual(self.ui.state_root, other.state_root)
        self.assertEqual({}, other.state())
        other.mark(2, 'paused')
        self.assertEqual({'1'}, set(self.ui.state()))
        self.assertEqual({'2'}, set(other.state()))

    def test_replacing_checkout_keeps_progress_for_the_same_installation(self):
        write_json(self.ui.config_path, self.c)
        self.ui.mark(2, 'verified')
        second = self.root / 'replacement-checkout'; second.mkdir()
        write_json(second / 'site.json', self.c)
        resumed = setup.GuidedSetup(second, second / 'site.json', state_base=self.root / 'host-state',
                                    registry_root=self.root / 'registry')
        self.assertEqual(self.ui.state_root, resumed.state_root)
        self.assertEqual('verified', resumed.state()['2']['status'])
        self.assertFalse((second / '.setup-state').exists())

    def legacy_registry_fixture(self, source_config=None):
        source = self.repo / 'site.json'
        write_json(source, source_config or self.c)
        old = source.parent / '.setup-state' / hashlib.sha256(str(source.absolute()).encode()).hexdigest()[:16]
        old.mkdir(parents=True, mode=0o700)
        write_json(old / 'progress.json', {'1': {'status': 'verified'}, '2': {'status': 'verified'}})
        canonical = self.root / 'registry' / self.c['deployment_id'] / 'site.json'
        canonical.parent.mkdir(parents=True)
        write_json(canonical, self.c)
        return canonical, old

    def test_first_registry_migration_keeps_matching_legacy_source_progress(self):
        canonical, old = self.legacy_registry_fixture(dict(self.c, timezone='Etc/UTC'))
        before = (old / 'progress.json').read_bytes()
        # Fixtures belong to the CI user; the actual guided command is root-only.
        with mock.patch('samurai_stack.installation_registry._owner'):
            ui = setup.GuidedSetup(self.repo, canonical, state_base=self.root / 'host-state',
                                   registry_root=self.root / 'registry')
            self.assertEqual('verified', ui.state()['1']['status'])
            self.assertEqual('verified', ui.state()['2']['status'])
            ui.mark(3, 'paused')
            self.assertEqual({'1', '2', '3'}, set(ui.state()))
        self.assertEqual(before, (old / 'progress.json').read_bytes())
        self.assertEqual(self.c, json.loads(canonical.read_text()))

    def test_portable_bundle_reads_matching_cwd_legacy_progress(self):
        canonical, old = self.legacy_registry_fixture()
        bundle = self.root / 'portable-bundle'; bundle.mkdir()
        write_json(bundle / 'site.json', dict(self.c, install_root='/opt/unrelated-installation'))
        original = (old / 'progress.json').read_bytes()
        with mock.patch.object(setup.Path, 'cwd', return_value=self.repo), \
             mock.patch('samurai_stack.installation_registry._owner'):
            ui = setup.GuidedSetup(bundle, canonical, state_base=self.root / 'host-state',
                                   registry_root=self.root / 'registry')
            self.assertEqual('verified', ui.state()['2']['status'])
            ui.mark(3, 'paused')
        self.assertEqual(original, (old / 'progress.json').read_bytes())
        self.assertEqual(self.c, json.loads(canonical.read_text()))

    def test_storage_correction_requires_explicit_config_before_resolution(self):
        message = io.StringIO()
        with redirect_stderr(message), mock.patch('samurai_stack.setup_recovery.resolve_configuration') as resolve:
            with self.assertRaises(SystemExit) as captured:
                setup.main(['--choose-storage'])
        self.assertEqual(2, captured.exception.code)
        self.assertIn('requires --config', message.getvalue())
        resolve.assert_not_called()

    def test_fresh_starts_blank_menu_without_resolving_an_old_site(self):
        with mock.patch('samurai_stack.setup_fresh.begin_fresh', return_value=self.root / 'new-session/site.json') as begin, \
             mock.patch('samurai_stack.setup_recovery.resolve_configuration') as resolve, \
             mock.patch.object(self.ui, 'menu', return_value=0) as menu:
            code, text = self.run_cli(['--fresh'])
        self.assertEqual(0, code)
        begin.assert_called_once_with()
        resolve.assert_not_called()
        menu.assert_called_once_with()
        self.assertIn('all six phases start as not started', text)

    def test_fresh_cannot_be_combined_with_resume_or_maintenance(self):
        for arguments in (['--config', 'old.json'], ['3'], ['--recover'], ['--choose-storage'], ['--hardlinks']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as captured:
                    setup.main(['--fresh', *arguments])
                self.assertEqual(2, captured.exception.code)

    def test_storage_correction_accepts_explicit_config(self):
        with mock.patch.object(self.ui, 'choose_storage', return_value=True) as choose:
            code, _ = self.run_cli(['--config', str(self.ui.config_path), '--choose-storage'])
        self.assertEqual(0, code)
        choose.assert_called_once_with()

    def test_registry_progress_never_uses_another_sources_identity(self):
        for key, value in (('deployment_id', '8f8d3fd0-380d-42ab-9c35-ad8f08e5efcb'),
                           ('install_root', '/opt/different-installation')):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary) / 'source'; repo.mkdir()
                source = repo / 'site.json'
                write_json(source, dict(self.c, **{key: value}))
                old = repo / '.setup-state' / hashlib.sha256(str(source.absolute()).encode()).hexdigest()[:16]
                old.mkdir(parents=True)
                write_json(old / 'progress.json', {'2': {'status': 'verified'}})
                canonical = Path(temporary) / 'canonical.json'; write_json(canonical, self.c)
                ui = setup.GuidedSetup(repo, canonical, state_base=Path(temporary) / 'host-state',
                                       registry_root=Path(temporary) / 'registry')
                self.assertEqual({}, ui.state())

    def test_current_registry_progress_precedes_matching_source_legacy_progress(self):
        canonical, old = self.legacy_registry_fixture()
        ui = setup.GuidedSetup(self.repo, canonical, state_base=self.root / 'host-state',
                               registry_root=self.root / 'registry')
        ui.mark(2, 'paused')
        self.assertEqual('paused', ui.state()['2']['status'])
        self.assertEqual('verified', json.loads((old / 'progress.json').read_text())['2']['status'])

    def test_unsafe_source_does_not_override_or_block_canonical_progress(self):
        from samurai_stack import installation_registry as registry
        canonical, _ = self.legacy_registry_fixture()
        with mock.patch.object(registry, '_owner', side_effect=SafetyError('untrusted source')):
            ui = setup.GuidedSetup(self.repo, canonical, state_base=self.root / 'host-state',
                                   registry_root=self.root / 'registry')
            self.assertEqual({}, ui.state())
            ui.mark(2, 'paused')
            self.assertEqual('paused', ui.state()['2']['status'])

    def test_registry_failure_does_not_overwrite_the_active_site(self):
        write_json(self.ui.config_path, self.c)
        original = self.ui.config_path.read_bytes()
        updated = dict(self.c, timezone='Etc/UTC')
        self.remember.side_effect = SafetyError('Storage mount identity mismatch')
        with self.assertRaisesRegex(SafetyError, 'mount identity'):
            self.ui._save_configuration(updated)
        self.assertEqual(original, self.ui.config_path.read_bytes())

    def test_canonical_site_is_written_only_by_the_verified_registry(self):
        write_json(self.ui.config_path, self.c)
        self.remember.return_value = self.ui.config_path
        with mock.patch.object(setup, 'write_json') as write:
            self.ui._save_configuration(self.c)
        self.remember.assert_called_once_with(self.c)
        write.assert_not_called()

    def test_project_collision_stops_before_plan_or_apply_prompt(self):
        self.install_fixture()
        details = {'project': 'homelab-2', 'requested_deployment_id': self.c['deployment_id'],
                   'requested_stage': 2, 'containers': []}
        with mock.patch.object(engine, 'check_container_ownership', side_effect=engine.ProjectCollision(details)), \
             mock.patch.object(engine, 'plan') as plan, mock.patch.object(self.ui, 'invoke') as invoke, \
             self.assertRaises(engine.ProjectCollision):
            self.ui.install(self.c, 2)
        plan.assert_not_called(); invoke.assert_not_called(); self.read.assert_not_called()

    def test_interrupting_previously_verified_step_persists_paused_state(self):
        self.ui.mark(1, 'verified')
        with mock.patch.object(self.ui, 'prepare', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.ui.step(1)
        self.assertEqual('paused', self.ui.state()['1']['status'])

    def test_real_child_vendor_output_is_private_on_failure(self):
        package = self.repo / 'samurai_stack'
        package.mkdir()
        package.joinpath('__init__.py').write_text('')
        package.joinpath('cli.py').write_text('import sys\nprint("{}")\nsys.stderr.write("synthetic-private-vendor-error")\nraise SystemExit(1)\n')
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

    def test_switching_provider_discards_another_providers_assigned_address(self):
        from samurai_stack import catalog
        self.c['vpn'].update(provider='mullvad', wireguard_addresses='10.64.0.2/32', port_forwarding=False)
        write_json(self.ui.config_path, self.c)
        self.read.side_effect = ['1', '1', 'no']
        with mock.patch.object(engine, 'compose_path', return_value=self.root / 'absent-compose'):
            selected = self.ui.vpn_profile(self.c)
        self.assertEqual('protonvpn', selected['vpn']['provider'])
        self.assertNotIn('wireguard_addresses', selected['vpn'])
        env = catalog._torrent(selected)['gluetun']['environment']
        self.assertEqual('10.2.0.2/32', env['WIREGUARD_ADDRESSES'])
        self.assertEqual('10.64.0.2/32', self.c['vpn']['wireguard_addresses'])

    def test_same_provider_keeps_its_reviewed_wireguard_address(self):
        self.c['vpn']['wireguard_addresses'] = '10.2.0.3/32'
        write_json(self.ui.config_path, self.c)
        self.read.side_effect = ['1', '1', 'no']
        with mock.patch.object(engine, 'compose_path', return_value=self.root / 'absent-compose'):
            selected = self.ui.vpn_profile(self.c)
        self.assertEqual('10.2.0.3/32', selected['vpn']['wireguard_addresses'])

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
