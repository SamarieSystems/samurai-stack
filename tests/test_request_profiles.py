"""Request-choice policy tested against a stateful native-contract model."""
import copy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from samurai_stack import catalog, cli, engine, media_profiles, request_profiles as policy
from samurai_stack.safety import SafetyError, digest, write_json
from test_catalog import config
from test_media_profiles import StarrProfiles


class Vendor(StarrProfiles):
    def __init__(self, c):
        super().__init__()
        self.c, self.extra_calls = c, []
        self.requests, self.rules = [], []
        self.import_lists = {app: [] for app in media_profiles.SERVICES}
        self.collections = []
        self.profile_cache = {}
        self.connections = {}
        self.lost_once = False
        self.lost_profile_once = False
        self.lost_delete_once = False
        for app in media_profiles.SERVICES:
            schema = self.schemas[app]
            for number, name in enumerate(('WEBDL-2160p', 'WEBRip-2160p'), 80):
                schema['items'].append({'quality': {'id': number, 'name': name}, 'items': [], 'allowed': True})
                self.definitions[app].append({'id': number, 'quality': {'id': number, 'name': name},
                                              'minSize': 0, 'maxSize': 2000, 'preferredSize': 1999})
            factories = [{**copy.deepcopy(schema), 'id': identifier, 'name': label}
                         for identifier, label in policy.FACTORIES.items()]
            self.profiles[app] = factories
            languages = self.languages(app)
            originals = media_profiles._formats(c, app, self.format_schemas[app], {'id': -2, 'name': 'Original'})
            for row in originals:
                self('POST', 'http://fixture:'+str(catalog.PORTS[app])+'/api/v3/customformat', json=row)
            baseline = media_profiles._profile(c, app, schema, self.formats[app], {r['name'] for r in originals}, {'id': -2, 'name': 'Original'})
            self.profiles[app].append({**baseline, 'id': 7})
            self.titles[app] = [
                {'id': 91, 'title': 'English title', 'path': '/data/library/example/normal', 'qualityProfileId': 7,
                 'genres': ['Drama'], 'originalLanguage': {'id': 1, 'name': 'English'}, 'seriesType': 'standard'},
                {'id': 92, 'title': 'Japanese animation', 'path': '/data/library/example/anime', 'qualityProfileId': 7,
                 'genres': ['Animation'], 'originalLanguage': {'id': 8, 'name': 'Japanese'}, 'seriesType': 'anime'},
                {'id': 93, 'title': 'Western animation', 'path': '/data/library/example/western', 'qualityProfileId': 7,
                 'genres': ['Animation'], 'originalLanguage': {'id': 1, 'name': 'English'}, 'seriesType': 'standard'},
            ]
            self.connections[app] = [{'id': 0, 'name': app, 'hostname': catalog.role_host(c, 2), 'port': catalog.PORTS[app],
                'isDefault': True, 'is4k': False, 'activeProfileId': 7, 'activeProfileName': baseline['name'],
                'externalUrl': 'https://example.invalid/'+app, 'apiKey': 'fixture-owned-private-key',
                **({'activeAnimeProfileId': 7, 'activeAnimeProfileName': baseline['name']} if app == 'sonarr' else {})}]
        self.calls.clear()

    def languages(self, app):
        return [{'id': 0, 'name': 'Unknown'}, {'id': 1, 'name': 'English'}, {'id': 8, 'name': 'Japanese'},
                {'id': -2, 'name': 'Original'}, *([{'id': -1, 'name': 'Any'}] if app == 'radarr' else [])]

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        parts = urlsplit(url)
        if parts.port == catalog.PORTS['seerr']:
            self.extra_calls.append((method, parts.path, copy.deepcopy(json)))
            path = parts.path.removeprefix('/api/v1/')
            if method == 'GET':
                if path == 'request': return {'pageInfo': {'results': len(self.requests)}, 'results': copy.deepcopy(self.requests)}
                if path == 'overrideRule': return copy.deepcopy(self.rules)
                if path.startswith('settings/'): return copy.deepcopy(self.connections[path.split('/')[1]])
                if path.startswith('service/'):
                    app=path.split('/')[1]
                    if app not in self.profile_cache:
                        self.profile_cache[app]=[{'id':p['id'],'name':p['name']} for p in self.profiles[app]]
                    return {'profiles':copy.deepcopy(self.profile_cache[app])}
            if method=='POST' and path.startswith('settings/cache/') and path.endswith('/flush'):
                self.profile_cache.pop(path.split('/')[2],None)
                return None
            if method == 'PUT' and path.startswith('settings/'):
                _, app, identifier = path.split('/')
                if 'id' in json: raise RuntimeError('OpenAPI read-only body/id')
                self.connections[app] = [{**copy.deepcopy(json), 'id': int(identifier)}]
                return copy.deepcopy(self.connections[app][0])
            raise AssertionError((method, path))
        app = 'sonarr' if parts.port == catalog.PORTS['sonarr'] else 'radarr'
        path = parts.path.removeprefix('/api/v3/')
        kind = 'series' if app == 'sonarr' else 'movie'
        if method == 'GET' and path == 'importlist': return copy.deepcopy(self.import_lists[app])
        if app == 'radarr' and (path == 'collection' or path.startswith('collection/')):
            if method == 'GET':
                return copy.deepcopy(self.collections if path == 'collection' else next(r for r in self.collections if r['id'] == int(path.split('/')[1])))
            if method == 'PUT' and path.startswith('collection/'):
                self.calls.append((method, app, path, copy.deepcopy(json)))
                if not self.ignore_writes:self.collections[next(i for i,r in enumerate(self.collections) if r['id']==json['id'])]=copy.deepcopy(json)
                return copy.deepcopy(json)
        if method == 'GET' and path == 'language': return copy.deepcopy(self.languages(app))
        if path == kind or path.startswith(kind+'/'):
            if method == 'GET':
                return copy.deepcopy(self.titles[app] if path == kind else next(r for r in self.titles[app] if r['id'] == int(path.split('/')[1])))
            if method == 'PUT' and path.startswith(kind+'/'):
                self.calls.append((method, app, path, copy.deepcopy(json)))
                index = next(i for i,r in enumerate(self.titles[app]) if r['id']==json['id'])
                if not self.ignore_writes:self.titles[app][index]=copy.deepcopy(json)
                return copy.deepcopy(json)
        if method == 'DELETE' and path.startswith('qualityprofile/'):
            identifier=int(path.split('/')[1])
            if any(t['qualityProfileId']==identifier for t in self.titles[app]): raise RuntimeError('Profile is in use')
            if any(r.get('qualityProfileId')==identifier for r in [*self.import_lists[app], *(self.collections if app=='radarr' else [])]): raise RuntimeError('Profile is referenced')
            self.calls.append((method,app,path))
            if not self.ignore_writes:self.profiles[app]=[p for p in self.profiles[app] if p['id']!=identifier]
            if self.lost_delete_once:
                self.lost_delete_once=False
                raise RuntimeError('lost profile retirement response')
            return None
        result=super().__call__(method,url,headers=headers,json=json,timeout=timeout)
        if self.lost_once and method=='POST' and path=='customformat':
            self.lost_once=False
            raise RuntimeError('lost response')
        if self.lost_profile_once and method=='POST' and path=='qualityprofile':
            self.lost_profile_once=False
            raise RuntimeError('lost profile creation response')
        return result


class RequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.c=config();self.c['install_root']=self.directory.name
        self.c['deployment_id']='f26af450-4590-4d51-aa7a-f3284b90f174'
        for name in ('state','backups'):Path(self.directory.name,name).mkdir()
        self.vendor=Vendor(self.c)
        patch.object(policy.hardlinks,'check_targets').start()
        patch.object(engine,'host_identity',return_value='synthetic-request-host').start()
        patch.object(engine,'deployment_lock',return_value=nullcontext()).start()
        # The temporary receipts belong to the CI runner, not root. Model
        # root ownership only for this isolated fixture tree; keep private
        # mode/symlink/schema checks and production ownership enforcement.
        patch.object(policy.onboarding,'_root_owned',
                     side_effect=lambda path: Path(path).is_relative_to(Path(self.directory.name))).start()
        self.addCleanup(patch.stopall)

    def plan(self, **kwargs):
        return policy.plan(self.c,request=self.vendor,secret_resolver=lambda _: 'fixture-key',**kwargs)

    def apply(self, document=None):
        return policy.apply(self.c,document or self.plan(),request=self.vendor,secret_resolver=lambda _: 'fixture-key')

    def test_preview_is_read_only_and_binds_exact_anime_ids_not_animation_names(self):
        reviewed=self.plan()
        self.assertFalse(self.vendor.writes)
        for app,row in reviewed['services'].items():
            self.assertEqual(7,row['profile_id'])
            self.assertEqual(4,len(row['choices']))
            self.assertEqual([92],[t['id'] for t in row['anime_assignments']])
            self.assertEqual(list(range(1,7)),[p['id'] for p in row['retire_factory_profiles']])

    def test_native_contract_apply_retains_standard_id_paths_and_exact_four_choices(self):
        before=copy.deepcopy(self.vendor.titles)
        result=self.apply()
        self.assertTrue(result['ok'])
        for app in media_profiles.SERVICES:
            self.assertEqual({7,8,9,10},{p['id'] for p in self.vendor.profiles[app]})
            self.assertEqual(7,result['choices'][app]['hd'])
            self.assertEqual([7,8,7],[t['qualityProfileId'] for t in self.vendor.titles[app]])
            for old,new in zip(before[app],self.vendor.titles[app]):
                self.assertEqual({k:v for k,v in old.items() if k!='qualityProfileId'}, {k:v for k,v in new.items() if k!='qualityProfileId'})
            self.assertFalse(any('search' in str(call).lower() for call in self.vendor.calls))
        self.assertTrue(policy.has_policy(self.c))
        self.assertEqual('verified',policy.receipt(self.c)['state'])

    def test_default_is_hd_and_anime_default_is_separate_without_duplicate_servers(self):
        self.apply()
        for app in media_profiles.SERVICES:
            self.assertEqual(1,len(self.vendor.connections[app]))
            row=self.vendor.connections[app][0]
            self.assertEqual(7,row['activeProfileId']);self.assertFalse(row['is4k'])
            self.assertEqual('https://example.invalid/'+app,row['externalUrl'])
        self.assertEqual(8,self.vendor.connections['sonarr'][0]['activeAnimeProfileId'])
        self.assertTrue(all('id' not in call[2] for call in self.vendor.extra_calls if call[0]=='PUT'))

    def test_unmonitored_collection_profile_migration_preserves_all_other_fields(self):
        self.vendor.titles['radarr'][1]['collection']={'tmdbId': 23026}
        self.vendor.collections=[{'id':5,'title':'Japanese animation collection','tmdbId':23026,'qualityProfileId':1,
            'monitored':False,'rootFolderPath':'/existing/root','searchOnAdd':False,'movies':[{'id':92}]}]
        before=copy.deepcopy(self.vendor.collections[0])
        reviewed=self.plan()
        self.assertEqual('anime_hd',reviewed['services']['radarr']['collection_assignments'][0]['to_kind'])
        self.apply(reviewed)
        self.assertEqual({**before,'qualityProfileId':8},self.vendor.collections[0])
        self.assertFalse(any(call[2]=='collection' for call in self.vendor.calls if call[0]=='PUT'))

    def test_monitored_collection_and_import_list_references_refuse_before_writes(self):
        self.vendor.collections=[{'id':5,'qualityProfileId':1,'monitored':True}]
        with self.assertRaisesRegex(SafetyError,'monitored collection'):self.plan()
        self.assertFalse(self.vendor.writes)
        self.vendor.collections=[]
        self.vendor.import_lists['sonarr']=[{'id':2,'qualityProfileId':1,'enableAutomaticAdd':False}]
        with self.assertRaisesRegex(SafetyError,'import list'):self.plan()
        self.assertFalse(self.vendor.writes)

    def test_stale_seerr_cache_is_flushed_only_for_starr_profiles_and_verified_natively(self):
        self.vendor.profile_cache={app:[{'id':p['id'],'name':p['name']} for p in self.vendor.profiles[app]] for app in media_profiles.SERVICES}
        self.apply()
        flushes=[p for method,p,body in self.vendor.extra_calls if method=='POST']
        self.assertEqual(['/api/v1/settings/cache/sonarr/flush','/api/v1/settings/cache/radarr/flush'],flushes)
        self.assertTrue(all(len(rows)==4 for rows in self.vendor.profile_cache.values()))
        self.vendor.extra_calls.clear()
        self.apply()
        self.assertFalse(any(method!='GET' for method,_,_ in self.vendor.extra_calls))

    def test_profiles_enforce_resolution_remux_and_no_automatic_upgrades(self):
        self.apply()
        for app in media_profiles.SERVICES:
            for row in self.vendor.profiles[app]:
                names={r['quality']['name'] for r in policy._leaves(row['items']) if r['allowed']}
                self.assertEqual(policy.UHD if row['id'] in (9,10) else media_profiles.QUALITIES,names)
                self.assertFalse(any('remux' in name.lower() or 'cam' in name.lower() for name in names))
                self.assertFalse(row['upgradeAllowed'])

    def test_english_dubs_are_not_blocked_by_original_language_rule(self):
        self.apply()
        for app in media_profiles.SERVICES:
            formats={f['id']:f['name'].split(' | ')[0] for f in self.vendor.formats[app]}
            for profile in self.vendor.profiles[app]:
                scores={formats[r['format']]:r['score'] for r in profile['formatItems']}
                self.assertGreater(scores['Dual audio release'],scores['English audio'])
                self.assertGreater(scores['English subtitle release'],0)
                if 'Missing original audio' in scores:self.assertEqual(0,scores['Missing original audio'])
                self.assertEqual(0 if profile['id'] in (8,10) else -10000,scores['No English audio'])
                self.assertEqual(-10000 if profile['id'] in (8,10) else 0,scores['No English or Japanese audio'])
        self.assertEqual('English',next(p for p in self.vendor.profiles['radarr'] if p['id']==7)['language']['name'])
        self.assertEqual('Any',next(p for p in self.vendor.profiles['radarr'] if p['id']==8)['language']['name'])

    def test_only_encoded_uhd_global_sizes_change_without_hd_opt_in(self):
        before=copy.deepcopy(self.vendor.definitions)
        self.apply()
        for app in media_profiles.SERVICES:
            for old,new in zip(before[app],self.vendor.definitions[app]):
                if old['quality']['name'] in policy.UHD:
                    self.assertEqual(tuple(new[k] for k in media_profiles.LIMIT_KEYS),policy.UHD_LIMITS[app])
                else:self.assertEqual(old,new)

    def test_ordinary_maintenance_recognizes_saved_policy_instead_of_creating_fifth_profile(self):
        self.apply()
        before=copy.deepcopy(self.vendor.profiles)
        options={'request':self.vendor,'secret_resolver':lambda _: 'fixture-key'}
        self.assertEqual(3,media_profiles.plan(self.c,**options)['policy_version'])
        self.assertEqual(policy.profile_names(self.c)['hd'],media_profiles.profile_name(self.c))
        self.assertTrue(media_profiles.verify(self.c,**options)['ok'])
        self.assertTrue(media_profiles.apply(self.c,media_profiles.plan(self.c,**options),**options)['ok'])
        self.assertEqual(before,self.vendor.profiles)

    def test_assigned_factory_is_refused_before_any_write(self):
        self.vendor.titles['sonarr'][0]['qualityProfileId']=1
        with self.assertRaises(SafetyError):self.plan()
        self.assertFalse(self.vendor.writes)

    def test_request_and_override_references_prevent_retirement(self):
        for field,value in [('requests',[{'id':1,'type':'movie','profileId':1,'serverId':0}]),('rules',[{'profileId':2}])]:
            setattr(self.vendor,field,value)
            with self.assertRaises(SafetyError):self.plan()
            setattr(self.vendor,field,[])
        self.assertFalse(self.vendor.writes)

    def test_custom_profile_is_preserved_instead_of_silently_deleted(self):
        self.vendor.profiles['radarr'].append({**copy.deepcopy(self.vendor.profiles['radarr'][0]),'id':99,'name':'Owner custom'})
        with self.assertRaises(SafetyError):self.plan()
        self.assertFalse(self.vendor.writes)

    def test_stale_preview_refuses_every_api_write(self):
        reviewed=self.plan()
        self.vendor.profiles['radarr'][0]['futureVendorField']['retained']=False
        with self.assertRaises(SafetyError):self.apply(reviewed)
        self.assertFalse(self.vendor.writes)

    def test_ignored_creation_cannot_claim_success(self):
        reviewed=self.plan();self.vendor.ignore_writes=True
        with self.assertRaises(SafetyError):self.apply(reviewed)
        self.assertEqual('intent',policy.receipt(self.c)['state'])
        self.assertEqual(7,len(self.vendor.profiles['sonarr']))

    def test_lost_creation_response_resumes_without_duplicate_formats(self):
        reviewed=self.plan();self.vendor.lost_once=True
        with self.assertRaises(SafetyError):self.apply(reviewed)
        self.assertTrue(self.apply()['ok'])
        for app in media_profiles.SERVICES:
            labels=[f['name'] for f in self.vendor.formats[app]]
            self.assertEqual(len(labels),len(set(labels)))

    def test_owner_edit_after_interrupted_first_opt_in_is_not_overwritten(self):
        self.vendor.lost_once=True
        with self.assertRaises(SafetyError):self.apply()
        original=next(p for p in self.vendor.profiles['sonarr'] if p['id']==7)
        original['minFormatScore']=500
        before=copy.deepcopy(self.vendor.profiles)
        with self.assertRaisesRegex(SafetyError,'baseline HD profile is customized'):self.plan()
        self.assertEqual(before,self.vendor.profiles)

    def test_lost_profile_creation_response_resumes_and_pins_the_existing_exact_profile(self):
        self.vendor.lost_profile_once=True
        with self.assertRaises(SafetyError):self.apply()
        self.assertEqual('intent',policy.receipt(self.c)['state'])
        self.assertTrue(self.apply()['ok'])
        self.assertEqual({7,8,9,10},{p['id'] for p in self.vendor.profiles['sonarr']})

    def test_interrupted_factory_retirement_resumes_without_recreating_deleted_defaults(self):
        self.vendor.lost_delete_once=True
        with self.assertRaises(SafetyError):self.apply()
        self.assertEqual('intent',policy.receipt(self.c)['state'])
        self.assertTrue(self.apply()['ok'])
        self.assertEqual({7,8,9,10},{p['id'] for p in self.vendor.profiles['sonarr']})

    def test_deleted_pinned_profile_is_not_silently_recreated(self):
        self.apply()
        self.vendor.profiles['sonarr']=[p for p in self.vendor.profiles['sonarr'] if p['id']!=8]
        before=copy.deepcopy(self.vendor.profiles)
        with self.assertRaisesRegex(SafetyError,'profile was replaced or removed'):self.plan()
        self.assertEqual(before,self.vendor.profiles)

    def test_foreign_receipt_host_refuses_even_read_only_maintenance(self):
        self.apply()
        with patch.object(engine,'host_identity',return_value='other-host'):
            with self.assertRaises(SafetyError):policy.has_policy(self.c)

    def test_receipt_root_ownership_guard_remains_enforced(self):
        self.apply()
        with patch.object(policy.onboarding,'_root_owned',return_value=False):
            with self.assertRaisesRegex(SafetyError,'owned by root'):policy.has_policy(self.c)

    def test_old_hd_plan_cannot_replace_curated_owner_policy(self):
        self.apply()
        with self.assertRaises(SafetyError):media_profiles.apply(self.c,{'policy_version':2,'review_hash':'0'*64},request=self.vendor,secret_resolver=lambda _: 'fixture-key')

    def test_scoped_binding_survives_unrelated_extras_but_keeps_media_authority(self):
        self.apply()
        current=copy.deepcopy(self.c)
        current['features'].update(photos=False,music=False,dashboard=False)
        current['hosts']['photos']='192.168.1.80'
        options={'request':self.vendor,'secret_resolver':lambda _: 'fixture-key'}
        self.assertTrue(policy.verify(current,**options)['ok'])
        self.assertEqual(policy.BINDING_SCOPE,policy.receipt(current)['binding_scope'])
        edits=[('bind_address','192.168.1.99'),('install_root',self.directory.name+'-other'),
               ('puid',2000),('pgid',2000),('deployment_id','fd969810-006f-4989-b60f-90daf76a58e1')]
        for key,value in edits:
            changed=copy.deepcopy(current);changed[key]=value
            self.assertFalse(policy._matches_binding(changed,policy.receipt(current)))
        for key,value in [('hosts',{'media':'192.168.1.99'}),('storage',{'data_root':'/different'})]:
            changed=copy.deepcopy(current);changed[key].update(value)
            self.assertFalse(policy._matches_binding(changed,policy.receipt(current)))
        changed=copy.deepcopy(current);changed['projects']={'media':'another-media'}
        saved=policy.receipt(current)
        with patch.object(policy.naming,'project_name',return_value='another-media'):
            self.assertFalse(policy._matches_binding(changed,saved))

    def migration(self):
        self.apply()
        path=policy._path(self.c)
        legacy=policy.receipt(self.c);legacy.pop('binding_scope');legacy['config_digest']=digest(self.c)
        write_json(path,legacy,replace=True)
        previous=copy.deepcopy(self.c)
        current=copy.deepcopy(self.c);current['features']['photos']=not current['features']['photos']
        root=Path(self.directory.name)
        write_json(root/'state/onboarding.json',{'schema_version':1,'directory':'synthetic-retained-authority'})
        self.enterContext(patch.object(policy,'_migration_paths',return_value=(root,path)))
        self.enterContext(patch.object(policy,'validate_config'))
        self.enterContext(patch.object(policy.storage,'verify',return_value={'storage':'verified','mount':{'source':'UUID=fixture'}}))
        self.targets={'sonarr':{'container':'a'*64,'image':'sha256:'+'b'*64,'directory':{'device':1,'inode':2}}}
        self.target_mock=self.enterContext(patch.object(policy,'_migration_targets',side_effect=lambda c,r:copy.deepcopy(self.targets)))
        self.vendor.calls.clear();self.vendor.extra_calls.clear()
        self.options={'request':self.vendor,'secret_resolver':lambda _: 'fixture-key'}
        return current,previous,path,legacy

    def test_legacy_requires_exact_predecessor_and_unknown_scope_is_not_adopted(self):
        current,previous,path,legacy=self.migration()
        with self.assertRaises(SafetyError):policy.verify(current,**self.options)
        self.assertTrue(policy.verify(previous,**self.options)['ok'])
        wrong=copy.deepcopy(previous);wrong['timezone']='America/Denver'
        with self.assertRaises(SafetyError):policy.binding_migration_plan(current,wrong,**self.options)
        write_json(path,{**legacy,'binding_scope':'unknown'},replace=True)
        with self.assertRaises(SafetyError):policy.binding_migration_plan(current,previous,**self.options)
        self.assertFalse(self.vendor.writes)

    def test_reviewed_metadata_only_migration_keeps_native_policy_requests_and_private_original(self):
        current,previous,path,legacy=self.migration()
        self.vendor.requests=[{'id':8,'type':'movie','profileId':7,'serverId':0}]
        before=copy.deepcopy((self.vendor.profiles,self.vendor.formats,self.vendor.titles,self.vendor.connections,self.vendor.requests))
        raw=path.read_bytes()
        reviewed=policy.binding_migration_plan(current,previous,**self.options)
        self.assertEqual(1,len(reviewed['files']))
        self.assertNotIn('fixture-key',json.dumps(reviewed))
        self.assertNotIn('fixture-owned-private-key',json.dumps(reviewed))
        result=policy.apply_binding_migration(current,previous,reviewed,**self.options)
        self.assertTrue(result['ok']);self.assertEqual(0,result['native_settings_written'])
        self.assertEqual(raw,Path(result['backup'],'request-profiles.json').read_bytes())
        after=policy.receipt(current)
        self.assertEqual(legacy,{k:v for k,v in after.items() if k!='binding_scope'}|{'config_digest':legacy['config_digest']})
        self.assertEqual(before,(self.vendor.profiles,self.vendor.formats,self.vendor.titles,self.vendor.connections,self.vendor.requests))
        self.assertFalse(self.vendor.writes)
        self.assertTrue(all(method=='GET' for method,_,_ in self.vendor.extra_calls))
        self.assertTrue(policy.verify(current,**self.options)['ok'])
        retry=policy.binding_migration_plan(current,previous,**self.options)
        self.assertEqual([],retry['files'])
        self.assertEqual(0,policy.apply_binding_migration(current,previous,retry,**self.options)['files_updated'])

    def test_migration_refuses_authority_change_incomplete_policy_or_wrong_saved_ids(self):
        current,previous,path,legacy=self.migration()
        changed=copy.deepcopy(current);changed['storage']['data_root']='/other'
        with self.assertRaisesRegex(SafetyError,'identity changed'):
            policy.binding_migration_plan(changed,previous,**self.options)
        self.vendor.profiles['sonarr'][0]['upgradeAllowed']=True
        with self.assertRaises(SafetyError):policy.binding_migration_plan(current,previous,**self.options)
        self.assertEqual(legacy,json.loads(path.read_text()));self.assertFalse(self.vendor.writes)

    def test_raw_receipt_request_and_target_drift_invalidate_review_without_metadata_write(self):
        current,previous,path,_=self.migration()
        reviewed=policy.binding_migration_plan(current,previous,**self.options)
        original=path.read_text();path.write_text(original+'\n')
        with self.assertRaisesRegex(SafetyError,'changed after review'):
            policy.apply_binding_migration(current,previous,reviewed,**self.options)
        path.write_text(original)
        self.vendor.requests=[{'id':8,'type':'movie','profileId':7,'serverId':0}]
        with self.assertRaisesRegex(SafetyError,'changed after review'):
            policy.apply_binding_migration(current,previous,reviewed,**self.options)
        self.vendor.requests=[]
        self.targets['sonarr']['container']='c'*64
        with self.assertRaisesRegex(SafetyError,'changed after review'):
            policy.apply_binding_migration(current,previous,reviewed,**self.options)
        self.assertFalse(self.vendor.writes)
        self.assertEqual([],list(Path(self.directory.name,'backups').glob('request-binding-*')))

    def test_migration_keeps_backup_and_can_retry_after_interrupted_receipt_publication(self):
        current,previous,path,_=self.migration()
        reviewed=policy.binding_migration_plan(current,previous,**self.options)
        real=policy.write_json
        def lost_after_publish(target,value,**kwargs):
            real(target,value,**kwargs)
            if target==path:raise RuntimeError('synthetic lost completion')
        with patch.object(policy,'write_json',side_effect=lost_after_publish):
            with self.assertRaisesRegex(RuntimeError,'synthetic'):
                policy.apply_binding_migration(current,previous,reviewed,**self.options)
        self.assertEqual(1,len(list(Path(self.directory.name,'backups').glob('request-binding-*'))))
        retry=policy.binding_migration_plan(current,previous,**self.options)
        self.assertEqual([],retry['files'])
        self.assertTrue(policy.apply_binding_migration(current,previous,retry,**self.options)['ok'])
        self.assertFalse(self.vendor.writes)


