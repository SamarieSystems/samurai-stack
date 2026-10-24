from pathlib import Path
import tempfile
import unittest

from samurai_stack import engine
from samurai_stack.safety import SafetyError


class SabSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"install_root": str(self.root)}
        self.ini = self.root / "appdata" / "sabnzbd" / "sabnzbd.ini"
        self.ini.parent.mkdir(parents=True)
        self.key = "1234567890abcdef" * 2

    def test_configobj_preamble_and_nested_later_sections_are_supported(self):
        for quote in ["", "'", '"']:
            with self.subTest(quote=quote):
                self.ini.write_text(f'__version__ = 19\n[misc]\napi_key = {quote}{self.key}{quote}\nnzb_key = unrelated\n[servers]\n[[server.example.invalid]]\npassword = private\n')
                self.assertEqual(engine.secret_resolver(self.config)("sab_api_key"), self.key)

    def test_rejects_duplicate_sections_duplicate_keys_and_complex_shapes(self):
        examples = [f"[misc]\napi_key = {self.key}\n[misc]\napi_key = {self.key}\n",
                    f"[misc]\napi_key = {self.key}\napi_key = {self.key}\n",
                    "[misc]\napi_key = unexpected, list\n", f"[servers]\napi_key = {self.key}\n",
                    f"[misc]\n[[nested]]\napi_key = {self.key}\n"]
        for text in examples:
            with self.subTest(text=text[:24]):
                self.ini.write_text(text)
                with self.assertRaisesRegex(SafetyError, "secret sab_api_key"):
                    engine.secret_resolver(self.config)("sab_api_key")


if __name__ == "__main__":
    unittest.main()
