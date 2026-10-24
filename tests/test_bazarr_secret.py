from pathlib import Path
import tempfile
import unittest

from samurai_stack import engine
from samurai_stack.safety import SafetyError


class BazarrSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"install_root": str(self.root)}
        self.yaml = self.root / "appdata" / "bazarr" / "config" / "config.yaml"
        self.yaml.parent.mkdir(parents=True)
        self.key = "1234567890abcdef" * 2

    def test_reads_only_generated_auth_scalar_plain_or_quoted(self):
        for quote in ["", "'", '"']:
            with self.subTest(quote=quote):
                self.yaml.write_text(f"general:\n  ip: 0.0.0.0\nauth:\n  apikey: {quote}{self.key}{quote}\n  type: null\nsonarr:\n  apikey: unrelated\n")
                self.assertEqual(engine.secret_resolver(self.config)("bazarr_api_key"), self.key)

    def test_missing_yaml_does_not_adopt_a_starr_style_xml_key(self):
        (self.yaml.parent.parent / "config.xml").write_text(f"<Config><ApiKey>{self.key}</ApiKey></Config>")
        with self.assertRaisesRegex(SafetyError, "secret bazarr_api_key"):
            engine.secret_resolver(self.config)("bazarr_api_key")

    def test_rejects_duplicate_auth_duplicate_keys_aliases_and_unknown_scalars(self):
        examples = [f"auth:\n  apikey: {self.key}\nauth:\n  apikey: {self.key}\n",
                    f"auth:\n  apikey: {self.key}\n  apikey: {self.key}\n",
                    f"auth:\n  apikey: {self.key}\nauth: {{apikey: other}}\n",
                    f"auth:\n  <<: *defaults\n  apikey: {self.key}\n",
                    "auth:\n  apikey: *private\n", "auth:\n  apikey: !!str arbitrary\n",
                    f"unrelated:\n  apikey: {self.key}\n"]
        for text in examples:
            with self.subTest(text=text[:24]):
                self.yaml.write_text(text)
                with self.assertRaisesRegex(SafetyError, "secret bazarr_api_key"):
                    engine.secret_resolver(self.config)("bazarr_api_key")


if __name__ == "__main__":
    unittest.main()