class MigrationTargetTests(unittest.TestCase):
    def setUp(self):
        self.c=config();self.c['deployment_id']='f26af450-4590-4d51-aa7a-f3284b90f174'
        self.bad=None;self.calls=[]
        self.enterContext(patch.object(engine,'check_container_ownership'))
        self.enterContext(patch.object(policy.onboarding,'check_targets'))
        self.enterContext(patch.object(policy.onboarding,'_identity',return_value={'device':1,'inode':2}))
        reservation={'apps':{a:{'directory':{'device':1,'inode':2}} for a in ('sonarr','radarr','seerr')}}
        self.reservation=self.enterContext(patch.object(policy.onboarding,'_load',return_value=reservation))
        self.enterContext(patch.object(engine,'recipe',return_value={'services':{a:{'image':'fixture-'+a+'@sha256:'+'b'*64} for a in ('sonarr','radarr','seerr')}}))

    def runner(self,argv,**options):
        self.calls.append(argv)
        args=argv[3:]
        defaults={'Cmd':None,'Entrypoint':['/init'],'WorkingDir':''}
        if args[0]=='ps':
            app=next(v.split('=')[-1] for v in args if v.startswith('label=com.docker.compose.service='))
            self.app=app;value={'sonarr':'a','radarr':'b','seerr':'c'}[app]*64
        elif args[:2]==['image','inspect']:
            value='sha256:'+'b'*64 if args[-1]=='{{.Id}}' else json.dumps(defaults)
        elif args[0]=='inspect':
            if args[-1]=='{{json .Image}}':value=json.dumps('sha256:'+('f' if self.bad=='image' else 'b')*64)
            elif args[-1]=='{{json .Config}}':
                actual={**defaults,'WorkingDir':'/'}
                if self.bad=='command':actual['Cmd']=['alternate']
                if self.bad=='workdir':actual['WorkingDir']='/alternate'
                value=json.dumps(actual)
            elif args[-1]=='{{json .Mounts}}':
                destination='/app/config' if self.app=='seerr' else '/config'
                mounts=[{'Destination':destination,'Type':'bind','Source':str(Path(self.c['install_root'])/'appdata'/self.app),'RW':True}]
                if self.app!='seerr':mounts.append({'Destination':'/data','Type':'bind','Source':self.c['storage']['data_root'],'RW':True})
                if self.bad=='source':mounts[0]['Source']='/wrong'
                if self.bad=='readonly':mounts[0]['RW']=False
                if self.bad=='nested':mounts.append({'Destination':destination+'/data','Source':'/wrong'})
                if self.bad=='data-nested':mounts.append({'Destination':'/data/library','Source':'/wrong'})
                if self.bad=='app-overlay':mounts.append({'Destination':'/app','Source':'/wrong'})
                if self.bad=='lsiopy-overlay':mounts.append({'Destination':'/lsiopy','Source':'/wrong'})
                if self.bad=='lsiopy-nested':mounts.append({'Destination':'/lsiopy/bin','Source':'/wrong'})
                value=json.dumps(mounts)
            else:raise AssertionError(argv)
        else:raise AssertionError(argv)
        return subprocess.CompletedProcess(argv,0,value,'')

    def test_target_proves_original_directories_images_mounts_and_normalized_root_workdir(self):
        result=policy._migration_targets(self.c,self.runner)
        self.assertEqual({'sonarr','radarr','seerr'},set(result))
        self.assertTrue(all(row['image']=='sha256:'+'b'*64 for row in result.values()))
        self.assertFalse(any('exec' in argv for argv in self.calls))

    def test_wrong_process_mount_or_image_is_refused(self):
        for bad in ('image','command','workdir','source','readonly','nested','data-nested','app-overlay','lsiopy-overlay','lsiopy-nested'):
            with self.subTest(bad=bad):
                self.bad=bad
                with self.assertRaisesRegex(SafetyError,'image, process or persistent'):
                    policy._migration_targets(self.c,self.runner)

    def test_replaced_or_missing_original_directory_reservation_cannot_be_adopted(self):
        self.reservation.return_value['apps']['sonarr']['directory']['inode']=99
        with self.assertRaisesRegex(SafetyError,'directory changed'):policy._migration_targets(self.c,self.runner)
        self.reservation.return_value=None
        with self.assertRaisesRegex(SafetyError,'original application'):policy._migration_targets(self.c,self.runner)


