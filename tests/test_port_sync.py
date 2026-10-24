import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("samurai_port_sync", Path(__file__).resolve().parents[1] / "scripts" / "port_sync.py")
port_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(port_sync)


class API:
    def __init__(self):
        self.healthy = True
        self.prefs = {"current_network_interface": "tun0", "listen_port": 45000, "up_limit": 0}
        self.updates = []
        self.ignore = False
        self.auth_ok = True

    def vpn_healthy(self):
        return self.healthy

    def login(self):
        if not self.auth_ok:
            raise RuntimeError("authentication failed")

    def preferences(self):
        return dict(self.prefs)

    def set_preferences(self, update):
        self.updates.append(update)
        if not self.ignore:
            self.prefs.update(update)


class PortSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = Path(self.tmp.name) / "port"
        self.ready = Path(self.tmp.name) / "ready"
        self.port.write_text("47123\n")

    def run_poll(self, api):
        return port_sync.synchronize(api, port_path=self.port, ready_path=self.ready)

    def test_new_forwarded_port_is_read_back_before_healthy(self):
        api = API()
        self.assertEqual(self.run_poll(api), "synchronized")
        self.assertEqual(api.prefs["listen_port"], 47123)
        self.assertTrue(port_sync.ready_file(self.ready))
        self.assertEqual(api.prefs["up_limit"], 0)

    def test_vpn_failure_binds_loopback_and_clears_ready(self):
        api = API()
        self.run_poll(api)
        api.healthy = False
        self.assertEqual(self.run_poll(api), "holding")
        self.assertEqual(api.prefs["current_network_interface"], "lo")
        self.assertFalse(self.ready.exists())

    def test_missing_port_stays_on_loopback_until_valid_restoration(self):
        api = API()
        self.port.unlink()
        self.assertEqual(self.run_poll(api), "holding")
        self.assertEqual(api.prefs["current_network_interface"], "lo")
        self.port.write_text("53001")
        self.assertEqual(self.run_poll(api), "synchronized")
        self.assertEqual(api.prefs["current_network_interface"], "tun0")
        self.assertEqual(api.prefs["listen_port"], 53001)

    def test_false_success_never_publishes_ready(self):
        api = API()
        api.ignore = True
        with self.assertRaises(RuntimeError):
            self.run_poll(api)
        self.assertFalse(self.ready.exists())

    def test_bad_auth_never_changes_preferences_or_sets_ready(self):
        api = API()
        api.auth_ok = False
        with self.assertRaises(RuntimeError):
            self.run_poll(api)
        self.assertFalse(api.updates)
        self.assertFalse(self.ready.exists())

    def test_health_tolerates_only_tiny_timestamp_rounding(self):
        self.ready.write_text("synchronized\n")
        stamp = self.ready.stat().st_mtime
        with mock.patch.object(port_sync.time, "time", return_value=stamp - 0.001):
            self.assertTrue(port_sync.ready_file(self.ready))
        with mock.patch.object(port_sync.time, "time", return_value=stamp - 2):
            self.assertFalse(port_sync.ready_file(self.ready))
        with mock.patch.object(port_sync.time, "time", return_value=stamp + 61):
            self.assertFalse(port_sync.ready_file(self.ready))

    def test_malformed_port_never_triggers_shell_or_arbitrary_interface(self):
        for text in ["0", "65536", "-1", "123;whoami", "100\n200", "a" * 100]:
            with self.subTest(text=text):
                self.port.write_text(text)
                api = API()
                self.assertEqual(self.run_poll(api), "holding")
                self.assertEqual(api.updates, [{"current_network_interface": "lo"}])


if __name__ == "__main__":
    unittest.main()
