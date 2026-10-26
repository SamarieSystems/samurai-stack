"""Retire only an empty obsolete generated service, retaining every byte."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import retired_units as retirement
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError

OLD = '34d2f370-aec1-4ef9-983b-9c5bc4eb9fe2'


def generated(c, stage=2, old=OLD):
    project = c['deployment_name'] + '-' + str(stage)
    folder = c['install_root'] + '/stages/' + project
    command = '/usr/bin/docker --context default compose --project-name ' + project + ' --file ' + folder + '/compose.yaml'
    return ('# Managed by Samurai Stack: ' + old + '\n[Unit]\nDescription=Samurai Stack stage ' + str(stage) + '\n'
            'Requires=docker.service\nAfter=docker.service network-online.target\n[Service]\nType=simple\nUMask=0077\n'
            'ExecStartPre=/usr/bin/python3 ' + c['install_root'] + '/runtime_guard.py ' + folder + '/site.json ' + str(stage) + '\n'
            'ExecStart=' + command + ' up --no-color --abort-on-container-failure --pull never\n'
            'ExecStop=' + command + ' stop --timeout 45\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n')


class Runner:
    def __init__(self):
        self.calls = []
        self.endpoint = 'unix:///var/run/docker.sock'
        self.kind = 'linux'
        self.project_ids = ''
        self.owner_ids = ''
        self.active = 'inactive'
        self.other_active = 'inactive'
        self.after_disable = None
        self.fail_on = None
        self.fragment_path = None
        self.dropins = ''
        self.stale = 'no'
        self.load_state = 'not-found'

    def __call__(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append(args)
        if args == self.fail_on:
            raise RuntimeError('fixture-private-value-must-not-escape')
        if args[:6] == ['docker', '--context', 'default', 'context', 'inspect', 'default']:
            value = self.endpoint
        elif args[:5] == ['docker', '--context', 'default', 'info', '--format']:
            value = self.kind
        elif args[:5] == ['docker', '--context', 'default', 'ps', '--all']:
            value = self.project_ids if 'com.docker.compose.project=' in args[6] else self.owner_ids
        elif args[:2] == ['systemctl', 'show']:
            if '--property=FragmentPath' in args:
                value = self.fragment_path
            elif '--property=DropInPaths' in args:
                value = self.dropins
            elif '--property=NeedDaemonReload' in args:
                value = self.stale
            elif '--property=LoadState' in args:
                value = self.load_state
            else:
                value = self.active if args[2] == 'samurai-homelab-2.service' else self.other_active
        elif args[:2] == ['systemctl', 'disable']:
            if self.after_disable:
                self.after_disable()
            value = ''
        elif args[:2] in (['systemctl', 'stop'], ['systemctl', 'reset-failed']) or args == ['systemctl', 'daemon-reload']:
            value = ''
        else:
            raise AssertionError('Unexpected command: ' + repr(args))
        return SimpleNamespace(returncode=0, stdout=value, stderr='')


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.units, self.archives = self.root / 'units', self.root / 'archives'
        self.units.mkdir(mode=0o755)
        self.c = default_config('192.168.50.21')
        self.unit = self.units / 'samurai-homelab-2.service'
        self.unit.write_bytes(generated(self.c).encode())
        self.unit.chmod(0o644)
        self.original = self.unit.read_bytes()
        self.runner = Runner()
        self.runner.fragment_path = str(self.unit)
        guard = patch.object(retirement, '_guard')
        guard.start()
        self.addCleanup(guard.stop)
        if os.name == 'posix' and os.geteuid() != 0:
            owner = patch.object(retirement, '_owner')
            owner.start()
            self.addCleanup(owner.stop)

    def retire(self):
        return retirement.retire_conflicting(self.c, 2, runner=self.runner,
            unit_root=self.units, archive_root=self.archives)

    def mutations(self):
        return [c for c in self.runner.calls if c[0] == 'systemctl' and c[1] != 'show']

    def assert_untouched(self):
        self.assertEqual(self.unit.read_bytes(), self.original)
        self.assertFalse(any(self.archives.glob('*/*.service')))

    def test_empty_old_project_archives_exact_unit_bytes_and_mode_only(self):
        result = self.retire()
        path = Path(result['archive_path'])
        self.assertEqual(result['status'], 'obsolete_unit_archived')
        self.assertEqual(result['old_deployment_id'], OLD)
        self.assertEqual(result['unit_name'], self.unit.name)
        self.assertEqual(result['containers_removed'], 0)
        self.assertEqual(result['application_files_removed'], 0)
        self.assertEqual(path.read_bytes(), self.original)
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.mutations(), [['systemctl', 'stop', self.unit.name],
            ['systemctl', 'disable', self.unit.name], ['systemctl', 'daemon-reload'],
            ['systemctl', 'reset-failed', self.unit.name]])
        checks = [c for c in self.runner.calls if c[:5] == ['docker', '--context', 'default', 'ps', '--all']]
        self.assertEqual(len(checks), 4)
        self.assertEqual(sum('systems.samarie.deployment=' + OLD in c[6] for c in checks), 2)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertFalse(any(set(c) & {'rm', 'down', 'exec', 'cp', 'pull', 'prune'} for c in self.runner.calls))

    def test_no_unit_or_current_identity_is_a_noop_without_docker_calls(self):
        self.unit.write_bytes(generated(self.c, old=self.c['deployment_id']).encode())
        self.assertIsNone(self.retire())
        self.assertEqual(self.runner.calls, [])
        self.unit.unlink()  # Only this test's synthetic unit.
        self.assertIsNone(self.retire())
        self.assertFalse(self.archives.exists())

    def test_any_project_container_stopped_or_running_prevents_all_mutations(self):
        for identifier in ('a' * 12, 'b' * 64):
            with self.subTest(identifier=identifier):
                self.runner.project_ids = identifier
                with self.assertRaisesRegex(SafetyError, 'stopped or running'):
                    self.retire()
                self.assert_untouched()
                self.assertEqual(self.mutations(), [])
        self.assertFalse(self.archives.exists())

    def test_old_uuid_container_in_another_project_prevents_retirement(self):
        self.runner.owner_ids = 'b' * 64
        with self.assertRaisesRegex(SafetyError, 'stopped or running'):
            self.retire()
        self.assert_untouched()
        self.assertEqual(self.mutations(), [])

    def test_container_appearing_after_disable_aborts_before_archive(self):
        self.runner.after_disable = lambda: setattr(self.runner, 'project_ids', 'c' * 64)
        with self.assertRaisesRegex(SafetyError, 'No unit was archived'):
            self.retire()
        self.assert_untouched()
        self.assertEqual(self.mutations(), [['systemctl', 'stop', self.unit.name], ['systemctl', 'disable', self.unit.name]])

    def test_old_uuid_container_appearing_after_disable_also_aborts(self):
        self.runner.after_disable = lambda: setattr(self.runner, 'owner_ids', 'd' * 64)
        with self.assertRaises(SafetyError):
            self.retire()
        self.assert_untouched()

    def test_remote_unknown_or_unavailable_docker_is_not_empty_docker(self):
        for endpoint in ('ssh://example.invalid', '', 'tcp://127.0.0.1:2375'):
            with self.subTest(endpoint=endpoint):
                self.runner.endpoint = endpoint
                with self.assertRaisesRegex(SafetyError, 'local Unix-socket'):
                    self.retire()
                self.assert_untouched()
                self.assertEqual(self.mutations(), [])
        self.runner.endpoint = 'unix:///var/run/docker.sock'
        self.runner.fail_on = ['docker', '--context', 'default', 'info', '--format', '{{.OSType}}']
        with self.assertRaises(SafetyError) as caught:
            self.retire()
        self.assertNotIn('fixture-private', str(caught.exception))
        self.assert_untouched()

    def test_generated_project_install_path_and_all_exec_directives_must_match(self):
        for transform in (lambda s: s.replace('/opt/samurai', '/opt/other'),
                          lambda s: s.replace('--project-name homelab-2', '--project-name other-2'),
                          lambda s: s.replace('ExecStop=', 'ExecStopPost=/bin/true\nExecStop='),
                          lambda s: s.replace('Type=simple', 'Type=simple\nEnvironmentFile=/root/custom-docker.env'),
                          lambda s: s.replace('Type=simple', 'Type=simple\nRootDirectory=/srv/custom-root'),
                          lambda s: s.replace('Requires=docker.service', 'OnFailure=custom.service\nRequires=docker.service'),
                          lambda s: s + 'ExecStartPre=/bin/true\n',
                          lambda s: s.replace('--pull never', '--pull always'),
                          lambda s: s.replace('ExecStop=', 'ExecStop=\nIgnored='),
                          lambda s: s.replace('# Managed by Samurai Stack:', '# Not managed:')):
            with self.subTest(case=transform):
                self.unit.write_bytes(transform(self.original.decode()).encode())
                changed = self.unit.read_bytes()
                with self.assertRaises(SafetyError):
                    self.retire()
                self.assertEqual(self.unit.read_bytes(), changed)
                self.assertEqual(self.runner.calls, [])

    def test_reboot_safe_generated_policy_is_recognized_but_custom_restart_dependencies_are_not(self):
        text = generated(self.c).replace('Requires=docker.service\n',
            'Requires=docker.service\nPartOf=docker.service\nStartLimitIntervalSec=0\n')
        text = text.replace(' up --no-color ', ' up --no-color --force-recreate ')
        self.assertEqual(retirement._generated(self.c, 2, text), OLD)
        for changed in (text.replace('PartOf=docker.service', 'PartOf=unrelated.service'),
                        text.replace('StartLimitIntervalSec=0', 'StartLimitIntervalSec=15'),
                        text.replace('Restart=on-failure', 'Restart=always')):
            with self.subTest(changed=changed), self.assertRaises(SafetyError):
                retirement._generated(self.c, 2, changed)

    def test_duplicate_or_invalid_uuid_marker_refuses_before_commands(self):
        for text in (self.original.decode() + '# Managed by Samurai Stack: ' + OLD + '\n',
                     self.original.decode().replace(OLD, 'not-a-uuid')):
            self.unit.write_bytes(text.encode())
            with self.assertRaises(SafetyError):
                self.retire()
            self.assertEqual(self.runner.calls, [])

    def test_mutated_unit_after_disable_is_retained(self):
        changed = self.original + b'# changed concurrently\n'
        self.runner.after_disable = lambda: self.unit.write_bytes(changed)
        with self.assertRaisesRegex(SafetyError, 'changed during retirement'):
            self.retire()
        self.assertEqual(self.unit.read_bytes(), changed)
        self.assertFalse(any(self.archives.glob('*/*.service')))

    def test_other_active_unit_for_old_uuid_is_unchanged_without_retirement_deadlock(self):
        other = self.units / 'samurai-homelab-3.service'
        other.write_bytes(generated(self.c, 3).encode())
        other.chmod(0o644)
        before = other.read_bytes()
        self.runner.other_active = 'activating'
        result = self.retire()
        self.assertEqual(result['status'], 'obsolete_unit_archived')
        self.assertEqual(other.read_bytes(), before)
        self.assertFalse(any(other.name in call for call in self.runner.calls))

    def test_still_active_target_after_stop_is_not_archived(self):
        self.runner.active = 'deactivating'
        with self.assertRaisesRegex(SafetyError, 'did not become inactive'):
            self.retire()
        self.assert_untouched()

    def test_dropins_wrong_fragment_and_stale_loaded_unit_refuse_before_stop(self):
        for attribute, value in (('dropins', '/etc/systemd/system/example.conf'),
                                 ('fragment_path', '/run/systemd/transient/other.service'),
                                 ('stale', 'yes')):
            with self.subTest(attribute=attribute):
                original = getattr(self.runner, attribute)
                setattr(self.runner, attribute, value)
                with self.assertRaisesRegex(SafetyError, 'overrides or differs'):
                    self.retire()
                self.assert_untouched()
                self.assertEqual(self.mutations(), [])
                setattr(self.runner, attribute, original)

    def test_garbage_collected_unit_after_archive_is_a_successful_retirement(self):
        self.runner.fail_on = ['systemctl', 'reset-failed', self.unit.name]
        result = self.retire()
        self.assertEqual(Path(result['archive_path']).read_bytes(), self.original)
        self.assertIn(['systemctl', 'show', self.unit.name, '--property=LoadState', '--value'], self.runner.calls)

    def test_archive_move_failure_preserves_original_file(self):
        with patch.object(retirement.os, 'rename', side_effect=OSError('fixture disk failure')):
            with self.assertRaisesRegex(SafetyError, 'original unit was retained'):
                self.retire()
        self.assert_untouched()
        self.assertNotIn(['systemctl', 'daemon-reload'], self.runner.calls)

    def test_hardlinked_unit_is_not_archived(self):
        os.link(self.unit, self.root / 'unit-copy')
        with self.assertRaisesRegex(SafetyError, 'hard links'):
            self.retire()
        self.assert_untouched()
        self.assertEqual(self.runner.calls, [])

    @unittest.skipUnless(os.name == 'posix', 'File permission and symlink checks require POSIX')
    def test_nonprivate_archive_and_symlink_unit_refuse_without_repair(self):
        self.archives.mkdir(mode=0o755)
        with self.assertRaisesRegex(SafetyError, 'must be private'):
            self.retire()
        self.assert_untouched()
        self.assertEqual(self.mutations(), [])
        self.archives.chmod(0o700)
        original = self.root / 'original.service'
        self.unit.rename(original)
        self.unit.symlink_to(original)
        with self.assertRaises(SafetyError):
            self.retire()
        self.assertTrue(self.unit.is_symlink())


if __name__ == '__main__':
    unittest.main()
