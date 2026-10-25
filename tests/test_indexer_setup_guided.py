"""Optional guided source intake stays private, bounded and honest."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from samurai_stack import cli, indexer_setup, setup

KEY = "fixture-guided-indexer-key"  # release-audit: synthetic - hidden source intake credential
INPUT_ID = "b6f4d399-361b-4d65-8526-20a3964e1cc9"


def readiness(ok=False):
    return {"ok": ok, "status": "verified" if ok else "pending", "sources": [],
            "protocols": {"usenet": {"tested_sources": int(ok), "movies": ok, "tv": ok, "anime": False},
                          "torrent": {"tested_sources": 0, "movies": False, "tv": False, "anime": False}},
            "missing_protocols": [] if ok else ["usenet"], "missing_categories": [] if ok else ["movies", "tv"]}


def document(ok=False, action=False):
    return {"kind": "indexer-source-intake", "review_hash": "f" * 64, "readiness": readiness(ok),
            "action": {"name": "Samurai homelab NZBGeek", "base_url": "https://api.nzbgeek.info", "api_path": "/api"} if action else None,
            "app_profiles": [{"id": 1, "name": "Standard", "enableAutomaticSearch": True, "enableInteractiveSearch": True}]}


class GuidedIndexerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.messages = []
        self.read = mock.Mock()
        self.hidden = mock.Mock(return_value=KEY)
        self.ui = setup.GuidedSetup(self.root, self.root / "site.json", input_fn=self.read,
            output_fn=self.messages.append, secret_fn=self.hidden, state_base=self.root / "state", registry_root=self.root / "registry")
        self.c = cli.default_config()
        self.c["features"].update(usenet=True, torrents=False)
        self.preview = document(action=True)
        self.plan = self.enterContext(mock.patch.object(indexer_setup, "plan", side_effect=[document(), self.preview]))
        self.saved = self.enterContext(mock.patch.object(indexer_setup, "saved_inputs", return_value=[]))
        self.save = self.enterContext(mock.patch.object(indexer_setup, "save_input", return_value=INPUT_ID))
        self.apply = self.enterContext(mock.patch.object(indexer_setup, "apply", return_value=readiness(True)))

    def output(self):return "\n".join(self.messages)

    def test_existing_ready_sources_require_no_input_or_reconfiguration(self):
        self.plan.side_effect = None; self.plan.return_value = document(True)
        self.assertTrue(self.ui.indexer_sources(self.c))
        self.read.assert_not_called(); self.hidden.assert_not_called()
        self.saved.assert_not_called(); self.save.assert_not_called(); self.apply.assert_not_called()
        self.assertIn("separate from container health", self.output())

    def test_empty_choice_pauses_once_without_false_completion(self):
        self.read.return_value = ""
        self.assertFalse(self.ui.indexer_sources(self.c))
        self.read.assert_called_once(); self.save.assert_not_called(); self.apply.assert_not_called()
        self.assertNotIn("step is complete", self.output())
        self.assertIn("./setup 3", self.output())

    def test_nzbgeek_key_is_hidden_and_plan_review_uses_no_credential_arguments(self):
        self.read.side_effect = ["1", "y"]
        self.assertTrue(self.ui.indexer_sources(self.c))
        self.hidden.assert_called_once()
        self.save.assert_called_once_with(self.c, {"preset": "nzbgeek", "api_key": KEY})
        self.assertEqual(self.plan.call_args_list[1], mock.call(self.c, INPUT_ID))
        self.apply.assert_called_once_with(self.c, self.preview)
        self.assertNotIn(KEY, self.output())
        for path in (self.root / "state").rglob("*.json"):
            self.assertNotIn(KEY, path.read_text())

    def test_saved_input_retries_without_another_key_prompt(self):
        self.saved.return_value = [{"input_id": INPUT_ID, "label": "NZBGeek", "base_url": "https://api.nzbgeek.info"}]
        self.read.side_effect = ["r1", "y"]
        self.assertTrue(self.ui.indexer_sources(self.c))
        self.save.assert_not_called(); self.hidden.assert_not_called()
        self.apply.assert_called_once_with(self.c, self.preview)

    def test_review_decline_keeps_private_retry_input_without_vendor_write(self):
        self.read.side_effect = ["1", "n"]
        self.assertFalse(self.ui.indexer_sources(self.c))
        self.save.assert_called_once(); self.apply.assert_not_called()
        self.assertEqual(self.read.call_count, 2)

    def test_torrents_only_offers_torznab_and_allows_a_keyless_public_feed(self):
        self.c["features"].update(usenet=False, torrents=True)
        self.read.side_effect = ["3", "Public feed", "https://feed.example.invalid", "", "n", "y"]
        self.assertTrue(self.ui.indexer_sources(self.c))
        supplied = self.save.call_args.args[1]
        self.assertEqual(supplied, {"implementation": "Torznab", "label": "Public feed",
            "base_url": "https://feed.example.invalid", "api_path": "/api", "api_key": ""})
        self.hidden.assert_not_called()
        self.assertNotIn("Add NZBGeek", self.output())
        self.assertNotIn("Add Generic Newznab", self.output())

    def test_ambiguous_profiles_require_one_explicit_existing_profile(self):
        first = document()
        first["app_profiles"].append({**first["app_profiles"][0], "id": 2, "name": "Other"})
        self.plan.side_effect = [first, self.preview]
        self.read.side_effect = ["1", "2", "y"]
        self.assertTrue(self.ui.indexer_sources(self.c))
        self.assertEqual(self.save.call_args.args[1]["app_profile_id"], 2)

    def test_incomplete_after_one_added_source_stays_pending_without_loop(self):
        self.read.side_effect = ["1", "y"]
        self.apply.return_value = readiness(False)
        self.assertFalse(self.ui.indexer_sources(self.c))
        self.assertEqual(self.read.call_count, 2)
        self.apply.assert_called_once()
        self.assertIn("remain pending", self.output())
        self.assertNotIn("step is complete", self.output())

    def test_existing_failed_source_with_coverage_is_repair_action_not_add_loop(self):
        initial = document(); initial["readiness"].update(missing_protocols=[], missing_categories=[])
        self.plan.side_effect = None; self.plan.return_value = initial
        self.assertFalse(self.ui.indexer_sources(self.c))
        self.assertIn("Repair it", self.output())
        self.read.assert_not_called(); self.save.assert_not_called(); self.apply.assert_not_called()

    def test_media_checks_sources_after_wiring_and_cannot_finish_from_containers_alone(self):
        events = []
        with mock.patch.object(self.ui, "config", return_value=self.c), \
             mock.patch.object(self.ui, "install", return_value=True), \
             mock.patch.object(self.ui, "_remember_configuration"), \
             mock.patch.object(self.ui, "enroll_accounts", return_value={"sonarr", "radarr"}), \
             mock.patch.object(self.ui, "usenet_provider", return_value=True), \
             mock.patch.object(self.ui, "wire", side_effect=lambda stage: events.append("wire") or True), \
             mock.patch.object(self.ui, "media_naming", return_value=True), \
             mock.patch.object(self.ui, "capacity_policy", return_value=True), \
             mock.patch.object(self.ui, "subtitles", return_value=True), \
             mock.patch.object(self.ui, "indexer_sources", side_effect=lambda c: events.append("sources") or False):
            self.assertFalse(self.ui.media())
        self.assertEqual(events, ["wire", "sources"])


class IndexerCliTests(unittest.TestCase):
    def run_cli(self, args):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = cli.main(args)
        return code, output.getvalue()

    def test_plan_accepts_only_opaque_saved_input_reference(self):
        preview = document(action=True)
        with mock.patch.object(cli, "load_config", return_value=cli.default_config()), \
             mock.patch.object(indexer_setup, "plan", return_value=preview) as plan, \
             mock.patch.object(cli, "write_json") as write:
            code, output = self.run_cli(["indexers", "plan", "--input-id", INPUT_ID, "--out", "fixture-plan.json"])
        self.assertEqual(code, 0)
        self.assertEqual(plan.call_args.args[1], INPUT_ID)
        write.assert_called_once_with("fixture-plan.json", preview)
        self.assertNotIn(KEY, output)

    def test_pending_readonly_verify_has_distinct_exit_status(self):
        with mock.patch.object(cli, "load_config", return_value=cli.default_config()), \
             mock.patch.object(indexer_setup, "verify", return_value=readiness(False)), \
             mock.patch.object(indexer_setup, "apply") as apply:
            code, _ = self.run_cli(["indexers", "verify"])
        self.assertEqual(code, 2)
        apply.assert_not_called()

    def test_apply_requires_matching_hash_before_any_vendor_write(self):
        with mock.patch.object(cli, "load_config", return_value=cli.default_config()), \
             mock.patch.object(cli, "private_read", return_value=json.dumps(document(action=True))), \
             mock.patch.object(indexer_setup, "apply") as apply:
            code, _ = self.run_cli(["indexers", "apply", "--plan", "fixture.json", "--accept-plan", "wrong-review"])
        self.assertEqual(code, 1)
        apply.assert_not_called()

    def test_apply_uses_the_exact_private_reviewed_document(self):
        preview = document(action=True)
        with mock.patch.object(cli, "load_config", return_value=cli.default_config()), \
             mock.patch.object(cli, "private_read", return_value=json.dumps(preview)), \
             mock.patch.object(indexer_setup, "apply", return_value=readiness(True)) as apply:
            code, _ = self.run_cli(["indexers", "apply", "--plan", "fixture.json", "--accept-plan", preview["review_hash"]])
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[1], preview)


if __name__ == "__main__":
    unittest.main()
