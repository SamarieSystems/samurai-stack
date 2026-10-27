"""Monitoring edges, native API races and durable at-most-once dispatch."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from samurai_stack import monitor_search as m


def iso(value):return datetime.fromtimestamp(value, timezone.utc).isoformat()


def episode(identifier=1, *, sid=1, monitored=False, has_file=False, aired=100):
    return {'id': identifier, 'seriesId': sid, 'tvdbId': 10000 + identifier, 'seasonNumber': 1,
            'episodeNumber': identifier, 'monitored': monitored, 'hasFile': has_file,
            'airDateUtc': iso(aired) if aired is not None else None, 'lastSearchTime': None}


class Native:
    def __init__(self, count=1, shows=1):
        self.series = {i: {'id': i, 'tvdbId': 100 + i, 'monitored': True,
                           'seasons': [{'seasonNumber': 1, 'monitored': False}]} for i in range(1, shows + 1)}
        self.episodes = {i: episode(i, sid=(i - 1) % shows + 1) for i in range(1, count + 1)}
        self.queue = []; self.command_rows = []; self.posts = []; self.calls = []
        self.before_get = None; self.post_error = False; self.now = 10000

    def __call__(self, path, method='GET', payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        url = urlsplit(path); q = parse_qs(url.query)
        if method == 'POST':
            assert path == '/command' and payload['name'] == 'EpisodeSearch'
            self.posts.append(copy.deepcopy(payload))
            row = {'id': len(self.posts), 'name': 'EpisodeSearch', 'body': copy.deepcopy(payload),
                   'status': 'queued', 'queued': iso(self.now)}
            self.command_rows.append(row)
            if self.post_error:raise OSError('synthetic lost response after native acceptance')
            return copy.deepcopy(row)
        assert method == 'GET'
        if self.before_get:self.before_get(path)
        if url.path == '/series':return copy.deepcopy(list(self.series.values()))
        if url.path == '/episode':return copy.deepcopy([self.episodes[int(i)] for i in q['episodeIds'] if int(i) in self.episodes])
        if url.path == '/command':return copy.deepcopy(self.command_rows)
        if url.path == '/queue':rows = self.queue
        elif url.path == '/wanted/missing':
            monitored = q['monitored'] == ['true']
            rows = [r for r in self.episodes.values() if not r['hasFile'] and
                    (r['monitored'] and self.series[r['seriesId']]['monitored']) == monitored]
        else:raise AssertionError(path)
        rows = sorted(rows, key=lambda r: r.get('id', 0)); page = int(q.get('page', ['1'])[0]); size = int(q.get('pageSize', ['500'])[0])
        return {'page': page, 'pageSize': size, 'totalRecords': len(rows), 'records': copy.deepcopy(rows[(page - 1)*size:page*size])}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.native = Native(); self.state = None; self.now = 10000; self.saves = []

    def save(self, state):
        self.state = copy.deepcopy(state); self.saves.append(copy.deepcopy(state))

    def tick(self, **options):
        self.native.now = self.now
        result = m.tick(self.native, copy.deepcopy(self.state), binding='fixture-policy',
                        boot_id=options.pop('boot_id', 'fixture-boot'), now=self.now, save=options.pop('save', self.save), **options)
        self.now += 30
        return result

    def enable(self, ids=None):
        for i in ids or self.native.episodes:self.native.episodes[i]['monitored'] = True

    def test_first_install_only_baselines_existing_monitored_backlog(self):
        self.enable(); self.assertEqual(self.tick()['status'], 'baseline')
        self.tick(); self.assertEqual(self.native.posts, [])

    def test_episode_enable_searches_explicit_ids_and_does_not_repeat(self):
        self.tick(); self.enable(); report = self.tick(); self.tick()
        self.assertEqual(self.native.posts, [{'name': 'EpisodeSearch', 'episodeIds': [1]}])
        self.assertEqual(report['episodes_submitted'], 1)
        self.assertEqual(self.state['claims'][0]['status'], 'accepted')

    def test_series_enable_searches_effective_false_to_true(self):
        self.enable(); self.native.series[1]['monitored'] = False; self.tick()
        self.native.series[1]['monitored'] = True; self.tick()
        self.assertEqual(self.native.posts[0]['episodeIds'], [1])

    def test_episode_enable_in_unmonitored_season_is_still_native_effective(self):
        self.tick(); self.enable(); self.tick()
        self.assertFalse(self.native.series[1]['seasons'][0]['monitored'])
        self.assertEqual(len(self.native.posts), 1)

    def test_season_bulk_episode_flip_searches_only_missing_aired_ids(self):
        self.native = Native(5); self.native.episodes[2]['hasFile'] = True
        self.native.episodes[3]['airDateUtc'] = None; self.native.episodes[4]['airDateUtc'] = iso(20000)
        self.tick(); self.enable(); self.native.series[1]['seasons'][0]['monitored'] = True; self.tick()
        self.assertEqual(self.native.posts[0]['episodeIds'], [1, 5])

    def test_new_import_missing_entry_and_new_series_do_not_create_enable_events(self):
        self.tick(); self.native.episodes[2] = episode(2, monitored=True); self.tick()
        self.assertEqual(self.native.posts, [])

    def test_airdate_clock_change_alone_does_not_create_monitor_event(self):
        self.native.episodes[1]['airDateUtc'] = iso(20000); self.enable(); self.tick()
        self.now = 20001; self.tick(); self.assertEqual(self.native.posts, [])

    def test_episode_zero_is_valid_and_does_not_break_inventory(self):
        self.native.episodes[1]['episodeNumber'] = 0; self.tick(); self.enable(); self.tick()
        self.assertEqual(self.native.posts[0]['episodeIds'], [1])

    def test_queue_consumes_transition_without_a_duplicate_search(self):
        self.tick(); self.enable(); self.native.queue = [{'id': 1, 'episodeId': 1, 'seriesId': 1}]
        self.tick(); self.native.queue = []; self.tick()
        self.assertEqual(self.native.posts, []); self.assertEqual(self.state['pending'], {})

    def test_unknown_queued_pack_defers_show_and_then_drains(self):
        self.tick(); self.enable(); self.native.queue = [{'id': 1, 'episodeId': None, 'seriesId': 1}]
        self.tick(); self.assertIn('1', self.state['pending'])
        self.native.queue = []; self.tick(); self.assertEqual(len(self.native.posts), 1)

    def test_all_native_search_scopes_defer_overlapping_transition(self):
        for name, body in [('EpisodeSearch', {'episodeIds': [1]}), ('SeasonSearch', {'seriesId': 1, 'seasonNumber': 1}),
                           ('SeriesSearch', {'seriesId': 1}), ('MissingEpisodeSearch', {}), ('CutoffUnmetEpisodeSearch', {})]:
            with self.subTest(name=name):
                self.setUp(); self.tick(); self.enable()
                self.native.command_rows = [{'id': 9, 'name': name, 'body': body, 'status': 'started'}]
                self.tick(); self.assertEqual(self.native.posts, []); self.assertIn('1', self.state['pending'])

    def test_completed_search_after_enable_covers_pending_event(self):
        self.tick(); self.enable()
        self.native.episodes[1]['lastSearchTime'] = iso(self.now)
        self.tick(); self.assertEqual(self.native.posts, []); self.assertEqual(self.state['pending'], {})

    def test_later_deliberate_enable_is_a_new_generation_not_permanent_dedup(self):
        self.tick(); self.enable(); self.tick()
        self.native.command_rows[0]['status'] = 'completed'
        self.native.episodes[1]['monitored'] = False; self.tick(); self.enable(); self.tick()
        self.assertEqual(len(self.native.posts), 2)
        self.assertNotEqual(self.state['claims'][0]['generation'], self.state['claims'][1]['generation'])

    def test_turning_off_again_cancels_pending_intent(self):
        self.tick(); self.enable()
        self.native.command_rows = [{'name': 'SeriesSearch', 'body': {'seriesId': 1}, 'status': 'started'}]
        self.tick(); self.native.episodes[1]['monitored'] = False; self.tick()
        self.assertEqual(self.state['pending'], {}); self.assertEqual(self.native.posts, [])

    def test_final_per_episode_readback_blocks_racing_file_arrival(self):
        self.tick(); self.enable()
        def change(path):
            if path.startswith('/episode?'):self.native.episodes[1]['hasFile'] = True
        self.native.before_get = change; self.tick(); self.assertEqual(self.native.posts, [])

    def test_changed_provider_identity_does_not_retarget_an_old_enable(self):
        self.tick(); self.enable(); self.native.series[1]['tvdbId'] = 999
        self.tick(); self.assertEqual(self.native.posts, [])

    def test_missing_filter_torn_read_reconciles_observed_enable(self):
        self.tick()
        def flip(path):
            if path.startswith('/wanted/missing?monitored=false'):self.enable()
        self.native.before_get = flip; self.tick()
        self.assertEqual(self.native.posts[0]['episodeIds'], [1])

    def test_bulk_import_reconciles_bounded_chunks_without_permanent_dead_end(self):
        self.native = Native(620); self.tick()
        for row in self.native.episodes.values():row['hasFile'] = True
        self.tick(); self.assertEqual(len(self.state['snapshot']), 120)
        self.tick(); self.assertEqual(len(self.state['snapshot']), 0); self.assertEqual(self.native.posts, [])

    def test_reboot_and_long_gap_preserve_known_enable_and_pending_state(self):
        self.tick(); self.enable(); self.now += 20000; self.tick(boot_id='new-boot')
        self.assertEqual(len(self.native.posts), 1)
        self.tick(boot_id='new-boot'); self.assertEqual(len(self.native.posts), 1)

    def test_clock_backwards_refuses_instead_of_resetting_durable_state(self):
        self.tick(); original = copy.deepcopy(self.state); self.now = 100
        with self.assertRaisesRegex(m.MonitorError, 'clock moved backwards'):self.tick()
        self.assertEqual(self.state, original); self.assertEqual(self.native.posts, [])

    def test_explicit_baseline_refuses_existing_state(self):
        self.tick()
        with self.assertRaisesRegex(m.MonitorError, 'refuses to overwrite'):self.tick(baseline=True)

    def test_failed_final_get_retains_pending_for_successful_retry(self):
        self.tick(); self.enable()
        def fail(path):
            if path.startswith('/episode?'):raise OSError('synthetic read outage')
        self.native.before_get = fail
        with self.assertRaises(OSError):self.tick()
        self.assertIn('1', self.state['pending']); self.assertEqual(self.native.posts, [])
        self.native.before_get = None; self.tick(); self.assertEqual(len(self.native.posts), 1)

    def test_ambiguous_post_is_never_repeated_and_native_history_reconciles_it(self):
        self.tick(); self.enable(); self.native.post_error = True
        with self.assertRaisesRegex(m.MonitorError, 'uncertain'):self.tick()
        self.assertEqual(self.state['claims'][0]['status'], 'uncertain')
        self.native.post_error = False; self.now += 10000; self.tick(boot_id='after-reboot')
        self.assertEqual(len(self.native.posts), 1); self.assertEqual(self.state['claims'][0]['status'], 'reconciled')

    def test_crash_after_durable_claim_before_post_does_not_blindly_retry(self):
        self.tick(); self.enable()
        def crash(state):
            self.save(state)
            if state['claims']:raise OSError('synthetic crash after intent publication')
        with self.assertRaises(OSError):self.tick(save=crash)
        self.tick(); self.assertEqual(self.native.posts, [])
        self.assertEqual(self.state['claims'][0]['status'], 'uncertain')

    def test_dispatch_rechecks_target_and_low_space_guard_before_any_post(self):
        self.tick(); self.enable()
        with self.assertRaisesRegex(m.MonitorError, 'low space'):
            self.tick(before_submit=lambda: (_ for _ in ()).throw(m.MonitorError('low space')))
        self.assertEqual(self.native.posts, []); self.assertIn('1', self.state['pending'])

    def test_hourly_budget_preserves_overflow_then_drains_later(self):
        self.enterContext(patch.object(m, 'HOURLY_EPISODES', 24))
        self.native = Native(30); self.tick(); self.enable(); self.tick()
        self.assertEqual(len(self.native.posts[0]['episodeIds']), 24)
        self.assertEqual(len(self.state['pending']), 6)
        self.native.command_rows[0]['status'] = 'completed'; self.tick()
        self.assertEqual(len(self.native.posts), 1)
        self.now += 3601; self.tick(); self.assertEqual(self.native.posts[1]['episodeIds'], list(range(25, 31)))

    def test_daily_budget_prevents_unbounded_backlog_searches(self):
        self.enterContext(patch.object(m, 'HOURLY_EPISODES', 24))
        self.enterContext(patch.object(m, 'DAILY_EPISODES', 96))
        self.native = Native(110); self.tick(); self.enable()
        for _ in range(4):
            self.tick()
            for row in self.native.command_rows:row['status'] = 'completed'
            self.now += 3601
        self.assertEqual(sum(len(p['episodeIds']) for p in self.native.posts), 96)
        self.tick(); self.assertEqual(len(self.native.posts), 4); self.assertEqual(len(self.state['pending']), 14)

    def test_show_budget_limits_even_repeated_deferrals_and_fairly_rotates(self):
        self.native = Native(3, shows=3); self.tick(); self.enable()
        self.native.command_rows = [{'name': 'MissingEpisodeSearch', 'status': 'started', 'body': {}}]
        self.native.calls.clear(); self.tick()
        reads = [p for method,p,_ in self.native.calls if p.startswith('/episode?')]
        self.assertEqual(len(reads), 2)
        self.native.command_rows = []; self.tick(); self.assertEqual(self.native.posts[0]['episodeIds'], [3])

    def test_invalid_state_and_malformed_native_inventory_fail_without_post(self):
        self.tick(); self.state['binding'] = 'another-deployment'
        with self.assertRaisesRegex(m.MonitorError, 'another policy'):self.tick()
        self.assertEqual(self.native.posts, [])

    def test_monitor_off_during_expensive_target_proof_is_caught_by_final_readback(self):
        self.tick(); self.enable()
        self.tick(before_submit=lambda: self.native.episodes[1].update(monitored=False))
        self.assertEqual(self.native.posts, [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX native application identity/umask')
    def test_native_linuxserver_0664_config_is_accepted_only_for_expected_group(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root / 'appdata/sonarr/config.xml'; path.parent.mkdir(parents=True)
            path.write_text('<Config><ApiKey>' + 'a'*32 + '</ApiKey><UrlBase></UrlBase></Config>')
            path.chmod(0o664)
            policy = {'config': {'install_root': str(root), 'puid': os.getuid(), 'pgid': os.getgid(),
                                 'hosts': {'media': '192.168.50.12'}}}
            requests = []
            api = m.api_for(policy, lambda method,url,**kw: requests.append((method,url,kw)))
            api('/series'); self.assertEqual(requests[0][2]['headers'], {'X-Api-Key': 'a'*32})
            policy['config']['pgid'] += 1
            with self.assertRaisesRegex(m.MonitorError, 'permissions differ'):m.api_for(policy)
            policy['config']['pgid'] -= 1; path.chmod(0o666)
            with self.assertRaisesRegex(m.MonitorError, 'permissions differ'):m.api_for(policy)

    def test_api_write_boundary_rejects_settings_and_unbounded_episode_ids(self):
        with tempfile.TemporaryDirectory() as name, patch.object(m, 'read', return_value='a'*32):
            root = Path(name); key = root / 'secrets/sonarr_api_key'; key.parent.mkdir(); key.write_text('synthetic')
            policy = {'config': {'install_root': name, 'hosts': {'media': '192.168.50.12'}}}
            request = unittest.mock.Mock(); api = m.api_for(policy, request)
            for path, payload in [('/series', {}), ('/command', {'name': 'MissingEpisodeSearch', 'episodeIds': [1]}),
                                  ('/command', {'name': 'EpisodeSearch', 'episodeIds': list(range(1, 26))}),
                                  ('/command', {'name': 'EpisodeSearch', 'episodeIds': [1, 1]})]:
                with self.assertRaises(m.MonitorError):api(path, 'POST', payload)
            request.assert_not_called()

    def test_secrets_are_not_logged_when_transport_fails(self):
        with patch.object(m, 'run_policy', side_effect=OSError('fixture-secret-do-not-print')), \
             patch.object(m.os, 'geteuid', return_value=0, create=True), \
             patch('sys.platform', 'linux'), patch.object(Path, 'exists', return_value=False), \
             patch('sys.stderr') as error:
            self.assertEqual(m.main(['--policy', '/fixture']), 1)
            self.assertNotIn('fixture-secret', ''.join(str(c) for c in error.write.call_args_list))


if __name__ == '__main__':unittest.main()
