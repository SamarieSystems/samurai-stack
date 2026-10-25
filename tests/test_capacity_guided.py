"""Capacity onboarding reviews thresholds, saves exact plans and resumes safely."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import capacity, cli, engine, setup


def preview():
    return {'review_hash': 'e' * 64, 'kind': 'capacity-policy', 'native': {
        'sonarr': {'after': {'minimumFreeSpaceWhenImporting': 51200, 'skipFreeSpaceCheckWhenImporting': False}}},
        'capacity': {role: {'available_bytes': 105 * capacity.GIB, 'size_bytes': 4000 * capacity.GIB}
                     for role in ('data', 'app')}}


class GuidedCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.messages, self.c = [], cli.default_config()
        self.read = mock.Mock()
        self.ui = setup.GuidedSetup(self.root, self.root / 'site.json', input_fn=self.read,
            output_fn=self.messages.append, state_base=self.root / 'progress', registry_root=self.root / 'registry')
        self.saved = self.enterContext(mock.patch.object(capacity, '_saved', return_value=None))
        self.compose = self.enterContext(mock.patch.object(engine, 'compose_path', return_value=self.root / 'absent-compose'))
        self.plan = preview()

    def test_skip_is_explicit_and_never_plans_or_changes_apps(self):
        self.read.return_value = 'n'
        with mock.patch.object(self.ui, 'invoke') as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c, automatic=True))
        invoke.assert_not_called()
        self.assertIn('not quotas', '\n'.join(self.messages))

    def test_defaults_are_reviewed_before_apply_and_independently_verified(self):
        self.read.side_effect = ['y', '', '', 'y']
        with mock.patch.object(self.ui, 'invoke', side_effect=[(0, self.plan), (0, {'ok': True}), (0, {'ok': True})]) as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c, automatic=True))
        planned, applied, verified = [call.args[0] for call in invoke.call_args_list]
        self.assertEqual(planned[-5:], ['--data-reserve-gib', '50', '--app-reserve-gib', '20', '--no-qbit-guard'])
        self.assertEqual(applied[:2], ['capacity', 'apply'])
        self.assertEqual(applied[-1], self.plan['review_hash'])
        self.assertEqual(verified, ['capacity', 'verify'])
        self.assertIn('never automatically resumes', '\n'.join(self.messages))

    def test_declined_review_never_applies(self):
        self.read.side_effect = ['y', '', '', 'n']
        with mock.patch.object(self.ui, 'invoke', return_value=(0, self.plan)) as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c))
        invoke.assert_called_once()

    def test_verified_saved_policy_automatic_retry_preserves_reserves_without_prompts(self):
        self.saved.return_value = {'data_reserve_bytes': 80 * capacity.GIB, 'app_reserve_bytes': 24 * capacity.GIB, 'qbit_guard': True}
        with mock.patch.object(self.ui, 'invoke', return_value=(0, {'ok': True})) as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c, automatic=True))
        self.read.assert_not_called()
        invoke.assert_called_once_with(['capacity', 'verify'], mock.ANY)
        self.assertIn('data 80 GiB', '\n'.join(self.messages))

    def test_torrent_completion_offers_watcher_with_saved_reserves(self):
        self.saved.return_value = {'data_reserve_bytes': 80 * capacity.GIB, 'app_reserve_bytes': 24 * capacity.GIB, 'qbit_guard': False}
        present = self.root / 'present-compose'; present.write_text('{}')
        self.compose.return_value = present
        self.read.side_effect = ['y', '', '', '', 'y']
        with mock.patch.object(self.ui, 'invoke', side_effect=[(0, {'ok': True}), (0, self.plan),
                (0, {'ok': True}), (0, {'ok': True})]) as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c, automatic=True, after_torrents=True))
        arguments = invoke.call_args_list[1].args[0]
        self.assertEqual(arguments[-5:], ['--data-reserve-gib', '80', '--app-reserve-gib', '24', '--qbit-guard'])

    def test_invalid_reserve_input_reprompts_without_silently_coercing_it(self):
        self.read.side_effect = ['y', '0', '1.5', '64', '24', 'n']
        with mock.patch.object(self.ui, 'invoke', return_value=(0, self.plan)) as invoke:
            self.assertTrue(self.ui.capacity_policy(self.c))
        self.assertEqual(invoke.call_args.args[0][-5:], ['--data-reserve-gib', '64', '--app-reserve-gib', '24', '--no-qbit-guard'])
        self.assertEqual('\n'.join(self.messages).count('Enter a whole GiB'), 2)

    def test_plan_or_readback_failure_never_claims_verified(self):
        for replies in ([(1, {})], [(0, self.plan), (0, {'ok': True}), (2, {'ok': False})]):
            with self.subTest(replies=replies):
                self.read.side_effect = ['y', '', '', 'y']
                self.messages.clear()
                with mock.patch.object(self.ui, 'invoke', side_effect=replies):
                    self.assertFalse(self.ui.capacity_policy(self.c))
                self.assertNotIn('Free-space safeguards verified.', '\n'.join(self.messages))

    def test_media_protects_storage_after_wiring_before_source_intake(self):
        events = []
        with mock.patch.object(self.ui, 'config', return_value=self.c), \
             mock.patch.object(self.ui, 'install', return_value=True), \
             mock.patch.object(self.ui, '_remember_configuration'), \
             mock.patch.object(self.ui, 'enroll_accounts', return_value=set()), \
             mock.patch.object(self.ui, 'usenet_provider', return_value=True), \
             mock.patch.object(self.ui, 'wire', side_effect=lambda stage: events.append('wire') or True), \
             mock.patch.object(self.ui, 'media_naming', return_value=True), \
             mock.patch.object(self.ui, 'capacity_policy', side_effect=lambda *a, **k: events.append('capacity') or False), \
             mock.patch.object(self.ui, 'indexer_sources') as sources:
            self.assertFalse(self.ui.media())
        self.assertEqual(events, ['wire', 'capacity'])
        sources.assert_not_called()


class CapacityCliTests(unittest.TestCase):
    def run_cli(self, args):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = cli.main(args)
        return code, output.getvalue()

    def test_plan_has_explicit_choices_and_no_credential_arguments(self):
        with mock.patch.object(cli, 'load_config', return_value=cli.default_config()), \
             mock.patch.object(capacity, 'plan', return_value=preview()) as plan, mock.patch.object(cli, 'write_json'):
            code, _ = self.run_cli(['capacity', 'plan', '--data-reserve-gib', '80', '--app-reserve-gib', '24', '--qbit-guard'])
        self.assertEqual(code, 0)
        self.assertEqual(plan.call_args.kwargs, {'data_reserve_gib': 80, 'app_reserve_gib': 24, 'qbit_guard': True})

    def test_apply_rejects_hash_mismatch_and_unreviewed_parameter_overrides(self):
        for suffix in (['--accept-plan', 'wrong'], ['--accept-plan', 'e' * 64, '--data-reserve-gib', '1'],
                       ['--accept-plan', 'e' * 64, '--no-qbit-guard']):
            with self.subTest(suffix=suffix), mock.patch.object(cli, 'load_config', return_value=cli.default_config()), \
                 mock.patch.object(cli, 'private_read', return_value=json.dumps(preview())), mock.patch.object(capacity, 'apply') as apply:
                code, _ = self.run_cli(['capacity', 'apply', '--plan', 'fixture.json', *suffix])
            self.assertEqual(code, 1); apply.assert_not_called()

    def test_apply_uses_exact_private_review_and_pending_verify_has_exit_two(self):
        with mock.patch.object(cli, 'load_config', return_value=cli.default_config()), \
             mock.patch.object(cli, 'private_read', return_value=json.dumps(preview())), \
             mock.patch.object(capacity, 'apply', return_value={'ok': True}) as apply:
            code, _ = self.run_cli(['capacity', 'apply', '--plan', 'fixture.json', '--accept-plan', 'e' * 64])
        self.assertEqual(code, 0); self.assertEqual(apply.call_args.args[1], preview())
        with mock.patch.object(cli, 'load_config', return_value=cli.default_config()), \
             mock.patch.object(capacity, 'verify', return_value={'status': 'pending', 'ok': False}):
            self.assertEqual(self.run_cli(['capacity', 'verify'])[0], 2)

    def test_guided_capacity_action_cannot_run_with_another_mutating_step(self):
        for other in ('3', '--fresh', '--restore-accounts', '--hardlinks', '--quality-profiles', '--recover', '--choose-storage'):
            with self.subTest(other=other), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                setup.main(['--capacity', other])
            self.assertEqual(stopped.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
