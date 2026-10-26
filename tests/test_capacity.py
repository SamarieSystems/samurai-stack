"""Capacity floors and low-space actions use only owned, bounded endpoints."""
from contextlib import nullcontext
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from samurai_stack import capacity, capacity_guard as guard, engine, installation_registry, naming
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = default_config("192.168.50.21")
        self.c["compose_projects"] = {"2": "house-media", "3": "house-torrents"}
        self.app, self.data, self.units = self.root / "application", self.root / "data", self.root / "units"
        for path in (self.app, self.data, self.units):
            path.mkdir(mode=0o700)
        for name in ("state", "backups", "secrets"):
            (self.app / name).mkdir(mode=0o700)
        self.c["install_root"] = self.app.as_posix().removeprefix(self.app.drive)
        self.c["storage"] = {"mode": "local", "data_root": self.data.as_posix().removeprefix(self.data.drive),
                             "expected_mount": {"target": self.data.as_posix().removeprefix(self.data.drive), "source": "/dev/data", "fstype": "ext4"}}
        write_json(self.app / ".samurai-install.json", {"deployment_id": self.c["deployment_id"]})
        write_json(self.data / ".samurai-storage.json", {"deployment_id": self.c["deployment_id"], "expected_mount": self.c["storage"]["expected_mount"]}, mode=0o644)
        for name in ("qbit_username", "qbit_password"):
            path = self.app / "secrets" / name
            path.write_text("synthetic-only-capacity-fixture")
            path.chmod(0o600)
        self.enterContext(patch.object(guard, "owner"))  # UID-0 ownership boundary, not file/link/mode validation.
        self.enterContext(patch.object(engine, "linux_only"))
        self.enterContext(patch.object(engine, "host_identity", return_value="capacity-host"))
        self.enterContext(patch.object(guard, "host_id", return_value="capacity-host"))
        self.enterContext(patch.object(engine, "deployment_lock", return_value=nullcontext()))
        self.enterContext(patch.object(capacity.os, "geteuid", return_value=0, create=True))
        self.enterContext(patch.object(installation_registry, "REGISTRY_ROOT", self.root / "registry"))
        self.enterContext(patch.object(naming, "UNIT_ROOT", self.root / "application-units"))
        self.space = {"data": {"mount": self.c["storage"]["expected_mount"], "available_bytes": 105 * capacity.GIB, "size_bytes": 4000 * capacity.GIB},
                      "app": {"mount": {"target": "/", "source": "/dev/app", "fstype": "ext4"}, "available_bytes": 101 * capacity.GIB, "size_bytes": 128 * capacity.GIB}}
        self.original_measure = guard.capacities
        self.enterContext(patch.object(guard, "capacities", side_effect=lambda *args: copy.deepcopy(self.space)))
        self.starr = {name: {"id": 1, "copyUsingHardlinks": True, "skipFreeSpaceCheckWhenImporting": True,
                             "minimumFreeSpaceWhenImporting": 100, "futureVendorField": {"kept": True}} for name in ("sonarr", "radarr")}
        self.starr["sonarr"]["skipFreeSpaceCheckWhenGrabbing"] = True
        self.sab = {"download_free": "500M", "complete_free": "", "fulldisk_autoresume": True, "unrelated": "keep"}
        self.calls, self.http = [], []
        self.foreign = self.bad_port = self.bad_namespace = self.bad_interface = self.bad_mount = False
        self.torrents = [{"hash": "a" * 40, "state": "downloading", "progress": 0.5, "amount_left": 123},
                         {"hash": "b" * 40, "state": "uploading", "progress": 1, "amount_left": 0},
                         {"hash": "c" * 40, "state": "stoppedDL", "progress": 0, "amount_left": 123}]

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        ids = {name: char * 64 for name, char in (("sonarr", "1"), ("radarr", "2"), ("sabnzbd", "3"), ("qbittorrent", "4"), ("gluetun", "5"))}
        if argv[:2] == ["ip", "-j"]:
            value = json.dumps([{"addr_info": [{"local": self.c["bind_address"]}]}])
        elif argv[:1] == ["systemctl"]:
            value = ""
        else:
            self.assertEqual(argv[:3], ["docker", "--context", "default"])
            args = argv[3:]
            if args[:2] == ["context", "inspect"]:
                value = "unix:///var/run/docker.sock"
            elif args[:2] == ["compose", "version"]:
                value = "2.30.0"
            elif args[:1] == ["info"]:
                value = "linux"
            elif args[:1] == ["exec"]:
                self.assertEqual(args[1], ids["gluetun"])
                self.assertEqual(args[2:], ["ip", "address", "show", "dev", "tun0"])
                value = "tun0 present"
            elif args[:1] == ["ps"]:
                service = next(item.split("=", 2)[-1] for item in args if item.startswith("label=com.docker.compose.service="))
                value = ids[service]
            elif args[:1] == ["inspect"]:
                service = next(name for name, identifier in ids.items() if identifier == args[1])
                stage = 3 if service in {"qbittorrent", "gluetun"} else 2
                port = {"sonarr": 8989, "radarr": 7878, "sabnzbd": 8080, "gluetun": 8081}.get(service)
                labels = {"systems.samarie.deployment": "foreign" if self.foreign else self.c["deployment_id"],
                          "systems.samarie.stage": str(stage), "com.docker.compose.project": "house-torrents" if stage == 3 else "house-media",
                          "com.docker.compose.service": service}
                ports = {str(port) + "/tcp": [{"HostIp": "0.0.0.0" if self.bad_port else self.c["bind_address"], "HostPort": str(port)}]}
                mounts = [{"Type": "bind", "Source": "/foreign" if self.bad_mount else str(Path(self.c["install_root"]) / "appdata" / service),
                           "Destination": "/gluetun" if service == "gluetun" else "/config"}]
                row = {"id": ids[service], "labels": labels, "ports": ports, "mounts": mounts,
                       "network": "bridge" if self.bad_namespace else "container:" + ids["gluetun"]}
                value = json.dumps(labels if args[-1] == "{{json .Config.Labels}}" else ports if args[-1] == "{{json .NetworkSettings.Ports}}" else row)
            else:
                self.fail("Unexpected command: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, value, "")

    def request(self, method, url, headers=None, json=None, form=None, **kwargs):
        self.http.append((method, url, copy.deepcopy(json), copy.deepcopy(form)))
        parsed = urlsplit(url)
        if parsed.port in {8989, 7878}:
            service = "sonarr" if parsed.port == 8989 else "radarr"
            self.assertTrue(parsed.path.startswith("/api/v3/config/mediamanagement"))
            if method == "PUT":
                self.starr[service] = copy.deepcopy(json)
            return copy.deepcopy(self.starr[service])
        if parsed.port == 8080:
            values = parse_qs(parsed.query)
            if values["mode"] == ["set_config"]:
                key, value = values["keyword"][0], values["value"][0]
                self.sab[key] = int(value) if key == "fulldisk_autoresume" else value
            return {"config": {"misc": copy.deepcopy(self.sab)}}
        self.assertEqual(parsed.port, 8081)
        endpoint = parsed.path.removeprefix("/api/v2/")
        if endpoint == "auth/login":
            return "Ok."
        if endpoint == "app/preferences":
            return {"current_network_interface": "eth0" if self.bad_interface else "tun0",
                    "bypass_local_auth": False, "bypass_auth_subnet_whitelist_enabled": False}
        if endpoint == "app/version":
            return "v5.1.0"
        if endpoint == "torrents/info":
            return copy.deepcopy(self.torrents)
        if endpoint == "torrents/stop":
            for row in self.torrents:
                if row["hash"] in form["hashes"].split("|"):
                    row["state"] = "stoppedDL"
            return ""
        self.fail("Unexpected API action: " + method + " " + endpoint)

    def plan(self, **kwargs):
        return capacity.plan(self.c, runner=self.runner, request=self.request, secret_resolver=lambda name: "synthetic-key", unit_root=self.units, **kwargs)

    def apply(self, document):
        return capacity.apply(self.c, document, runner=self.runner, request=self.request, secret_resolver=lambda name: "synthetic-key", unit_root=self.units)

    def test_review_is_readonly_and_reports_two_filesystems_and_native_changes(self):
        document = self.plan()
        self.assertEqual(document["data_reserve_bytes"], 50 * capacity.GIB)
        self.assertEqual(document["app_reserve_bytes"], 20 * capacity.GIB)
        self.assertEqual(document["native"]["sonarr"]["after"]["minimumFreeSpaceWhenImporting"], 51200)
        self.assertFalse(document["native"]["sonarr"]["after"]["skipFreeSpaceCheckWhenGrabbing"])
        self.assertNotIn("skipFreeSpaceCheckWhenGrabbing", document["native"]["radarr"]["after"])
        self.assertFalse(document["native"]["sabnzbd"]["after"]["fulldisk_autoresume"])
        self.assertTrue(all(row[0] == "GET" for row in self.http))
        self.assertFalse(capacity.policy_path(self.c).exists())

    def test_apply_and_retry_preserve_unrelated_fields_and_stricter_floors(self):
        self.sab["download_free"] = "100G"
        self.starr["radarr"]["minimumFreeSpaceWhenImporting"] = 102400
        result = self.apply(self.plan())
        self.assertTrue(result["ok"])
        self.assertEqual(self.sab["download_free"], "100G")
        self.assertEqual(self.sab["complete_free"], str(50 * capacity.GIB))
        self.assertEqual(self.sab["unrelated"], "keep")
        self.assertEqual(self.starr["radarr"]["minimumFreeSpaceWhenImporting"], 102400)
        for row in self.starr.values():
            self.assertEqual(row["futureVendorField"], {"kept": True})
            self.assertTrue(row["copyUsingHardlinks"])
        self.http.clear()
        self.assertTrue(self.apply(self.plan())["ok"])
        self.assertFalse(any(row[0] == "PUT" or "mode=set_config" in row[1] for row in self.http))

    def test_stale_native_settings_reject_before_backup_or_writes(self):
        reviewed = self.plan()
        self.starr["radarr"]["futureVendorField"] = "changed by someone else"
        self.http.clear()
        with self.assertRaisesRegex(SafetyError, "changed after review"):
            self.apply(reviewed)
        self.assertTrue(all(row[0] == "GET" for row in self.http))
        self.assertEqual(list((self.app / "backups").iterdir()), [])

    def test_foreign_target_never_receives_capacity_credentials_or_writes(self):
        self.foreign = True
        with self.assertRaises(SafetyError):
            self.plan(qbit_guard=True)
        self.assertEqual(self.http, [])

    def test_guard_requires_owned_namespace_binding_interface_and_appdata(self):
        for field in ("bad_port", "bad_namespace", "bad_interface", "bad_mount"):
            with self.subTest(field=field):
                setattr(self, field, True)
                with self.assertRaises(SafetyError):
                    self.plan(qbit_guard=True)
                setattr(self, field, False)
        self.assertFalse(any(row[0] == "PUT" or row[1].endswith("/torrents/stop") for row in self.http))

    def test_low_space_stops_only_incomplete_hashes_and_preserves_seeders_and_manual_stops(self):
        self.assertTrue(self.apply(self.plan(qbit_guard=True))["ok"])
        self.space["data"]["available_bytes"] = 49 * capacity.GIB
        self.http.clear()
        result = guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=self.request)
        self.assertEqual(result["torrents_stopped"], 1)
        self.assertEqual(result["low_filesystems"], ["data"])
        stopped = [row for row in self.http if row[1].endswith("/torrents/stop")]
        self.assertEqual(stopped[0][3], {"hashes": "a" * 40})
        self.assertEqual([row["state"] for row in self.torrents], ["stoppedDL", "uploading", "stoppedDL"])
        self.assertFalse(any("setPreferences" in row[1] or "delete" in row[1] or "resume" in row[1] or "start" in row[1] for row in self.http))
        self.space["data"]["available_bytes"] = 100 * capacity.GIB
        self.http.clear()
        guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=self.request)
        self.assertEqual(self.http, [])
        self.assertEqual(self.torrents[0]["state"], "stoppedDL")

    def test_low_app_space_also_stops_downloads_and_keeps_units_numeric(self):
        self.apply(self.plan(qbit_guard=True))
        self.space["app"]["available_bytes"] = 19 * capacity.GIB
        result = guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=self.request)
        self.assertEqual(result["low_filesystems"], ["app"])
        self.assertEqual(sorted(path.name for path in self.units.iterdir()),
                         ["samurai-homelab-capacity-guard.service", "samurai-homelab-capacity-guard.timer"])
        self.assertFalse(any("restart" in argv or "samurai-homelab-3.service" in argv for argv in self.calls))

    def test_unknown_owned_timer_or_policy_identity_is_not_overwritten(self):
        unit = self.units / "samurai-homelab-capacity-guard.timer"
        unit.write_text("someone else's unit\n")
        unit.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, "exact generated owned"):
            self.plan(qbit_guard=True)
        self.assertEqual(unit.read_text(), "someone else's unit\n")

    def test_available_bytes_can_change_without_approving_changed_native_policy(self):
        reviewed = self.plan()
        self.space["data"]["available_bytes"] -= capacity.GIB
        self.assertTrue(self.apply(reviewed)["ok"])

    def test_invalid_or_completed_torrent_rows_never_expand_stop_authority(self):
        self.assertEqual(guard.incomplete_hashes([{"hash": "a" * 40, "state": "downloading", "progress": 1, "amount_left": 0}]), [])
        for change in ({"hash": "all"}, {"progress": None}, {"amount_left": -1}):
            row = {"hash": "a" * 40, "state": "downloading", "progress": 0.5, "amount_left": 1, **change}
            with self.assertRaises(guard.GuardError):
                guard.incomplete_hashes([row])

    def test_native_readback_failure_keeps_backup_and_does_not_claim_saved_policy(self):
        reviewed = self.plan()
        def ignore_put(method, url, **kwargs):
            return self.request('GET' if method == 'PUT' else method, url, **kwargs)
        with self.assertRaisesRegex(SafetyError, 'readback differs'):
            capacity.apply(self.c, reviewed, runner=self.runner, request=ignore_put,
                           secret_resolver=lambda name: 'synthetic-key', unit_root=self.units)
        self.assertFalse(capacity.policy_path(self.c).exists())
        self.assertEqual(len(list((self.app / 'backups').iterdir())), 1)

    def test_stop_failure_retains_private_intent_and_never_attempts_resume(self):
        self.apply(self.plan(qbit_guard=True))
        self.space['data']['available_bytes'] = 1
        def ignore_stop(method, url, **kwargs):
            if url.endswith('/torrents/stop'):
                return ''
            return self.request(method, url, **kwargs)
        with self.assertRaisesRegex(guard.GuardError, 'stop readback'):
            guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=ignore_stop)
        saved = json.loads((self.app / 'state' / 'capacity-guard-status.json').read_text())
        self.assertEqual(saved['paused_hashes'], ['a' * 40])
        self.assertFalse(saved['automatic_resume'])
        self.assertEqual(self.torrents[1]['state'], 'uploading')

    def test_torrent_completed_during_proof_is_not_stopped(self):
        self.apply(self.plan(qbit_guard=True))
        self.space['data']['available_bytes'] = 1
        def completed(method, url, **kwargs):
            if url.endswith('/app/version'):
                self.torrents[0].update(state='uploading', progress=1, amount_left=0)
            return self.request(method, url, **kwargs)
        self.http.clear()
        result = guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=completed)
        self.assertEqual(result['torrents_stopped'], 0)
        self.assertFalse(any(row[1].endswith('/torrents/stop') for row in self.http))

    def test_installed_companion_is_standalone_under_isolated_python(self):
        self.apply(self.plan(qbit_guard=True))
        empty = self.root / 'empty'
        empty.mkdir()
        result = subprocess.run([sys.executable, '-I', str(self.app / 'capacity_guard.py'), '--help'], cwd=empty,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('policy', result.stdout)
        self.assertEqual(list(empty.iterdir()), [])
        for unit in self.units.iterdir():
            self.assertNotIn('synthetic-only-capacity-fixture', unit.read_text())

    def test_changed_policy_host_and_application_mount_refuse_before_controls(self):
        self.apply(self.plan(qbit_guard=True))
        self.http.clear()
        self.space['app']['mount']['source'] = '/dev/replaced'
        with self.assertRaisesRegex(guard.GuardError, 'mount identity changed'):
            guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=self.request)
        self.assertEqual(self.http, [])
        with patch.object(guard, 'host_id', return_value='another-host'), self.assertRaisesRegex(guard.GuardError, 'another host'):
            guard.run_policy(capacity.policy_path(self.c), runner=self.runner, request=self.request)

    def test_live_capacity_measurement_checks_mount_marker_and_available_not_root_reserved_bytes(self):
        def mounts(argv, **kwargs):
            kind = 'data' if Path(argv[3]).name == 'data' else 'app'
            row = {**self.space[kind]['mount'], 'options': 'rw,relatime'}
            return subprocess.CompletedProcess(argv, 0, json.dumps({'filesystems': [row]}), '')
        from types import SimpleNamespace
        values = SimpleNamespace(f_bavail=10, f_bfree=99, f_frsize=1024, f_blocks=100)
        with patch.object(guard.os, 'statvfs', return_value=values, create=True):
            measured = self.original_measure(self.c, mounts)
        self.assertEqual(measured['data']['available_bytes'], 10240)
        self.assertEqual(measured['app']['available_bytes'], 10240)
        write_json(self.data / '.samurai-storage.json', {'deployment_id': 'foreign'}, replace=True, mode=0o644)
        with patch.object(guard.os, 'statvfs', return_value=values, create=True), self.assertRaisesRegex(guard.GuardError, 'ownership differs'):
            self.original_measure(self.c, mounts)

    def test_invalid_reserves_and_unreadable_custom_source_fail_before_any_writes(self):
        for value in (0, -1, True, 1.5, 1048577):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                self.plan(data_reserve_gib=value)
        script = self.app / 'capacity_guard.py'
        script.write_text('unrelated public source\n')
        script.chmod(0o600)
        with self.assertRaisesRegex(SafetyError, 'watcher source differs'):
            self.plan(qbit_guard=True)
        self.assertEqual(script.read_text(), 'unrelated public source\n')

    def test_known_owned_predecessor_upgrade_is_reviewed_backed_up_and_idempotent(self):
        self.apply(self.plan(qbit_guard=True))
        script = self.app / 'capacity_guard.py'
        predecessor = '# Synthetic released predecessor for upgrade transaction test.\n'
        script.write_bytes(predecessor.encode())
        old_sha = capacity._source_sha(predecessor)
        with patch.object(capacity, 'WATCHER_PREDECESSORS', frozenset({old_sha})):
            self.http.clear()
            reviewed = self.plan(qbit_guard=True)
            self.assertEqual(reviewed['watcher_source'], {
                'before_sha256': old_sha, 'after_sha256': capacity._source_sha(Path(guard.__file__).read_text()),
                'action': 'upgrade_known_predecessor'})
            self.assertEqual(script.read_text(), predecessor)
            self.assertTrue(all(row[0] == 'GET' or row[1].endswith('/auth/login') for row in self.http))
            self.assertTrue(self.apply(reviewed)['ok'])
            backup = self.app / 'backups' / ('capacity-watcher-before-' + reviewed['review_hash'] + '.py')
            self.assertEqual(backup.read_text(), predecessor)
            if os.name == 'posix':
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(script.read_text(), Path(guard.__file__).read_text())
            retried = self.plan(qbit_guard=True)
            self.assertEqual(retried['watcher_source']['action'], 'keep')
            self.assertTrue(self.apply(retried)['ok'])
            self.assertEqual([backup], list((self.app / 'backups').glob('capacity-watcher-before-*.py')))

    def test_known_source_without_an_existing_owned_policy_and_units_is_never_adopted(self):
        script = self.app / 'capacity_guard.py'
        predecessor = '# Synthetic predecessor is not proof of installation ownership.\n'
        script.write_bytes(predecessor.encode()); script.chmod(0o600)
        with patch.object(capacity, 'WATCHER_PREDECESSORS', frozenset({capacity._source_sha(predecessor)})):
            with self.assertRaisesRegex(SafetyError, 'supported owned predecessor'):
                self.plan(qbit_guard=True)
        self.assertEqual(self.http, [])
        self.assertEqual(list((self.app / 'backups').iterdir()), [])
        self.assertEqual(script.read_text(), predecessor)

    def test_changed_predecessor_after_review_refuses_before_native_or_source_writes(self):
        self.apply(self.plan(qbit_guard=True))
        script = self.app / 'capacity_guard.py'
        predecessor = '# Synthetic released predecessor for stale review test.\n'
        script.write_bytes(predecessor.encode())
        with patch.object(capacity, 'WATCHER_PREDECESSORS', frozenset({capacity._source_sha(predecessor)})):
            reviewed = self.plan(qbit_guard=True)
            changed = predecessor + '# An administrator edited the installed watcher.\n'
            script.write_bytes(changed.encode())
            before_policy = capacity.policy_path(self.c).read_bytes()
            self.http.clear(); self.calls.clear()
            with self.assertRaisesRegex(SafetyError, 'supported owned predecessor'):
                self.apply(reviewed)
            self.assertEqual(self.http, [])
            self.assertEqual(script.read_text(), changed)
            self.assertEqual(capacity.policy_path(self.c).read_bytes(), before_policy)
            self.assertEqual([], list((self.app / 'backups').glob('capacity-watcher-before-*.py')))

    def test_missing_generated_unit_does_not_authorize_a_known_predecessor_upgrade(self):
        self.apply(self.plan(qbit_guard=True))
        script = self.app / 'capacity_guard.py'
        predecessor = '# Synthetic predecessor with missing generated service.\n'
        script.write_bytes(predecessor.encode())
        (self.units / 'samurai-homelab-capacity-guard.service').unlink()
        with patch.object(capacity, 'WATCHER_PREDECESSORS', frozenset({capacity._source_sha(predecessor)})):
            with self.assertRaisesRegex(SafetyError, 'supported owned predecessor'):
                self.plan(qbit_guard=True)
        self.assertEqual(script.read_text(), predecessor)

    def test_wrong_installation_marker_stops_before_any_endpoint_credentials(self):
        write_json(self.app / '.samurai-install.json', {'deployment_id': 'foreign'}, replace=True)
        with self.assertRaisesRegex(SafetyError, 'does not own'):
            self.plan(qbit_guard=True)
        self.assertEqual(self.http, [])

    def test_current_qbit_empty_login_body_requires_fresh_sid_and_authenticated_readback(self):
        expected = 'fixture-capacity-http-login'  # release-audit: synthetic - local HTTP fixture only
        session_name = 'QBT_SID_8081'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):pass
            def do_POST(self):
                supplied = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                if supplied.get('password') != [expected]:
                    self.send_response(401); self.end_headers(); return
                self.send_response(204)
                self.send_header('Set-Cookie', session_name + '=fixture-private-session; HttpOnly; Path=/')
                self.end_headers()
            def do_GET(self):
                if self.headers.get('Cookie') != session_name + '=fixture-private-session':
                    self.send_response(403); self.end_headers(); return
                payload = json.dumps({'current_network_interface': 'tun0', 'bypass_local_auth': False,
                                      'bypass_auth_subnet_whitelist_enabled': False}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original = guard.Transport
        class LocalTransport(original):
            def __call__(self, method, url, **kwargs):
                target = 'http://127.0.0.1:' + str(server.server_port) + urlsplit(url).path
                return super().__call__(method, target, **kwargs)
        with patch.object(guard, 'Transport', LocalTransport):
            for session_name in ('SID', 'QBT_SID_8081'):
                with self.subTest(session_name=session_name):
                    api = guard.qbit_backend(self.c, self.runner, credentials=lambda name: expected)
                    self.assertEqual(api('app/preferences')['current_network_interface'], 'tun0')
                    # A second backend must not inherit the first client's cookie.
                    with self.assertRaises(guard.GuardError):
                        guard.qbit_backend(self.c, self.runner, credentials=lambda name: 'fixture-wrong')

    def test_qbit_login_body_alone_never_accepts_bypassed_or_missing_preferences(self):
        for reply in ('Ok.', ''):
            for prefs in ({}, {'current_network_interface': 'tun0'},
                          {'current_network_interface': 'tun0', 'bypass_local_auth': True,
                           'bypass_auth_subnet_whitelist_enabled': False}):
                with self.subTest(reply=reply, prefs=prefs):
                    def request(method, url, **kwargs):
                        return reply if url.endswith('/auth/login') else prefs
                    with self.assertRaises(guard.GuardError):
                        guard.qbit_backend(self.c, self.runner, request, lambda name: 'fixture-only')

    def test_default_backend_never_uses_engine_global_cookie_session(self):
        with patch.object(guard, 'qbit_backend') as backend, patch.object(capacity, 'Inventory') as inventory:
            inventory.return_value.public.return_value = {}
            capacity.plan(self.c, qbit_guard=True, runner=self.runner, unit_root=self.units)
        self.assertIsNone(backend.call_args.args[2])

    @unittest.skipUnless(os.name == 'posix', 'Control directory mode enforcement is POSIX-only')
    def test_public_control_directory_is_not_silently_repaired(self):
        state = self.app / 'state'
        state.chmod(0o755)
        try:
            with self.assertRaisesRegex(SafetyError, 'private to root'):
                self.plan(qbit_guard=True)
            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
        finally:
            state.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
