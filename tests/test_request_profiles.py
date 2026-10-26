"""Request-choice policy tested against a stateful native-contract model."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from samurai_stack import catalog, engine, media_profiles, request_profiles as policy
from samurai_stack.safety import SafetyError
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


if __name__=='__main__':unittest.main()