class BindingCliTests(unittest.TestCase):
    def test_binding_plan_uses_root_private_predecessor_and_separate_backend(self):
        previous=cli.default_config();current=copy.deepcopy(previous);current['features']['photos']=True
        reviewed={'kind':'request-profile-binding-migration','review_hash':'fixture-review'}
        with patch.object(cli,'load_config',return_value=current), \
             patch.object(policy.onboarding,'_private_json',return_value=previous) as private, \
             patch.object(policy,'binding_migration_plan',return_value=reviewed) as plan, \
             patch.object(cli,'write_json') as write,redirect_stdout(io.StringIO()):
            self.assertEqual(0,cli.main(['profiles','binding-plan','--previous-config','old-site.json','--out','review.json']))
        private.assert_called_once_with(Path('old-site.json'))
        plan.assert_called_once_with(current,previous)
        write.assert_called_once_with('review.json',reviewed)

    def test_binding_apply_rejects_hash_mismatch_without_migration_or_profile_apply(self):
        with patch.object(cli,'load_config',return_value=cli.default_config()), \
             patch.object(policy.onboarding,'_private_json',return_value=cli.default_config()), \
             patch.object(cli,'private_read',return_value='{"review_hash":"other"}'), \
             patch.object(policy,'apply_binding_migration') as migrate, \
             patch.object(media_profiles,'apply') as apply,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(1,cli.main(['profiles','binding-apply','--previous-config','old-site.json','--plan','review.json','--accept-plan','reviewed']))
        migrate.assert_not_called();apply.assert_not_called()

    def test_binding_actions_require_predecessor_and_cannot_include_new_policy_choices(self):
        for args in (['profiles','binding-plan'],['profiles','binding-plan','--previous-config','old','--global-sizes'],
                     ['profiles','binding-plan','--previous-config','old','--request-choices'],
                     ['profiles','verify','--previous-config','old']):
            with self.subTest(args=args),patch.object(cli,'load_config',return_value=cli.default_config()), \
                 patch.object(policy,'binding_migration_plan') as plan, \
                 redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(1,cli.main(args))
                plan.assert_not_called()


if __name__=='__main__':unittest.main()
