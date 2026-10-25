"""Optional subtitle onboarding and the public Bash-facing CLI boundary."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from samurai_stack import cli, setup, subtitle_setup
from samurai_stack.safety import SafetyError, write_json


class GuidedSubtitleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "app"; (self.app / "state").mkdir(parents=True)
        self.c = cli.default_config(); self.c["install_root"] = str(self.app)
        self.messages, self.read = [], Mock(return_value="y")
        self.ui = setup.GuidedSetup(self.root, self.root / "site.json", input_fn=self.read,
            output_fn=self.messages.append, state_base=self.root / "progress", registry_root=self.root / "registry")
        self.enterContext(patch.object(self.ui, "_state_dir", return_value=self.root))
        self.preview = {"kind": "subtitle-future-policy", "providers": list(subtitle_setup.DEFAULT_PROVIDERS),
                        "review_hash": "a"*64}
        self.report = {"ok": True, "scope": "future_additions_only", "notifications": []}
        self.plan = self.enterContext(patch.object(subtitle_setup, "plan", return_value=self.preview))
        self.apply = self.enterContext(patch.object(subtitle_setup, "apply", return_value=self.report))
        self.verify = self.enterContext(patch.object(subtitle_setup, "verify", return_value=self.report))

    def output(self):return "\n".join(self.messages)

    def test_fresh_optional_policy_is_reviewed_once_and_reports_delivery_limit(self):
        self.assertTrue(self.ui.subtitles(self.c))
        self.read.assert_called_once()
        self.plan.assert_called_once_with(self.c, subtitle_setup.DEFAULT_PROVIDERS)
        self.apply.assert_called_once_with(self.c, self.preview)
        self.assertIn("existing-library backfill was not run", self.output())
        self.assertIn("download has not been tested", self.output())
        self.assertIn("No current title will be assigned or searched", self.output())

    def test_declining_optional_subtitles_does_not_block_media_or_write_backend(self):
        self.read.return_value = "n"
        self.assertTrue(self.ui.subtitles(self.c))
        self.apply.assert_not_called(); self.verify.assert_not_called()
        self.assertIn("deferred", self.output())
        self.assertFalse((self.app / "state/subtitle-setup.json").exists())

    def test_completed_policy_keeps_provider_choices_without_prompt(self):
        path = self.app / "state/subtitle-setup.json"; write_json(path, {})
        with patch.object(subtitle_setup, "_receipt", return_value={"state": "verified", "providers": ["opensubtitlescom"]}):
            self.assertTrue(self.ui.subtitles(self.c))
        self.verify.assert_called_once_with(self.c)
        self.plan.assert_called_once_with(self.c, ["opensubtitlescom"])
        self.apply.assert_not_called(); self.read.assert_not_called()
        self.assertIn("provider choices were kept", self.output())

    def test_interrupted_policy_reuses_chosen_providers_for_review(self):
        path = self.app / "state/subtitle-setup.json"; write_json(path, {})
        with patch.object(subtitle_setup, "_receipt", return_value={"state": "intent", "providers": ["opensubtitlescom"]}):
            self.assertTrue(self.ui.subtitles(self.c))
        self.plan.assert_called_once_with(self.c, ["opensubtitlescom"])
        self.verify.assert_not_called()

    def test_exact_legacy_verified_profile_gets_reviewed_repair_instead_of_dead_end(self):
        path = self.app / "state/subtitle-setup.json"; write_json(path, {})
        self.preview['repair_profile'] = True
        with patch.object(subtitle_setup, "_receipt", return_value={"state": "verified", "providers": ["gestdown"]}):
            self.assertTrue(self.ui.subtitles(self.c))
        self.verify.assert_not_called();self.apply.assert_called_once_with(self.c, self.preview)
        self.read.assert_called_once()
        self.assertIn('repairs the exact earlier installer-created', self.output())

    def test_saved_policy_cannot_claim_success_without_verified_readback(self):
        path = self.app / "state/subtitle-setup.json"; write_json(path, {})
        self.verify.return_value = {'ok': False}
        with patch.object(subtitle_setup, "_receipt", return_value={"state": "verified", "providers": ["gestdown"]}):
            with self.assertRaisesRegex(SafetyError, 'did not pass'):self.ui.subtitles(self.c)
        self.assertNotIn('passed readback', self.output())

    def test_throttled_provider_is_visible_without_false_download_success(self):
        self.report["notifications"] = ["Provider is throttled or unavailable: gestdown"]
        self.assertTrue(self.ui.subtitles(self.c))
        self.assertIn("Needs attention: Provider", self.output())
        self.assertIn("download has not been tested", self.output())

    def test_failed_readback_cannot_claim_configured_policy(self):
        self.apply.return_value = {"ok": False, "scope": "future_additions_only"}
        with self.assertRaisesRegex(SafetyError, "did not confirm"):self.ui.subtitles(self.c)
        self.assertNotIn("saved and verified", self.output())

    def test_media_runs_subtitle_review_after_wiring_and_capacity_before_indexer_pause(self):
        events = []
        with patch.object(self.ui, "config", return_value=self.c), \
             patch.object(self.ui, "install", return_value=True), \
             patch.object(self.ui, "_remember_configuration"), \
             patch.object(self.ui, "enroll_accounts", return_value=set()), \
             patch.object(self.ui, "usenet_provider", return_value=True), \
             patch.object(self.ui, "endpoints"), \
             patch.object(self.ui, "wire", side_effect=lambda stage:events.append("wire") or True), \
             patch.object(self.ui, "media_naming", return_value=True), \
             patch.object(self.ui, "capacity_policy", side_effect=lambda *a, **k:events.append("capacity") or True), \
             patch.object(self.ui, "subtitles", side_effect=lambda c:events.append("subtitles") or True), \
             patch.object(self.ui, "indexer_sources", side_effect=lambda c:events.append("indexers") or False):
            self.assertFalse(self.ui.media())
        self.assertEqual(events, ["wire", "capacity", "subtitles", "indexers"])


class SubtitleCliTests(unittest.TestCase):
    def run_cli(self, args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.enterContext(patch.object(cli, "load_config", return_value=cli.default_config()))

    def test_plan_defaults_and_explicit_provider_selection(self):
        for suffix, expected in (([], subtitle_setup.DEFAULT_PROVIDERS), (["--providers", "gestdown"], ["gestdown"])):
            with self.subTest(suffix=suffix), patch.object(subtitle_setup, "plan", return_value={"review_hash": "a"*64}) as plan, \
                 patch.object(cli, "write_json") as write:
                code, _, _ = self.run_cli(["subtitles", "plan", "--out", "fixture.json", *suffix])
                self.assertEqual(code, 0); self.assertEqual(plan.call_args.args[1], expected)
                write.assert_called_once_with("fixture.json", {"review_hash": "a"*64})

    def test_verify_is_readonly(self):
        with patch.object(subtitle_setup, "verify", return_value={"ok": True}) as verify, \
             patch.object(subtitle_setup, "apply") as apply:
            code, _, _ = self.run_cli(["subtitles", "verify"])
        self.assertEqual(code, 0); verify.assert_called_once(); apply.assert_not_called()

    def test_apply_requires_exact_hash_before_module_call(self):
        with patch.object(cli, "private_read", return_value=json.dumps({"review_hash": "a"*64})), \
             patch.object(subtitle_setup, "apply") as apply:
            code, _, error = self.run_cli(["subtitles", "apply", "--plan", "fixture.json", "--accept-plan", "bad"])
        self.assertEqual(code, 1); self.assertIn("hash does not match", error); apply.assert_not_called()

    def test_apply_uses_exact_reviewed_document(self):
        preview = {"kind": "subtitle-future-policy", "review_hash": "a"*64}
        with patch.object(cli, "private_read", return_value=json.dumps(preview)), \
             patch.object(subtitle_setup, "apply", return_value={"ok": True}) as apply:
            code, _, _ = self.run_cli(["subtitles", "apply", "--plan", "fixture.json", "--accept-plan", preview["review_hash"]])
        self.assertEqual(code, 0); self.assertEqual(apply.call_args.args[1], preview)

    def test_provider_choice_cannot_override_accepted_plan_or_readonly_verify(self):
        for action in ("apply", "verify"):
            with self.subTest(action=action), patch.object(subtitle_setup, "apply") as apply, \
                 patch.object(subtitle_setup, "verify") as verify:
                code, _, _ = self.run_cli(["subtitles", action, "--providers", "gestdown"])
                self.assertEqual(code, 1); apply.assert_not_called(); verify.assert_not_called()


if __name__ == "__main__":unittest.main()
