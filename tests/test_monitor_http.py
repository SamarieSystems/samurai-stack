"""Real HTTP boundary acceptance with a synthetic Sonarr, never a live library."""
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from samurai_stack import monitor_search as m


class MonitorHttpTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()
        self.series = {'id': 7, 'tvdbId': 701, 'monitored': True,
                       'seasons': [{'seasonNumber': 1, 'monitored': False}]}
        self.episodes = [dict(id=i, seriesId=7, tvdbId=7000+i, seasonNumber=1,
                             episodeNumber=i, monitored=False, hasFile=False,
                             airDateUtc='2026-01-01T00:00:00Z', lastSearchTime=None)
                         for i in range(1, 5)]
        self.episodes[2]['airDateUtc'] = '2030-01-01T00:00:00Z'
        self.episodes[3]['hasFile'] = True
        self.posts, self.commands, self.queue = [], [], []
        self.state = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def reply(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path)
                query = parse_qs(path.query)
                if path.path.endswith('/series'):
                    return self.reply([owner.series])
                if path.path.endswith('/wanted/missing'):
                    active = query['monitored'] == ['true']
                    rows = [e for e in owner.episodes if not e['hasFile']
                            and m.timestamp(e['airDateUtc']) <= owner.now
                            and bool(e['monitored'] and owner.series['monitored']) == active]
                    return self.reply({'totalRecords': len(rows), 'records': rows})
                if path.path.endswith('/episode'):
                    ids = {int(i) for i in query['episodeIds']}
                    return self.reply([e for e in owner.episodes if e['id'] in ids])
                if path.path.endswith('/queue'):
                    return self.reply({'totalRecords': len(owner.queue), 'records': owner.queue})
                if path.path.endswith('/command'):
                    return self.reply(owner.commands)
                return self.reply({'error': 'unexpected fixture endpoint'}, 404)

            def do_POST(self):
                if not self.path.endswith('/command'):
                    return self.reply({'error': 'unexpected fixture write'}, 400)
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if value.get('name') != 'EpisodeSearch' or set(value) != {'name', 'episodeIds'}:
                    return self.reply({'error': 'unexpected fixture command'}, 400)
                owner.posts.append(value)
                command = {'id': len(owner.posts), 'name': 'EpisodeSearch',
                           'status': 'queued', 'body': value,
                           'queued': datetime.fromtimestamp(owner.now, timezone.utc).isoformat()}
                owner.commands.append(command)
                self.reply(command, 201)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        transport = m.Transport()
        base = 'http://127.0.0.1:' + str(server.server_port) + '/api/v3'
        self.api = lambda path, method='GET', payload=None: transport(
            method, base + path, headers={'X-Api-Key': 'fixture-http-key'}, json_body=payload)

    def poll(self, boot='fixture-boot'):
        def save(state): self.state = copy.deepcopy(state)
        result = m.tick(self.api, copy.deepcopy(self.state), binding='fixture-http-policy',
                        boot_id=boot, now=self.now, save=save)
        self.now += 35
        return result

    def test_individual_enable_works_with_season_off_and_has_no_duplicate(self):
        self.assertEqual(self.poll()['status'], 'baseline')
        self.episodes[0]['monitored'] = True
        self.assertEqual(self.poll()['episodes_submitted'], 1)
        self.assertEqual(self.posts, [{'name': 'EpisodeSearch', 'episodeIds': [1]}])
        self.poll(); self.poll(boot='fixture-after-reboot')
        self.assertEqual(len(self.posts), 1)

    def test_season_bulk_enable_searches_only_aired_missing(self):
        self.poll()
        self.series['seasons'][0]['monitored'] = True
        for row in self.episodes: row['monitored'] = True
        self.poll()
        self.assertEqual(self.posts, [{'name': 'EpisodeSearch', 'episodeIds': [1, 2]}])

    def test_show_enable_preserves_known_transition_over_outage(self):
        self.series['monitored'] = False
        for row in self.episodes: row['monitored'] = True
        self.poll(); self.now += 10000
        self.series['monitored'] = True
        self.poll(boot='fixture-after-outage')
        self.assertEqual(self.posts, [{'name': 'EpisodeSearch', 'episodeIds': [1, 2]}])

    def test_queued_episode_is_consumed_without_search(self):
        self.poll(); self.episodes[0]['monitored'] = True
        self.queue = [{'episodeId': 1, 'seriesId': 7}]
        self.poll(); self.queue = []; self.poll()
        self.assertEqual(self.posts, [])


if __name__ == '__main__': unittest.main()
