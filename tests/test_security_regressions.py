"""Independent regressions for credential transport and local secret handling.

HTTP fixtures use loopback servers only. Compose validation uses `config`,
which parses disposable synthetic inputs without starting/pulling containers.
"""
from contextlib import contextmanager
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import warnings

from samurai_stack import catalog, cli, engine
from samurai_stack.safety import SafetyError


@contextmanager
def http_fixture(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback(self)

        def do_POST(self):
            callback(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def reply(handler, body=b"{}", status=200, location=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    if location:
        handler.send_header("Location", location)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class TransportSecurityTests(unittest.TestCase):
    def test_redirects_never_forward_api_keys_to_another_origin(self):
        received = []

        def sink(handler):
            received.append(handler.headers.get("X-Api-Key"))
            reply(handler)

        with http_fixture(sink) as destination:
            for code, method in ((302, "GET"), (307, "POST"), (308, "POST")):
                with self.subTest(code=code):
                    def redirect(handler):
                        reply(handler, status=code, location=destination + "/capture")

                    with http_fixture(redirect) as origin:
                        with self.assertRaises(SafetyError) as captured:
                            engine.request(method, origin + "/api", {"X-Api-Key": "synthetic-redirect-fixture"})
                        self.assertNotIn("synthetic-redirect-fixture", str(captured.exception))
        self.assertEqual(received, [])

    def test_environment_http_proxy_cannot_receive_credentials(self):
        direct_hits, proxy_hits = [], []

        def origin(handler):
            direct_hits.append(True)
            reply(handler, b'{"source":"origin"}')

        def proxy(handler):
            proxy_hits.append(handler.headers.get("X-Api-Key") is not None)
            reply(handler, b'{"source":"proxy"}')

        with http_fixture(origin) as address, http_fixture(proxy) as proxy_address:
            values = {key: proxy_address for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")}
            values.update({"NO_PROXY": "", "no_proxy": ""})
            # Clear urlopen's cached default opener as well, so this test would
            # catch a regression back to urlopen with ambient proxy settings.
            with patch.dict(os.environ, values), patch("urllib.request._opener", None):
                result = engine.request("GET", address + "/api", {"X-Api-Key": "synthetic-proxy-fixture"})
        self.assertEqual(result, {"source": "origin"})
        self.assertEqual(direct_hits, [True])
        self.assertEqual(proxy_hits, [])

    def test_http_errors_do_not_echo_response_or_credential_values(self):
        private_fixture = "synthetic" + "-response-sentinel"

        def rejected(handler):
            reply(handler, private_fixture.encode(), status=401)

        with http_fixture(rejected) as address:
            with self.assertRaises(SafetyError) as captured:
                engine.request("GET", address + "/api", {"X-Api-Key": private_fixture})
        self.assertIn("401", str(captured.exception))
        self.assertNotIn(private_fixture, str(captured.exception))
        self.assertNotIn(address, str(captured.exception))

    def test_json_form_text_and_empty_response_contracts_remain_usable(self):
        requests = []

        def endpoint(handler):
            length = int(handler.headers.get("Content-Length", 0))
            requests.append((handler.path, handler.headers.get("Content-Type"), handler.rfile.read(length)))
            reply(handler, {"/text": b"Ok.", "/empty": b"", "/json": b'{"ready":true}'}.get(handler.path, b"{}"))

        fixture_value = "synthetic-'\"-$-\\-form"
        with http_fixture(endpoint) as address:
            self.assertEqual(engine.request("GET", address + "/text"), "Ok.")
            self.assertIsNone(engine.request("GET", address + "/empty"))
            self.assertEqual(engine.request("GET", address + "/json"), {"ready": True})
            engine.request("POST", address + "/form", form={"password": fixture_value})
            engine.request("POST", address + "/body", json={"value": fixture_value})
        self.assertEqual(parse_qs(requests[-2][2].decode()), {"password": [fixture_value]})
        self.assertEqual(requests[-2][1], "application/x-www-form-urlencoded")
        self.assertEqual(json.loads(requests[-1][2]), {"value": fixture_value})

    def test_mixed_body_formats_fail_before_any_request(self):
        with patch.object(engine._HTTP, "open") as opened:
            with self.assertRaises(SafetyError):
                engine.request("POST", "http://127.0.0.1:1/", json={}, form={})
        opened.assert_not_called()


class HiddenInputTests(unittest.TestCase):
    def test_getpass_warning_refuses_echo_fallback_without_writing_secret(self):
        def unsafe_input(*args):
            warnings.warn("synthetic-input-warning-sentinel", cli.getpass.GetPassWarning)
            raise AssertionError("Echo fallback must not continue after the warning")

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-local-secret"
            with patch.object(cli, "load_config", return_value={}), \
                 patch.object(engine, "linux_only"), patch.object(engine, "initialize_install"), \
                 patch.object(engine, "secret_path", return_value=target), \
                 patch.object(cli.getpass, "getpass", side_effect=unsafe_input), \
                 patch.object(cli, "atomic_write") as written, \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = cli.main(["secret", "fixture_name"])
            self.assertEqual(result, 1)
            self.assertFalse(target.exists())
            written.assert_not_called()
        self.assertIn("Hidden credential input is unavailable", output.getvalue())
        self.assertNotIn("synthetic-input-warning-sentinel", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_private_file_import_does_not_request_terminal_input(self):
        fixture_value = "synthetic-import-fixture\n"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-local-secret"
            with patch.object(cli, "load_config", return_value={}), \
                 patch.object(engine, "linux_only"), patch.object(engine, "initialize_install"), \
                 patch.object(engine, "secret_path", return_value=target), \
                 patch.object(cli, "private_read", return_value=fixture_value), \
                 patch.object(cli.getpass, "getpass") as hidden_input, \
                 patch.object(cli, "atomic_write") as written, \
                 contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["secret", "fixture_name", "--from-file", "fixture-private-file"])
            self.assertEqual(result, 0)
            hidden_input.assert_not_called()
            written.assert_called_once_with(target, fixture_value)


SYNTHETIC_VALUES = {
    "VALUE_PLAIN": "fixture-plain",
    "VALUE_QUOTES": "fixture-single'and-double\"quotes",
    "VALUE_DOLLARS": "fixture-$UNSET-${UNSET}-$$-literal",
    "VALUE_BACKSLASH": "fixture-one\\two\\three",
    "VALUE_TRAILING": "fixture-trailing-backslash" + "\\",
    "VALUE_COMBINED": "fixture-#hash-'single'-\"double\"-$literal-" + "\\",
}


def create_synthetic_env(root, values=None):
    values = SYNTHETIC_VALUES if values is None else values
    config = {"install_root": str(root), "deployment_name": "fixture"}
    stage = root / "stages" / "fixture-4"
    stage.mkdir(parents=True, exist_ok=True)
    mapping = {name: "fixture_" + str(index) for index, name in enumerate(values)}
    private_values = {mapping[name]: value for name, value in values.items()}
    with patch.object(catalog, "environment_secrets", return_value={"fixture": mapping}), \
         patch.object(engine, "secret_resolver", return_value=private_values.__getitem__):
        engine.env_files(config, 4)
    return stage / "fixture.env"


class DotenvSecurityTests(unittest.TestCase):
    def test_output_keeps_dollars_quoted_backslashes_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = create_synthetic_env(Path(directory))
            encoded = env_path.read_text()
            self.assertIn('VALUE_DOLLARS="fixture-$$UNSET-$${UNSET}-$$$$-literal"', encoded)
            self.assertIn('VALUE_TRAILING="fixture-trailing-backslash\\\\"', encoded)
            self.assertIn('single\'and-double\\"quotes', encoded)
            if os.name == "posix":
                self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

    def test_controls_cannot_overwrite_an_existing_env_file(self):
        for value in ("fixture\nnext-line", "fixture\rcarriage", "fixture\x00nul"):
            with self.subTest(control=repr(value[-1:])), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "stages" / "fixture-4" / "fixture.env"
                target.parent.mkdir(parents=True)
                target.write_text("retained-existing-configuration\n")
                with self.assertRaises(SafetyError):
                    create_synthetic_env(root, {"VALUE_INVALID": value})
                self.assertEqual(target.read_text(), "retained-existing-configuration\n")

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable on this host")
    def test_actual_compose_parser_preserves_every_synthetic_value(self):
        with tempfile.TemporaryDirectory(prefix="samurai-env-parser-") as directory:
            root = Path(directory)
            env_path = create_synthetic_env(root)
            model = {"services": {"fixture": {"image": "busybox:1.36", "env_file": [str(env_path)]}}}
            compose = root / "compose.json"
            compose.write_text(json.dumps(model))
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("COMPOSE_") and key not in SYNTHETIC_VALUES}
            result = subprocess.run(["docker", "--context", "default", "compose", "--project-name", "samurai-env-parser-fixture",
                                     "--project-directory", str(root), "--file", str(compose), "config", "--format", "json"],
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, "Compose config rejected disposable synthetic input")
            actual = json.loads(result.stdout)["services"]["fixture"]["environment"]
            for key, expected in SYNTHETIC_VALUES.items():
                # Compose's config renderer doubles every '$' in the resolved
                # output so that model can be safely reused as a Compose file.
                # Undo that serialization layer, not dotenv parsing itself.
                # Upstream: docker/compose cmd/compose/config.go runConfig.
                resolved = actual[key].replace("$$", "$")
                self.assertEqual(hashlib.sha256(resolved.encode()).hexdigest(),
                                 hashlib.sha256(expected.encode()).hexdigest(), key)


if __name__ == "__main__":
    unittest.main()
