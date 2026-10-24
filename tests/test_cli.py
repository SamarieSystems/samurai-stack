import unittest
from unittest.mock import patch

from samurai_stack.cli import ask_bool


class WizardAnswers(unittest.TestCase):
    def test_typo_reprompts_instead_of_disabling_feature(self):
        with patch('builtins.input', side_effect=['yse', 'YES']) as read, patch('builtins.print'):
            self.assertTrue(ask_bool('Enable torrents'))
            self.assertEqual(read.call_count, 2)

    def test_short_answers_and_defaults(self):
        for answer, default, expected in [('n', True, False), ('y', False, True), ('', True, True), ('', False, False)]:
            with self.subTest(answer=answer, default=default), patch('builtins.input', return_value=answer):
                self.assertEqual(ask_bool('Enable service', default), expected)
