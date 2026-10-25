"""Optional reviewed future-import policy through the Bash-facing entry points."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import cli, media_naming, setup


class GuidedNamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = cli.default_config()
        self.messages, self.read = [], mock.Mock(return_value="y")
        self.ui = setup.GuidedSetup(self.root, self.root / "site.json", input_fn=self.read,
            output_fn=self.messages.append, state_base=self.root / "progress", registry_root=self.root / "registry")
        self.plan = {"kind": "future-import-naming", "review_hash": "b"*64,
            "services": {service: {"action": "update", "changes": {"renameMovies" if service == "radarr" else "renameEpisodes":
                {"before": False, "after": True}}, "examples": {"movieExample" if service == "radarr" else "singleEpisodeExample": "Example file"}}
                for service in media_naming.SERVICES}}

    def test_optional_policy_reviews_once_and_verifies_independently(self):
        with mock.patch.object(self.ui, "invoke", side_effect=[(0, self.plan), (0, {"ok": True}), (0, {"ok": True})]) as invoke:
            self.assertTrue(self.ui.media_naming(self.c))
        self.read.assert_called_once()
        arguments = [call.args[0] for call in invoke.call_args_list]
        self.assertEqual(arguments[0][:2], ["naming", "plan"])
        self.assertEqual(arguments[1], ["naming", "apply", "--plan", arguments[0][-1], "--accept-plan", self.plan["review_hash"]])
        self.assertEqual(arguments[2], ["naming", "verify"])
        self.assertIn("Existing title paths and files are not renamed", "\n".join(self.messages))
        self.assertIn("saved and verified", "\n".join(self.messages))

    def test_optional_decline_does_not_block_media_or_write_application_settings(self):
        self.read.return_value = "n"
        with mock.patch.object(self.ui, "invoke", return_value=(0, self.plan)) as invoke:
            self.assertTrue(self.ui.media_naming(self.c))
        invoke.assert_called_once()
        self.assertIn("Naming settings were kept", "\n".join(self.messages))

    def test_matching_policy_never_prompts_or_applies_again(self):
        for item in self.plan["services"].values(): item["action"] = "verify"; item["changes"] = {}
        with mock.patch.object(self.ui, "invoke", return_value=(0, self.plan)) as invoke:
            self.assertTrue(self.ui.media_naming(self.c))
        self.read.assert_not_called(); invoke.assert_called_once()

    def test_plan_apply_or_independent_readback_failure_cannot_claim_verified(self):
        for replies in ([(1, {})], [(0, self.plan), (1, {})], [(0, self.plan), (0, {"ok": True}), (2, {"ok": False})]):
            with self.subTest(replies=replies):
                self.messages.clear()
                with mock.patch.object(self.ui, "invoke", side_effect=replies):
                    self.assertFalse(self.ui.media_naming(self.c))
                self.assertNotIn("saved and verified", "\n".join(self.messages))

    def test_naming_follows_wiring_and_failure_stops_later_optional_steps(self):
        events = []
        with mock.patch.object(self.ui, "config", return_value=self.c), \
             mock.patch.object(self.ui, "install", return_value=True), \
             mock.patch.object(self.ui, "_remember_configuration"), \
             mock.patch.object(self.ui, "enroll_accounts", return_value=set()), \
             mock.patch.object(self.ui, "usenet_provider", return_value=True), \
             mock.patch.object(self.ui, "endpoints"), \
             mock.patch.object(self.ui, "wire", side_effect=lambda stage: events.append("wire") or True), \
             mock.patch.object(self.ui, "media_naming", side_effect=lambda c: events.append("naming") or False), \
             mock.patch.object(self.ui, "capacity_policy") as capacity, \
             mock.patch.object(self.ui, "subtitles") as subtitles, \
             mock.patch.object(self.ui, "indexer_sources") as sources:
            self.assertFalse(self.ui.media())
        self.assertEqual(events, ["wire", "naming"])
        capacity.assert_not_called(); subtitles.assert_not_called(); sources.assert_not_called()


class NamingCliTests(unittest.TestCase):
    def run_cli(self, args):
        with mock.patch.object(cli, "load_config", return_value=cli.default_config()), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_readonly_plan_and_verify_have_separate_results(self):
        preview = {"kind": "future-import-naming", "review_hash": "b"*64}
        with mock.patch.object(media_naming, "plan", return_value=preview), mock.patch.object(cli, "write_json") as write:
            code, _, _ = self.run_cli(["naming", "plan", "--out", "fixture.json"])
        self.assertEqual(code, 0); write.assert_called_once_with("fixture.json", preview)
        with mock.patch.object(media_naming, "verify", return_value={"status": "pending", "ok": False}), \
             mock.patch.object(media_naming, "apply") as apply:
            code, _, _ = self.run_cli(["naming", "verify"])
        self.assertEqual(code, 2); apply.assert_not_called()

    def test_apply_requires_the_exact_review_hash_before_any_module_write(self):
        preview = {"kind": "future-import-naming", "review_hash": "b"*64}
        with mock.patch.object(cli, "private_read", return_value=json.dumps(preview)), \
             mock.patch.object(media_naming, "apply", return_value={"ok": True}) as apply:
            code, _, error = self.run_cli(["naming", "apply", "--plan", "fixture.json", "--accept-plan", "wrong"])
            self.assertEqual(code, 1); self.assertIn("hash does not match", error); apply.assert_not_called()
            code, _, _ = self.run_cli(["naming", "apply", "--plan", "fixture.json", "--accept-plan", preview["review_hash"]])
            self.assertEqual(code, 0); self.assertEqual(apply.call_args.args[1], preview)

    def test_apply_without_review_arguments_is_refused(self):
        with mock.patch.object(media_naming, "apply") as apply:
            code, _, error = self.run_cli(["naming", "apply"])
        self.assertEqual(code, 1); self.assertIn("requires --plan and --accept-plan", error); apply.assert_not_called()


if __name__ == "__main__": unittest.main()
