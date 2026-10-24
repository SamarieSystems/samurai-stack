"""Exercise leak detection and redaction with disposable synthetic inputs."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check-release.py"
SPEC = importlib.util.spec_from_file_location("samurai_release_audit", MODULE_PATH)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class TextAuditTests(unittest.TestCase):
    def rules(self, content, path="config.conf"):
        return {item.rule for item in audit.audit_text(Path(path), content)}

    def test_blank_examples_and_variable_references_are_allowed(self):
        for text in ("PASSWORD=", "PASSWORD=${USER_PASSWORD}", "PASSWORD=$USER_PASSWORD",
                     "API_KEY=your-api-key", "SECRET=replace-me", "TOKEN=<local-token>",
                     "TOKEN_PATH=/run/secrets/service_token",
                     "VPN_PRIVATE_KEY_SECRETFILE=/run/secrets/wireguard_private_key", 'password = ""'):
            with self.subTest(text=text):
                self.assertFalse(self.rules(text, "examples/service.env.example"))

    def test_credential_literals_are_found_without_values_in_report(self):
        credential = "private" + "-sentinel-value"
        for text in ("PASSWORD=" + credential, 'password = "' + credential + '"',
                     '{"API_KEY": "' + credential + '"}'):
            with self.subTest(style=text.split("=", 1)[0]):
                found = audit.audit_text(Path("service.conf"), text)
                self.assertIn("credential-literal", {item.rule for item in found})
                self.assertNotIn(credential, repr(found))

    def test_test_directories_have_no_blanket_exemption(self):
        self.assertIn("credential-literal", self.rules('password="private-sentinel"', "tests/test_config.py"))

    def test_synthetic_assignment_requires_location_annotation_and_value(self):
        marker = " # release-audit: synthetic - negative credential fixture"
        sample = 'password="fixture-password"'
        self.assertFalse(self.rules(sample + marker, "tests/test_config.py"))
        self.assertFalse(self.rules(sample + marker, "scripts/fixtures/service.py"))
        self.assertIn("credential-literal", self.rules(sample, "tests/test_config.py"))
        self.assertIn("credential-literal", self.rules(sample + marker, "config.py"))
        self.assertIn("credential-literal", self.rules('password="unmarked-sentinel"' + marker, "tests/test_config.py"))

    def test_high_confidence_tokens_cannot_be_suppressed_by_comment(self):
        token = "gh" + "p_" + "a" * 36
        marker = " # release-audit: synthetic - negative credential fixture"
        self.assertIn("github-token", self.rules('TOKEN="' + token + '"' + marker, "tests/test_config.py"))

    def test_recognizable_secret_formats_are_detected(self):
        fixtures = {
            "github-token": "gh" + "p_" + "a" * 36,
            "aws-access-key": "AK" + "IA" + "A" * 16,
            "discord-token": "A" * 24 + "." + "B" * 6 + "." + "C" * 30,
            "api-token": "s" + "k-" + "a" * 32,
            "private-key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "credential-url": "https://fixture-user:" + "fixture-password@host.example.test",
            "discord-webhook": "https://discord.com/api/" + "webhooks/123456789/" + "a" * 30,
        }
        for rule, value in fixtures.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, self.rules(value))

    def test_operator_terms_are_optional_literal_and_redacted(self):
        terms = ("operator.internal.invalid", "fixture-operator-login", "ops[internal]")
        for value in terms:
            with self.subTest(value=value):
                self.assertFalse(self.rules(value))
                found = audit.audit_text(Path("config.conf"), value.upper(), deny_terms=terms)
                self.assertEqual({item.rule for item in found}, {"operator-denied-term"})
                self.assertNotIn(value, repr(found))
        self.assertFalse(audit.audit_text(Path("config.conf"), "opsi", deny_terms=("ops[internal]",)))
        self.assertFalse(self.rules("http://192.0.2.20:8096"))

    def test_credential_name_in_normal_expression_is_not_a_literal(self):
        self.assertFalse(self.rules('password = getpass.getpass("Password: ")'))
        self.assertFalse(self.rules('api_key = values.get("api_key", "")'))

    def test_python_examples_are_distinguished_from_real_credential_assignments(self):
        example = 'sample = \'password="negative-sentinel"\''
        self.assertFalse(self.rules(example, "tests/test_example.py"))
        for source in ('password="negative-sentinel"',
                       'config={"PASSWORD":"negative-sentinel"}',
                       'client(password="negative-sentinel")',
                       'config["PASSWORD"]="negative-sentinel"'):
            with self.subTest(source=source):
                self.assertIn("credential-literal", self.rules(source, "tests/test_example.py"))

    def test_secret_name_reference_requires_narrow_value_and_annotation(self):
        marker = " # release-audit: reference - image variable to local secret filename"
        sample = 'config={"API_KEY":"service_api_key"}'
        self.assertFalse(self.rules(sample + marker, "catalog.py"))
        self.assertIn("credential-literal", self.rules(sample, "catalog.py"))
        self.assertIn("credential-literal", self.rules('API_KEY="arbitrary-value"' + marker, "catalog.py"))
        self.assertIn("credential-literal", self.rules('API_KEY="service_api_key"' + marker, "service.conf"))


class TreeAuditTests(unittest.TestCase):
    def test_private_files_are_flagged_but_blank_example_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("PASSWORD=\n")
            self.assertTrue(audit.audit_tree(root).ok)
            for name in (".env", ".env.production", "id_ed25519", "backup.tar.gz", "capture.db"):
                path = root / name
                path.write_text("")
            report = audit.audit_tree(root)
            self.assertEqual(len(report.findings), 5)
            self.assertEqual({f.rule for f in report.findings}, {"private-or-archive-filename"})

    def test_binary_and_oversized_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unknown.bin").write_bytes(b"a\x00b")
            (root / "large.txt").write_bytes(b"a" * (audit.MAX_FILE_BYTES + 1))
            report = audit.audit_tree(root)
            self.assertEqual({f.rule for f in report.findings}, {"binary-unscanned-file", "oversized-unscanned-file"})

    def test_git_and_known_cache_directories_are_not_release_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".git", "__pycache__", ".venv"):
                (root / name).mkdir()
                (root / name / ".env").write_text("PASSWORD=private-sentinel")
            (root / "README.md").write_text("Public documentation.\n")
            report = audit.audit_tree(root)
            self.assertTrue(report.ok)
            self.assertEqual(report.files_scanned, 1)

    def test_symlink_target_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "outside"
            target.mkdir()
            (target / "file.txt").write_text("Preserved content.")
            candidate = root / "candidate"
            candidate.mkdir()
            try:
                (candidate / "linked").symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit unprivileged symlink creation")
            report = audit.audit_tree(candidate)
            self.assertEqual(report.files_scanned, 0)
            self.assertEqual({f.rule for f in report.findings}, {"symlink"})

    def test_cli_json_does_not_include_matching_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = "private" + "-cli-sentinel-value"
            (root / "settings.conf").write_text("PASSWORD=" + value)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = audit.main(["--root", str(root), "--json"])
            self.assertEqual(code, 1)
            self.assertNotIn(value, output.getvalue())
            report = json.loads(output.getvalue())
            self.assertFalse(report["ok"])
            self.assertEqual(report["findings"][0]["path"], "settings.conf")

    def test_missing_candidate_returns_error_without_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "private-path-sentinel")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = audit.main([missing, "--json"])
            self.assertEqual(code, 2)
            self.assertNotIn(missing, output.getvalue())

    def test_private_deny_file_is_applied_without_printing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "candidate"
            root.mkdir()
            term = "operator.internal.invalid"
            (root / "settings.conf").write_text("HOST=" + term)
            deny = Path(directory) / "private-terms.json"
            deny.write_text(json.dumps([term]))
            deny.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = audit.main([str(root), "--deny-file", str(deny), "--json"])
            self.assertEqual(code, 1)
            self.assertNotIn(term, output.getvalue())
            report = json.loads(output.getvalue())
            self.assertEqual(report["findings"][0]["rule"], "operator-denied-term")

    def test_private_deny_file_cannot_be_inside_release_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deny = root / "private-terms.json"
            deny.write_text("[]")
            deny.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = audit.main([str(root), "--deny-file", str(deny), "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"], "invalid-private-deny-file")

    def test_invalid_private_deny_files_fail_closed(self):
        for payload in ({"terms": []}, [""], [" padded "], ["line\nbreak"], [123], ["a"] * 1001):
            with self.subTest(kind=type(payload).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "private-terms.json"
                path.write_text(json.dumps(payload))
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    audit.load_deny_terms(path)

    @unittest.skipIf(os.name != "posix", "Unix private file permission contract")
    def test_group_readable_deny_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-terms.json"
            path.write_text("[]")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                audit.load_deny_terms(path)


if __name__ == "__main__":
    unittest.main()
