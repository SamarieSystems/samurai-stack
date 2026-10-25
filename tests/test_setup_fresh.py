"""Fresh setup archives abandoned metadata without rebuilding old choices."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import installation_registry as registry, retired_units, setup_fresh as fresh
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError, write_json
from tests.test_retired_units import generated, OLD


class Runner:
    def __init__(self, units):
        self.units = units
        self.calls = []
        self.containers = {}
        self.kind = 'linux'
        self.endpoint = 'unix:///var/run/docker.sock'
        self.dropins = ''
        self.after_disable = None

    def __call__(self, argv, **kwargs):
        argv = list(map(str, argv));self.calls.append(argv)
        if argv[:6] == ['docker', '--context', 'default', 'context', 'inspect', 'default']:
            text = self.endpoint
        elif argv[:5] == ['docker', '--context', 'default', 'info', '--format']:
            text = self.kind
        elif argv[:5] == ['docker', '--context', 'default', 'ps', '--all']:
            text = self.containers.get(argv[6], '')
        elif argv[:2] == ['systemctl', 'show']:
            prop = argv[3]
            text = {'--property=FragmentPath':str(self.units / argv[2]),
                    '--property=DropInPaths':self.dropins,'--property=NeedDaemonReload':'no',
                    '--property=ActiveState':'inactive','--property=LoadState':'not-found'}[prop]
        elif argv[:2] == ['systemctl', 'disable']:
            if self.after_disable:self.after_disable()
            text = ''
        elif argv[:2] in (['systemctl', 'stop'], ['systemctl', 'reset-failed']) or argv == ['systemctl', 'daemon-reload']:
            text = ''
        else:raise AssertionError('Unexpected command: '+repr(argv))
        return SimpleNamespace(stdout=text,stderr='',returncode=0)


class FreshSetupTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.units=self.root/'units';self.units.mkdir(mode=0o755)
        self.registry=self.root/'installations';self.drafts=self.root/'drafts';self.archives=self.root/'archives'
        self.runner=Runner(self.units);self.messages=[]
        self.enterContext(patch.object(registry,'_guard'))
        self.enterContext(patch.object(retired_units,'_guard'))
        self.enterContext(patch.object(registry,'host_identity',return_value='c'*64))
        self.which=self.enterContext(patch.object(fresh.shutil,'which',return_value='/usr/bin/docker'))
        if os.name=='posix' and os.geteuid()!=0:
            self.enterContext(patch.object(registry,'_owner'))
            self.enterContext(patch.object(retired_units,'_owner'))
        self.c=default_config('192.168.50.21')
        self.c['deployment_id']=OLD
        self.c['install_root']=str(self.root/'absent-apps') if os.name=='posix' else '/srv/fresh-test-apps'
        # Windows cannot represent the Linux config's paths natively. Separate
        # path-safety tests below retain real temporary filesystem checks.
        if os.name!='posix':self.enterContext(patch.object(fresh,'_empty_application_root'))

    def begin(self):
        return fresh.begin_fresh(registry_root=self.registry,setup_root=self.drafts,unit_root=self.units,
                    archive_root=self.archives,runner=self.runner,output_fn=self.messages.append)

    def pending(self):return fresh.pending_fresh_configuration(setup_root=self.drafts)

    def unit(self,c=None,stage=2):
        c=c or self.c
        path=self.units/('samurai-'+c['deployment_name']+'-'+str(stage)+'.service')
        path.write_text(generated(c,stage=stage,old=c['deployment_id']))
        path.chmod(0o644)
        return path

    def canonical(self,c=None):
        c=c or self.c
        directory=registry._mkdir(self.registry/c['deployment_id'])
        write_json(directory/'site.json',c)
        write_json(directory/'identity.json',{'schema_version':1,'host_id':registry.host_identity(),'identity':registry._identity(c)})
        return directory

    def mutations(self):return [c for c in self.runner.calls if c[0]=='systemctl' and c[1]!='show']

    def test_brand_new_host_without_docker_gets_empty_unique_session_and_pointer(self):
        self.which.return_value=None
        site=self.begin()
        self.assertFalse(site.exists())
        self.assertEqual(site,self.pending())
        self.assertEqual([],self.runner.calls)
        self.assertFalse(self.registry.exists())
        if os.name=='posix':self.assertEqual(site.parent.stat().st_mode&0o777,0o700)
        self.assertTrue(any('no previous configuration' in m for m in self.messages))

    def test_repeated_explicit_fresh_never_reuses_old_config_or_progress(self):
        first=self.begin();write_json(first,self.c)
        (first.parent/'progress-sentinel').write_text('old-progress')
        original=first.read_bytes()
        second=self.begin()
        self.assertNotEqual(first,second)
        self.assertFalse(second.exists())
        self.assertEqual(original,first.read_bytes())
        self.assertEqual('old-progress',(first.parent/'progress-sentinel').read_text())
        self.assertEqual(second,self.pending())

    def test_pending_validates_config_if_step_two_saved_it(self):
        site=self.begin();write_json(site,self.c)
        self.assertEqual(site,self.pending())
        site.write_text('not-json')
        with self.assertRaises(SafetyError):self.pending()

    def test_missing_pointer_returns_none_without_creating_state(self):
        self.assertIsNone(self.pending())
        self.assertFalse(self.drafts.exists())

    def test_foreign_or_traversal_pointer_refuses_without_reading_arbitrary_site(self):
        site=self.begin();pointer=site.parent.parent/'active-fresh.json'
        for value in ({'schema_version':1,'host_id':'d'*64,'session':site.parent.name},
                      {'schema_version':1,'host_id':registry.host_identity(),'session':'../somewhere'}):
            write_json(pointer,value,replace=True)
            with self.assertRaises(SafetyError):self.pending()

    @unittest.skipUnless(os.name=='posix','Generated Linux paths require POSIX')
    def test_old_unit_and_registry_are_archived_byte_identically_without_media_access(self):
        unit=self.unit();unit_bytes=unit.read_bytes();directory=self.canonical()
        original={p.name:p.read_bytes() for p in directory.iterdir()}
        media=self.root/'personal-media';media.mkdir();sentinel=media/'photos';sentinel.write_bytes(b'personal')
        before=sentinel.stat();data=sentinel.read_bytes()
        site=self.begin()
        self.assertFalse(site.exists());self.assertFalse(unit.exists());self.assertFalse(directory.exists())
        archived=list(self.archives.glob('*/*.service'));self.assertEqual(len(archived),1)
        self.assertEqual(unit_bytes,archived[0].read_bytes())
        saved=list((self.registry/'.retired').iterdir());self.assertEqual(len(saved),1)
        self.assertEqual(original,{p.name:p.read_bytes() for p in saved[0].iterdir()})
        after=sentinel.stat();self.assertEqual((before.st_ino,before.st_mtime_ns,data),(after.st_ino,after.st_mtime_ns,sentinel.read_bytes()))
        self.assertFalse(any(set(call)&{'rm','down','prune','pull','exec'} for call in self.runner.calls))

    def test_matching_stopped_container_prevents_any_unit_or_registry_mutation(self):
        unit=self.unit();self.runner.containers['label=com.docker.compose.project=homelab-2']='a'*64
        with self.assertRaisesRegex(SafetyError,'stopped or running'):self.begin()
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations());self.assertFalse(self.drafts.exists())

    def test_any_live_samurai_installation_is_preserved_and_blocks_fresh(self):
        unit=self.unit();self.runner.containers['label=systems.samarie.deployment']='b'*64
        with self.assertRaisesRegex(SafetyError,'Samurai containers still exist'):self.begin()
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations())

    def test_unrelated_docker_objects_are_not_queried_without_scoped_filters(self):
        self.begin()
        queries=[c for c in self.runner.calls if c[:5]==['docker','--context','default','ps','--all']]
        self.assertTrue(queries)
        self.assertTrue(all('--filter' in c for c in queries))
        self.assertEqual([],self.mutations())

    def test_nonmatching_custom_service_is_retained_without_systemd_calls(self):
        unit=self.units/'samurai-lab-isolation.service'
        original=b'[Service]\nExecStart=/usr/local/bin/isolated-network-guard\n'
        unit.write_bytes(original)
        site=self.begin()
        self.assertFalse(site.exists())
        self.assertEqual(original,unit.read_bytes())
        self.assertFalse(any(call[0]=='systemctl' for call in self.runner.calls))
        self.assertFalse(self.archives.exists())

    @unittest.skipUnless(os.name=='posix','Real Linux paths require POSIX')
    def test_surviving_application_file_blocks_before_any_cleanup(self):
        unit=self.unit();directory=self.canonical()
        app=Path(self.c['install_root']);app.mkdir();sentinel=app/'sonarr.db';sentinel.write_bytes(b'old-db')
        with self.assertRaisesRegex(SafetyError,'absent or empty'):self.begin()
        self.assertTrue(unit.exists());self.assertTrue(directory.exists());self.assertEqual(b'old-db',sentinel.read_bytes())
        self.assertEqual([],self.mutations())

    def test_all_candidates_validated_before_first_unit_is_stopped(self):
        first=self.unit()
        second=self.unit(dict(self.c,deployment_name='second'),stage=3)
        second.write_text(second.read_text()+'[Other]\nExecStart=/bin/true\n')
        with self.assertRaisesRegex(SafetyError,'custom section'):self.begin()
        self.assertTrue(first.exists());self.assertTrue(second.exists());self.assertEqual([],self.mutations())

    def test_dropin_prevents_all_cleanup(self):
        unit=self.unit();self.runner.dropins='/etc/systemd/system/custom.conf'
        with self.assertRaisesRegex(SafetyError,'overrides'):self.begin()
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations())

    def test_container_appearing_after_stop_prevents_archival_and_fresh_session(self):
        unit=self.unit()
        self.runner.after_disable=lambda:self.runner.containers.update({'label=systems.samarie.deployment='+OLD:'c'*64})
        with self.assertRaisesRegex(SafetyError,'stopped or running'):self.begin()
        self.assertTrue(unit.exists());self.assertFalse(self.drafts.exists())

    def test_remote_context_never_stops_services(self):
        unit=self.unit();self.runner.endpoint='tcp://remote:2375'
        with self.assertRaisesRegex(SafetyError,'local Docker engine'):self.begin()
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations())

    def test_broken_docker_without_metadata_can_open_phase_one_without_cleanup(self):
        self.runner.kind='windows'
        site=self.begin()
        self.assertFalse(site.exists());self.assertEqual([],self.mutations())
        self.assertFalse(self.registry.exists())

    def test_failed_docker_inventory_with_metadata_never_archives(self):
        unit=self.unit();self.runner.kind='windows'
        with self.assertRaisesRegex(SafetyError,'local Docker engine'):self.begin()
        self.assertFalse(self.drafts.exists())
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations())

    def test_missing_canonical_site_uses_only_valid_host_bound_cleanup_identity(self):
        directory=self.canonical();(directory/'site.json').unlink()
        original=(directory/'identity.json').read_bytes()
        site=self.begin()
        self.assertFalse(site.exists());self.assertFalse(directory.exists())
        saved=list((self.registry/'.retired').iterdir())
        self.assertEqual(original,(saved[0]/'identity.json').read_bytes())
        self.assertFalse((saved[0]/'site.json').exists())

    def test_empty_canonical_uuid_directory_archives_only_empty_metadata(self):
        directory=registry._mkdir(self.registry/OLD)
        site=self.begin()
        self.assertFalse(site.exists());self.assertFalse(directory.exists())
        saved=list((self.registry/'.retired').iterdir())
        self.assertEqual(1,len(saved));self.assertEqual([],list(saved[0].iterdir()))

    def test_nonempty_unbound_canonical_metadata_refuses_without_cleanup(self):
        directory=registry._mkdir(self.registry/OLD)
        sentinel=directory/'unknown.txt';sentinel.write_text('retain this')
        with self.assertRaises((SafetyError,OSError)):self.begin()
        self.assertEqual('retain this',sentinel.read_text());self.assertFalse(self.drafts.exists())

    def test_identity_only_foreign_host_is_not_retired(self):
        directory=self.canonical();(directory/'site.json').unlink()
        path=directory/'identity.json';value=json.loads(path.read_text());value['host_id']='d'*64
        write_json(path,value,replace=True)
        with self.assertRaisesRegex(SafetyError,'host-bound identity'):self.begin()
        self.assertTrue(path.exists());self.assertFalse((self.registry/'.retired').exists())

    def test_registry_lock_blocks_before_service_cleanup(self):
        unit=self.unit();self.canonical();write_json(self.registry/'.registry.lock',{'pid':1})
        with self.assertRaisesRegex(SafetyError,'busy or interrupted'):self.begin()
        self.assertTrue(unit.exists());self.assertEqual([],self.mutations())


if __name__=='__main__':unittest.main()
