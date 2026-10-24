import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from samurai_stack.safety import SafetyError


path = Path(__file__).resolve().parent.parent / "scripts" / "commission-apps.py"
spec = importlib.util.spec_from_file_location("commission_apps", path)
commission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commission)


class CommissionTests(unittest.TestCase):
    def setUp(self):
        self.config = {"deployment_id": "disposable-fixture", "bind_address": "192.168.1.50",
                       "hosts": {"media": "192.168.1.50", "photos": "192.168.1.50"},
                       "features": {"usenet": False}}

    def run_guard(self, *, identity="disposable-fixture", confirmation="disposable-fixture", mode=0o100600):
        marker = MagicMock()
        marker.stat.return_value = SimpleNamespace(st_mode=mode, st_uid=0, st_nlink=1)
        with patch.object(commission.sys, "platform", "linux"), \
                patch.object(commission.os, "geteuid", return_value=0, create=True), \
                patch.object(commission, "no_symlinks"), \
                patch.object(commission, "private_read", return_value=identity), \
                patch.object(commission.socket, "socket") as socket:
            commission.guard(self.config, confirmation, marker)
            socket.return_value.__enter__.return_value.bind.assert_called_once_with(("192.168.1.50", 0))

    def test_guard_requires_exact_disposable_identity_and_local_bind(self):
        self.run_guard()
        with self.assertRaises(SafetyError):
            self.run_guard(identity="other-deployment")
        with self.assertRaises(SafetyError):
            self.run_guard(confirmation="other-deployment")

    def test_guard_rejects_readable_marker_or_cross_host_targets(self):
        with self.assertRaises(SafetyError):
            self.run_guard(mode=0o100644)
        self.config["hosts"]["media"] = "192.168.1.51"
        with self.assertRaises(SafetyError):
            self.run_guard()

    def test_guard_rejects_non_linux_before_any_request(self):
        with patch.object(commission.sys, "platform", "win32"), \
                patch.object(commission.engine, "request") as request:
            with self.assertRaises(SafetyError):
                commission.guard(self.config, "disposable-fixture")
            request.assert_not_called()

    def test_existing_secret_is_never_rotated_or_overwritten(self):
        secret_path = MagicMock()
        secret_path.exists.return_value = True
        with patch.object(commission.engine, "secret_path", return_value=secret_path), \
                patch.object(commission, "private_read", return_value="persisted-value\n"), \
                patch.object(commission, "atomic_write") as write:
            self.assertEqual(commission.secret(self.config, "fixture"), "persisted-value")
            with self.assertRaises(SafetyError):
                commission.secret(self.config, "fixture", "different-value")
            write.assert_not_called()

    def test_error_report_does_not_echo_vendor_exception_or_secret(self):
        with patch.object(commission, "jellyfin", side_effect=RuntimeError("sentinel-private-value")), \
                patch.object(commission, "starr", return_value={"forms_auth_configured": True}), \
                patch.object(commission, "bazarr", return_value={"forms_auth_configured": True}), \
                patch.object(commission, "seerr", return_value={"admin_login": True}):
            report = commission.commission(self.config, 2)
        self.assertFalse(report["ok"])
        self.assertNotIn("sentinel-private-value", json.dumps(report))
        self.assertEqual(report["errors"][0]["service"], "jellyfin")

    def test_failed_identity_readback_does_not_pass(self):
        with self.assertRaises(SafetyError):
            commission.assert_fields({"isAdmin": False}, isAdmin=True)
        with self.assertRaises(SafetyError):
            commission.assert_fields("unexpected HTML", isAdmin=True)


if __name__ == "__main__":
    unittest.main()
