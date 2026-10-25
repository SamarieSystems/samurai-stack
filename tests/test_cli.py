import io
import json
from contextlib import redirect_stderr, redirect_stdout
import unittest
from unittest.mock import patch

from samurai_stack import cli, engine
from samurai_stack.cli import ask_bool


class WizardAnswers(unittest.TestCase):
    def test_profiles_apply_requires_matching_review_before_any_write(self):
        from samurai_stack import media_profiles
        from samurai_stack.safety import SafetyError
        with patch.object(cli, 'load_config', return_value=cli.default_config()), \
             patch.object(cli, 'private_read', return_value='{"review_hash":"fixture-other"}'), \
             patch.object(media_profiles, 'apply') as apply, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(1, cli.main(['profiles', 'apply', '--plan', 'fixture.json', '--accept-plan', 'fixture-review']))
        apply.assert_not_called()

    def test_typo_reprompts_instead_of_disabling_feature(self):
        with patch('builtins.input', side_effect=['yse', 'YES']) as read, patch('builtins.print'):
            self.assertTrue(ask_bool('Enable torrents'))
            self.assertEqual(read.call_count, 2)

    def test_short_answers_and_defaults(self):
        for answer, default, expected in [('n', True, False), ('y', False, True), ('', True, True), ('', False, False)]:
            with self.subTest(answer=answer, default=default), patch('builtins.input', return_value=answer):
                self.assertEqual(ask_bool('Enable service', default), expected)

    def test_project_collision_reports_selected_recovery_details_without_traceback(self):
        c = cli.default_config()
        details = {"project": "homelab-2", "requested_deployment_id": c["deployment_id"], "requested_stage": 2,
            "containers": [{"name": "homelab-2-bazarr-1", "status": "running", "project": "homelab-2",
                "stage": "2", "deployment_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "mounts": [
                    {"source": "/opt/original/appdata/bazarr", "destination": "/config", "source_exists": False, "appdata": True}]}]}
        output = io.StringIO()
        structured = io.StringIO()
        with patch.object(cli, 'load_config', return_value=c), \
             patch.object(engine, 'verify', side_effect=engine.ProjectCollision(details)), redirect_stderr(output), redirect_stdout(structured):
            self.assertEqual(1, cli.main(['verify', '2']))
        message = output.getvalue()
        for value in ('homelab-2-bazarr-1', 'running', details['containers'][0]['deployment_id'], c['deployment_id'],
                      '/opt/original/appdata/bazarr', 'host source missing', 'original site configuration'):
            self.assertIn(value, message)
        self.assertNotIn('Traceback', message)
        self.assertNotIn('choose a unique', message)
        self.assertIn('./setup --recover', message)
        self.assertEqual({'status': 'blocked', 'error_code': 'EXISTING_INSTALLATION_IDENTITY_CONFLICT',
                          'details': details}, json.loads(structured.getvalue()))
