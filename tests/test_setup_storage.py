import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError
from samurai_stack import setup_storage as setup


class FakeHost:
    def __init__(self):
        self.calls = []
        self.rows = [{"target": "/", "source": "/dev/fixture-root", "fstype": "ext4", "options": "rw,relatime"}]
        self.on_mount = None
        self.mount_error = False
        self.interfaces = [
            {"ifname": "eth1", "addr_info": [{"local": "192.168.50.20"}]},
            {"ifname": "eth0", "addr_info": [{"local": "192.168.10.20"}]},
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]},
        ]

    def __call__(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append(args)
        if args[:2] == ["ip", "-j"]:
            data = [{"dev": "eth0"}] if "route" in args else self.interfaces
        elif args[0] == "findmnt":
            if "--target" in args:
                target = Path(args[args.index("--target") + 1])
                choices = [row for row in self.rows if row["target"] == "/" or target == Path(row["target"]) or Path(row["target"]) in target.parents]
                data = {"filesystems": [max(choices, key=lambda row: len(row["target"]))]}
            else:
                data = {"filesystems": self.rows}
        elif args[0] == "mount":
            if self.mount_error:
                raise SafetyError("fixture mount failure")
            self.rows.append({"target": args[-1], "source": args[-2], "fstype": "nfs4", "options": "rw,hard,vers=4.1"})
            if self.on_mount:
                self.on_mount()
            data = None
        elif args == ["systemctl", "daemon-reload"]:
            data = None
        else:
            raise AssertionError("Unexpected command: " + repr(args))
        return subprocess.CompletedProcess(args, 0, "" if data is None else json.dumps(data), "")


class InputAnswers:
    def __init__(self, values):
        self.values = iter(values)
        self.prompts = []

    def __call__(self, question):
        self.prompts.append(question)
        try:
            return next(self.values)
        except StopIteration:
            raise AssertionError("Unexpected prompt: " + question) from None


class StorageDiscovery(unittest.TestCase):
    def test_lan_address_prefers_route_and_excludes_container_and_loopback(self):
        host = FakeHost()
        self.assertEqual(setup.discover_lan_addresses(host), [("192.168.10.20", "eth0"), ("192.168.50.20", "eth1")])
        self.assertFalse(any(call[0] != "ip" for call in host.calls))

    def test_bad_nas_inputs_cannot_become_mount_or_fstab_arguments(self):
        for value in ["http://nas", "nas;touch", "-o", "host/user", "0.0.0.0", "8.8.8.8", "nas\nother"]:
            with self.subTest(value=value), self.assertRaises(SafetyError):
                setup._nas_server(value)
        for value in ["/", "/export/../photos", "/export with spaces", "/export\\040other", "/export;echo", "relative"]:
            with self.subTest(value=value), self.assertRaises(SafetyError):
                setup._export_path(value)
        self.assertEqual(setup._nas_server("nas.example"), "nas.example")
        self.assertEqual(setup._export_path("/volume1/media"), "/volume1/media")

    def test_fstab_conflict_decodes_escaped_mountpoint(self):
        with self.assertRaises(SafetyError):
            setup._check_fstab("other:/data /mnt/\\155edia nfs4 rw,hard 0 0\n", PurePosixPath("/mnt/media"), "nas:/media")

    def test_fstab_soft_readonly_and_duplicate_entries_are_not_replaced(self):
        for text in ["nas:/media /mnt/media nfs4 rw,soft 0 0\n", "nas:/media /mnt/media nfs4 ro,hard 0 0\n",
                     "nas:/media /mnt/media nfs4 rw,hard 0 0\nnas:/media /mnt/media nfs4 rw,hard 0 0\n"]:
            with self.subTest(text=text), self.assertRaises(SafetyError):
                setup._check_fstab(text, PurePosixPath("/mnt/media"), "nas:/media")

    def test_root_media_id_and_typos_reprompt(self):
        read = InputAnswers(["0", "not-an-id", "1001"])
        self.assertEqual(setup._media_id(read, lambda _: None, "UID"), 1001)
        self.assertEqual(len(read.prompts), 3)

    def test_compose_names_offer_role_defaults_and_allow_custom_names(self):
        config = default_config()
        answers = InputAnswers(["friends-media", "", "ops", "", ""])
        setup._compose_names(config, answers, lambda _: None)
        self.assertEqual(config["compose_projects"], {
            "2": "friends-media", "3": "homelab-torrents", "4": "ops",
            "5": "homelab-photos", "6": "homelab-music",
        })
        self.assertEqual(len(answers.prompts), 5)

    def test_compose_names_reprompt_invalid_or_duplicate_without_losing_choices(self):
        config = default_config()
        answers = InputAnswers(["../invalid", "media", "media", "torrents", "", "", ""])
        output = []
        setup._compose_names(config, answers, output.append)
        self.assertEqual(config["compose_projects"]["2"], "media")
        self.assertEqual(config["compose_projects"]["3"], "torrents")
        self.assertTrue(any("lowercase" in line for line in output))
        self.assertTrue(any("different project name" in line for line in output))

    def test_existing_site_is_reused_without_probes_prompts_or_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "site.json"
            original = json.dumps(default_config()) + "\n"
            path.write_text(original)
            result = setup.ensure_site_config(path, input_fn=lambda _: self.fail("prompt"), runner=lambda *_: self.fail("command"), output_fn=lambda _: None)
            self.assertEqual(result["deployment_id"], json.loads(original)["deployment_id"])
            self.assertEqual(path.read_text(), original)

    def test_invalid_saved_site_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "site.json"
            original = '{"schema_version": 99}'
            path.write_text(original)
            with self.assertRaises(SafetyError):
                setup.ensure_site_config(path, input_fn=lambda _: self.fail("prompt"), output_fn=lambda _: None)
            self.assertEqual(path.read_text(), original)


@unittest.skipUnless(os.name == "posix", "Guided mount/config filesystem tests require Linux paths")
class GuidedFilesystem(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fstab = self.root / "fstab"
        self.original = "# existing mounts\nUUID=fixture-root / ext4 defaults 0 1\n"
        self.fstab.write_text(self.original)
        self.fstab.chmod(0o644)
        self.host = FakeHost()
        self.output = []

    def connect(self, answers):
        with patch.object(os, "geteuid", return_value=0):
            return setup.connect_nfs(input_fn=InputAnswers(answers), output_fn=self.output.append, runner=self.host, fstab_path=self.fstab)

    def test_success_mounts_then_persists_actual_type_and_private_original_backup(self):
        target = self.root / "media"
        found = self.connect(["nas.example", "/export/media", str(target), "yes"])
        self.assertEqual(found["fstype"], "nfs4")
        self.assertTrue(target.is_dir())
        text = self.fstab.read_text()
        self.assertTrue(text.startswith(self.original))
        self.assertIn(f"nas.example:/export/media {target} nfs4 rw,hard,_netdev,nofail,x-systemd.mount-timeout=30s 0 0", text)
        backups = list(self.root.glob("fstab.samurai-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.fstab.stat().st_mode), 0o644)
        self.assertFalse(Path(str(self.fstab) + ".samurai.lock").exists())
        self.assertEqual([call[0] for call in self.host.calls if call[0] not in {"findmnt"}], ["mount", "systemctl"])

    def test_decline_does_not_create_mountpoint_or_change_fstab(self):
        target = self.root / "media"
        with self.assertRaisesRegex(SafetyError, "cancelled"):
            self.connect(["nas.example", "/export/media", str(target), "no"])
        self.assertFalse(target.exists())
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertFalse(any(call[0] in {"mount", "systemctl"} for call in self.host.calls))

    def test_nonempty_target_is_refused_without_hiding_files(self):
        target = self.root / "media"
        target.mkdir()
        sentinel = target / "family-photo"
        sentinel.write_bytes(b"preserve fixture")
        with self.assertRaisesRegex(SafetyError, "stopped"):
            self.connect(["nas.example", "/export/media", str(target), "no"])
        self.assertEqual(sentinel.read_bytes(), b"preserve fixture")
        self.assertFalse(any(call[0] == "mount" for call in self.host.calls))
        self.assertEqual(self.fstab.read_text(), self.original)

    def test_existing_foreign_mount_is_refused(self):
        target = self.root / "media"
        target.mkdir()
        self.host.rows.append({"target": str(target), "source": "other:/photos", "fstype": "nfs4", "options": "rw,hard"})
        with self.assertRaisesRegex(SafetyError, "stopped"):
            self.connect(["nas.example", "/export/media", str(target), "no"])
        self.assertFalse(any(call[0] == "mount" for call in self.host.calls))
        self.assertEqual(self.fstab.read_text(), self.original)

    def test_failed_mount_never_adds_fstab_entry(self):
        target = self.root / "media"
        self.host.mount_error = True
        with self.assertRaisesRegex(SafetyError, "fstab was not changed"):
            self.connect(["nas.example", "/export/media", str(target), "yes"])
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertFalse(list(self.root.glob("fstab.samurai-backup-*")))

    def test_fstab_changed_while_mounting_is_preserved_and_not_overwritten(self):
        target = self.root / "media"
        changed = self.original + "# operator concurrent edit\n"
        self.host.on_mount = lambda: self.fstab.write_text(changed)
        with self.assertRaisesRegex(SafetyError, "fstab changed"):
            self.connect(["nas.example", "/export/media", str(target), "yes"])
        self.assertEqual(self.fstab.read_text(), changed)
        self.assertFalse(list(self.root.glob("fstab.samurai-backup-*")))

    def test_matching_existing_fstab_entry_is_not_duplicated(self):
        target = self.root / "media"
        original = self.original + f"nas.example:/export/media {target} nfs4 rw,hard,_netdev 0 0\n"
        self.fstab.write_text(original)
        self.connect(["nas.example", "/export/media", str(target), "yes"])
        self.assertEqual(self.fstab.read_text(), original)
        self.assertFalse(list(self.root.glob("fstab.samurai-backup-*")))

    def test_new_config_default_torrents_only_and_confirmation_before_private_write(self):
        self.host.interfaces = self.host.interfaces[1:2]
        app = self.root / "app"
        data = self.root / "data"
        path = self.root / "site.json"
        answers = InputAnswers(["", str(app), "Etc/UTC", "", "2", str(data), "", "", "", "", "", "", "", "yes"])
        config = setup.ensure_site_config(path, input_fn=answers, output_fn=self.output.append, runner=self.host, fstab_path=self.fstab)
        self.assertEqual(config["role"], "all")
        self.assertEqual(config["hardware"]["acceleration"], "cpu")
        self.assertEqual(config["downloads"]["prefer"], "torrents")
        self.assertFalse(config["features"]["usenet"])
        self.assertTrue(config["features"]["torrents"])
        self.assertEqual('homelab-media', config['compose_projects']['2'])
        self.assertEqual('homelab-torrents', config['compose_projects']['3'])
        self.assertFalse(config["features"]["photos"] or config["features"]["music"])
        self.assertEqual(config["storage"]["expected_mount"]["source"], "/dev/fixture-root")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(app.exists() or data.exists())
        self.assertFalse(any(call[0] == "mount" for call in self.host.calls))

    def test_declined_config_writes_no_file_or_data_tree(self):
        self.host.interfaces = self.host.interfaces[1:2]
        app, data, path = self.root / "app", self.root / "data", self.root / "site.json"
        answers = InputAnswers(["", str(app), "Etc/UTC", "", "2", str(data), "", "", "", "", "", "", "", "no"])
        with self.assertRaisesRegex(SafetyError, "without saving"):
            setup.ensure_site_config(path, input_fn=answers, output_fn=self.output.append, runner=self.host)
        self.assertFalse(path.exists() or data.exists() or app.exists())

    def test_mounted_nas_selection_reuses_exact_identity_without_export_prompts(self):
        self.host.interfaces = self.host.interfaces[1:2]
        nas = self.root / "nas"
        nas.mkdir()
        app, data, path = self.root / "app", nas / "samurai", self.root / "site.json"
        self.host.rows.append({"target": str(nas), "source": "nas.example:/export/media", "fstype": "nfs4", "options": "rw,hard"})
        self.fstab.write_text(self.original + f"nas.example:/export/media {nas} nfs4 rw,hard,_netdev 0 0\n")
        before = self.fstab.read_text()
        answers = InputAnswers(["", str(app), "Etc/UTC", "", "1", "1", str(data), "", "", "", "", "", "", "", "yes"])
        config = setup.ensure_site_config(path, input_fn=answers, output_fn=self.output.append, runner=self.host, fstab_path=self.fstab)
        self.assertEqual(config["storage"]["expected_mount"], {"target": str(nas), "source": "nas.example:/export/media", "fstype": "nfs4"})
        self.assertFalse(any("NAS address" in prompt for prompt in answers.prompts))
        self.assertFalse(any(call[0] == "mount" for call in self.host.calls))
        self.assertEqual(self.fstab.read_text(), before)

    def test_mounted_nas_without_fstab_resumes_persistence_without_remount(self):
        nas = self.root / "nas"
        nas.mkdir()
        self.host.rows.append({"target": str(nas), "source": "nas.example:/export/media", "fstype": "nfs4", "options": "rw,hard"})
        with patch.object(os, "geteuid", return_value=0):
            found = setup._choose_nfs(InputAnswers(["1", "yes"]), self.output.append, self.host, self.fstab)
        self.assertEqual(found["target"], str(nas))
        self.assertIn(f"nas.example:/export/media {nas} nfs4", self.fstab.read_text())
        backups = list(self.root.glob("fstab.samurai-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertFalse(any(call[0] == "mount" for call in self.host.calls))

    def test_decline_mounted_nas_persistence_leaves_it_mounted_and_setup_incomplete(self):
        nas = self.root / "nas"
        nas.mkdir()
        row = {"target": str(nas), "source": "nas.example:/export/media", "fstype": "nfs4", "options": "rw,hard"}
        self.host.rows.append(row)
        with self.assertRaisesRegex(SafetyError, "Persistent NAS setup is incomplete"):
            setup._choose_nfs(InputAnswers(["1", "no"]), self.output.append, self.host, self.fstab)
        self.assertIn(row, self.host.rows)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertFalse(any(call[0] in {"mount", "umount", "systemctl"} for call in self.host.calls))

    def test_owned_partial_install_is_not_adopted_with_a_new_uuid(self):
        app = self.root / "app"
        app.mkdir()
        marker = app / ".samurai-install.json"
        marker.write_text('{"deployment_id":"fixture-old"}')
        with self.assertRaisesRegex(SafetyError, "Recover its original site.json"):
            setup._new_directory(InputAnswers([str(app)]), self.output.append, "Apps", str(app), installation=True)
        self.assertEqual(marker.read_text(), '{"deployment_id":"fixture-old"}')

    def test_nonempty_data_choice_requires_new_path_without_touching_sentinel(self):
        occupied = self.root / "occupied"
        occupied.mkdir()
        sentinel = occupied / "family-photo"
        sentinel.write_bytes(b"untouched")
        fresh = self.root / "new"
        found = setup._new_directory(InputAnswers([str(occupied), str(fresh)]), self.output.append, "Data", str(occupied))
        self.assertEqual(found, fresh)
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertFalse(fresh.exists())

