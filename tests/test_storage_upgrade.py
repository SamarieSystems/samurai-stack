"""Reviewed legacy storage recovery preserves data and rejects stale plans."""
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from samurai_stack import engine, installation_registry as registry, storage, storage_upgrade as upgrade
from samurai_stack.cli import default_config, parser
from samurai_stack.safety import SafetyError, digest, write_json

DATA_UUID = '11111111-2222-4333-8444-555555555555'
APP_UUID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class StorageUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.app, self.data, self.reg = self.base/'app', self.base/'data', self.base/'registry'
        for path in (self.app, self.data, self.reg):
            path.mkdir(mode=0o700)
        self.c = default_config()
        self.c['install_root'] = self.app.as_posix().removeprefix(self.app.drive)
        self.c['storage'] = {'mode':'local','data_root':self.data.as_posix().removeprefix(self.data.drive),
            'expected_mount':{'target':self.data.as_posix().removeprefix(self.data.drive),'source':'/dev/sdb','fstype':'ext4'}}
        self.actual_data = {**self.c['storage']['expected_mount'],'source':'/dev/sda','uuid':DATA_UUID,'options':'rw,relatime'}
        self.actual_app = {'target':'/','source':'/dev/sdb1','fstype':'ext4','uuid':APP_UUID,'options':'rw'}
        self.enterContext(patch.object(upgrade,'_guard'))
        if os.name=='posix' and os.geteuid()!=0:
            self.enterContext(patch.object(upgrade,'_owner'))  # Root-owned production inputs; retain mode/link/size validation.
        self.enterContext(patch.object(engine,'host_identity',return_value='test-host'))
        self.enterContext(patch.object(engine,'deployment_lock',return_value=nullcontext()))
        self.enterContext(patch.object(storage,'as_media_identity',side_effect=lambda c,f:f()))
        for name in ('state','backups','appdata'):
            (self.app/name).mkdir(mode=0o700)
        for relative in storage.DIRECTORIES:
            (self.data/relative).mkdir(parents=True,exist_ok=True)
        self.precious = self.data/'library/movies/family-recording.mkv'
        self.precious.write_bytes(b'leave this alone')
        write_json(self.app/'.samurai-install.json',{'deployment_id':self.c['deployment_id']})
        self.marker = self.data/'.samurai-storage.json'
        write_json(self.marker,{'deployment_id':self.c['deployment_id'],'expected_mount':self.c['storage']['expected_mount']},mode=0o644)
        self.site = self.base/'site.json'
        write_json(self.site,self.c)
        write_json(self.app/'site.json',self.c)
        bucket = self.reg/self.c['deployment_id'];bucket.mkdir(mode=0o700)
        write_json(bucket/'site.json',self.c)
        write_json(bucket/'identity.json',{'schema_version':1,'host_id':'test-host','identity':registry._identity(self.c)})
        self.stage_site = engine.stage_path(self.c,2)/'site.json'
        self.stage_site.parent.mkdir(parents=True)
        write_json(self.stage_site,self.c)
        self.appdir = self.app/'appdata/bazarr';self.appdir.mkdir()
        info=self.appdir.stat()
        self.original_directory={'device':info.st_dev+1,'inode':info.st_ino}
        write_json(self.app/'state/onboarding.json',{'host_id':'test-host','deployment_id':self.c['deployment_id'],
            'apps':{'bazarr':{'directory':self.original_directory,'admin_verified':True}}})
        write_json(self.app/'state/subtitle-setup.json',{'host_id':'test-host','deployment_id':self.c['deployment_id'],
            'config_digest':digest(self.c),'target':{'directory':self.original_directory,'container':'old-container'},'profile':17})
        self.capacity=self.app/'state/capacity-policy.json'
        write_json(self.capacity,{'host_id':'test-host','deployment_id':self.c['deployment_id'],'config_digest':digest(self.c),
            'config':self.c,'app_mount':{'target':'/','source':'/dev/sda1','fstype':'ext4'},'data_reserve_bytes':50*1024**3})
        for name in upgrade.PREDECESSORS:
            (self.app/name).write_bytes(Path(upgrade.__file__).with_name(name).read_text().encode())
            (self.app/name).chmod(0o600)

    def runner(self, argv):
        self.assertEqual(argv[0],'findmnt')
        path=argv[argv.index('--target')+1].replace('\\','/')
        if len(path)>1 and path[1]==':':path=path[2:]
        row=self.actual_data if path.startswith(self.c['storage']['data_root']) else self.actual_app
        return SimpleNamespace(stdout=json.dumps({'filesystems':[row]}))

    def plan(self,**kwargs):
        return upgrade.plan(self.c,self.site,expected_data_uuid=DATA_UUID,expected_app_uuid=APP_UUID,
            runner=self.runner,registry_root=self.reg,**kwargs)

    def apply(self,document):
        return upgrade.apply(self.c,document,runner=self.runner,registry_root=self.reg)

    def test_plan_is_read_only_and_apply_updates_only_current_bound_controls(self):
        before=self.site.read_bytes();media=self.precious.stat();document=self.plan()
        self.assertEqual(self.site.read_bytes(),before)
        result=self.apply(document)
        self.assertTrue(result['ok']);self.assertFalse(result['services_changed'])
        self.assertEqual(self.precious.read_bytes(),b'leave this alone')
        self.assertEqual((self.precious.stat().st_ino,self.precious.stat().st_mtime_ns),(media.st_ino,media.st_mtime_ns))
        new=json.loads(self.site.read_text());self.assertEqual(new['storage']['expected_mount']['source'],'UUID='+DATA_UUID)
        self.assertEqual(json.loads(self.stage_site.read_text()),new)
        saved=json.loads(self.capacity.read_text());self.assertEqual(saved['config_digest'],digest(new))
        self.assertEqual(saved['app_mount']['source'],'UUID='+APP_UUID)
        receipt=json.loads((self.app/'state/onboarding.json').read_text())
        self.assertTrue(receipt['apps']['bazarr']['admin_verified'])
        self.assertEqual(receipt['apps']['bazarr']['directory']['filesystem']['source'],'UUID='+APP_UUID)
        backup=json.loads(Path(result['backup']).read_text());self.assertEqual(backup,document)

    def test_wrong_confirmed_uuid_never_changes_a_control_or_media_file(self):
        self.actual_data['uuid']='different-disk'
        with self.assertRaisesRegex(SafetyError,'Confirmed UUIDs'):
            self.plan()
        self.assertEqual(json.loads(self.site.read_text()),self.c)

    def test_wrong_marker_or_changed_inode_cannot_authorize_rebinding(self):
        marker=json.loads(self.marker.read_text());marker['deployment_id']='other'
        write_json(self.marker,marker,replace=True,mode=0o644)
        with self.assertRaisesRegex(SafetyError,'marker'):
            self.plan()
        marker['deployment_id']=self.c['deployment_id'];write_json(self.marker,marker,replace=True,mode=0o644)
        receipt=self.app/'state/onboarding.json';value=json.loads(receipt.read_text())
        value['apps']['bazarr']['directory']['inode']+=1;write_json(receipt,value,replace=True)
        with self.assertRaisesRegex(SafetyError,'inode changed'):
            self.plan()

    def test_unknown_guard_edits_are_retained(self):
        path=self.app/'runtime_guard.py';path.write_text('custom guard edits\n')
        with self.assertRaisesRegex(SafetyError,'unknown edits'):
            self.plan()
        self.assertEqual(path.read_text(),'custom guard edits\n')

    def test_changed_control_after_review_is_preserved(self):
        document=self.plan();path=self.app/'state/subtitle-setup.json'
        value=json.loads(path.read_text());value['profile']=18;write_json(path,value,replace=True)
        with self.assertRaisesRegex(SafetyError,'inputs changed'):
            self.apply(document)
        self.assertEqual(json.loads(path.read_text())['profile'],18)
        self.assertEqual(json.loads(self.site.read_text()),self.c)

    def test_review_hash_tampering_is_rejected(self):
        document=self.plan();document['expected_data_uuid']='different'
        with self.assertRaisesRegex(SafetyError,'intact reviewed'):
            self.apply(document)

    def test_failed_publication_restores_before_bytes_and_keeps_private_backup(self):
        document=self.plan();original=upgrade.atomic_write;calls=0
        def failing(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('synthetic second control write failure')
            return original(*args,**kwargs)
        with patch.object(upgrade,'atomic_write',side_effect=failing),self.assertRaises(OSError):
            self.apply(document)
        for row in document['changes']:
            self.assertEqual(Path(row['path']).read_text(encoding='utf-8-sig'),row['before'])
        self.assertTrue(list((self.app/'backups').glob('storage-uuid-*.json')))

    def test_backup_retains_exact_bom_and_crlf_source_bytes(self):
        original=b'\xef\xbb\xbf'+json.dumps(self.c,indent=2).replace('\n','\r\n').encode()
        self.site.write_bytes(original)
        document=self.plan()
        row=next(row for row in document['changes'] if Path(row['path'])==self.site)
        self.assertEqual(row['before'].encode('utf-8'),original)
        self.assertEqual(self.site.read_bytes(),original)

    def test_readonly_or_missing_uuid_mount_is_rejected(self):
        self.actual_data['options']='ro'
        with self.assertRaises(SafetyError):self.plan()
        self.actual_data['options']='rw';self.actual_data['uuid']=None
        with self.assertRaises(SafetyError):self.plan()

    def test_cli_has_explicit_plan_and_apply_not_implicit_device_adoption(self):
        args=parser().parse_args(['storage-identity','plan','--data-uuid',DATA_UUID,'--app-uuid',APP_UUID])
        self.assertEqual(args.command,'storage-identity');self.assertEqual(args.data_uuid,DATA_UUID)

    def test_guided_recovery_confirms_original_uuids_and_saves_review_before_apply(self):
        from samurai_stack.setup import GuidedSetup
        choices=iter([DATA_UUID,APP_UUID,'yes']);output=[]
        ui=GuidedSetup(Path(upgrade.__file__).parents[1],self.site,input_fn=lambda prompt:next(choices),
            output_fn=output.append,state_base=self.base/'progress',registry_root=self.reg)
        document=self.plan()
        with patch.object(upgrade,'observed',return_value=document['mounts']),\
             patch.object(upgrade,'plan',return_value=document) as planner,\
             patch.object(upgrade,'apply',return_value={'backup':'private-backup.json'}) as apply:
            self.assertTrue(ui.upgrade_storage_identity())
        self.assertEqual(planner.call_args.kwargs['expected_data_uuid'],DATA_UUID)
        self.assertEqual(planner.call_args.kwargs['expected_app_uuid'],APP_UUID)
        apply.assert_called_once()
        self.assertEqual(len(list((self.base/'progress').rglob('storage-identity-*.json'))),1)
        self.assertIn('Resume the existing numbered',' '.join(output))


if __name__=='__main__':unittest.main()
