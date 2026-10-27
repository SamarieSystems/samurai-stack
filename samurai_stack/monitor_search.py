"""Search explicit missing episode IDs after an observed monitoring enable.

Installed as a standalone, standard-library-only Linux timer program. A baseline
is an observation, never permission to search an existing library backlog.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

MAX_BYTES = 16 * 1024 * 1024
MAX_EPISODES = 50000
MAX_RECONCILE = 500
MAX_BATCH = 24
MAX_SHOWS = 2
HOURLY_EPISODES = 120
DAILY_EPISODES = 500
SCOPE = 'sonarr-monitor-search-v1'


class MonitorError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def clean(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise MonitorError('Monitor control paths cannot contain symbolic links')
    return path


def read(path, *, private=True, owners=(0,), native_gid=None):
    path = clean(path); info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise MonitorError('Monitor input must be a bounded regular file without hard links')
    rejected_mode = 0o077 if private else (0o002 | (0o020 if native_gid is None or info.st_gid != native_gid else 0))
    if os.name == 'posix' and (info.st_uid not in owners or info.st_mode & rejected_mode):
        raise MonitorError('Monitor input ownership or permissions differ')
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise MonitorError('Monitor input exceeds its bound')
    return raw.decode('utf-8-sig')


def write(path, value):
    path = clean(path)
    if path.exists():read(path)
    fd, name = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)  # Only our unpublished temporary control file.


def host_id():
    raw = Path('/etc/machine-id').read_bytes()
    if not raw.strip() or len(raw) > 4096:raise MonitorError('Cannot identify monitor host')
    return hashlib.sha256(raw).hexdigest()


def local_run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, **kwargs)


def call(runner, argv):
    try:
        result = runner(argv, timeout=12)
        if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > MAX_BYTES:raise ValueError()
        return result.stdout.strip()
    except Exception:
        raise MonitorError('Monitor could not prove a local prerequisite') from None


def projection(c):
    # Called by the package installer; the standalone runner only reads its
    # already-reviewed projection. Optional photos/music cannot change this.
    project = c.get('compose_projects', {}).get('2', c['deployment_name'] + '-2')
    return {'deployment_id': c['deployment_id'], 'install_root': c['install_root'],
            'bind_address': c['bind_address'], 'hosts': {'media': c['hosts']['media']},
            'compose_projects': {'2': project}, 'storage': c['storage'], 'puid': c['puid'], 'pgid': c['pgid']}


def build_policy(c, *, runner=local_run):
    """Prove an existing managed Sonarr; never adopt or configure an app."""
    from . import catalog, engine, naming, onboarding
    onboarding._guard(c); onboarding._paths(c)
    engine.check_container_ownership(c, 2, runner)
    onboarding.check_targets(c, ['sonarr'], runner)
    reservation = onboarding._load(c)
    saved = reservation.get('apps', {}).get('sonarr') if reservation else None
    directory = onboarding._identity(Path(c['install_root']) / 'appdata/sonarr')
    if not saved or not onboarding.same_identity(saved.get('directory'), directory):
        raise MonitorError('Monitor needs the original verified Sonarr directory reservation')
    config = projection(c)
    if config['compose_projects']['2'] != naming.project_name(c, 2):raise MonitorError('Unsupported media project')
    recipe = engine.recipe(c, 2)['services']['sonarr']
    image = json.loads(call(runner, ['docker', '--context', 'default', 'image', 'inspect', recipe['image']]))
    if not isinstance(image, list) or len(image) != 1:raise MonitorError('Pinned Sonarr image is unavailable')
    environment = dict(v.split('=', 1) for v in image[0]['Config'].get('Env', []) if '=' in v)
    environment.update({k: str(v) for k, v in recipe.get('environment', {}).items()})
    policy = {'schema_version': 1, 'kind': SCOPE, 'enabled': True, 'host_id': engine.host_identity(),
              'config': config, 'config_digest': digest(config),
              'limits': {'episodes_per_hour': HOURLY_EPISODES, 'episodes_per_day': DAILY_EPISODES,
                         'episodes_per_command': MAX_BATCH, 'shows_per_poll': MAX_SHOWS},
              'target': {'image': recipe['image'], 'image_id': image[0]['Id'],
                         'directory': {k: directory[k] for k in ('inode', 'filesystem')}, 'environment': environment}}
    check_target(policy, runner)
    return policy


def load_policy(path):
    p = json.loads(read(path)); c = p.get('config', {})
    try:valid_id = str(uuid.UUID(c.get('deployment_id'))) == c['deployment_id']
    except (ValueError, TypeError, KeyError, AttributeError):valid_id = False
    if (p.get('schema_version') != 1 or p.get('kind') != SCOPE or p.get('enabled') is not True
            or not valid_id or p.get('host_id') != host_id() or p.get('config_digest') != digest(c)
            or p.get('limits') != {'episodes_per_hour': HOURLY_EPISODES, 'episodes_per_day': DAILY_EPISODES,
                                   'episodes_per_command': MAX_BATCH, 'shows_per_poll': MAX_SHOWS}):
        raise MonitorError('Monitor policy belongs to another host or has an invalid identity')
    root = c.get('install_root')
    if (not isinstance(root, str) or not root.startswith('/') or '..' in Path(root).parts
            or re.search(r'[\s$`\\]', root) or str(clean(path).absolute()) != root + '/state/sonarr-monitor-policy.json'):
        raise MonitorError('Monitor policy is outside its owned installation')
    parent = clean(Path(root) / 'state').stat()
    if (not stat.S_ISDIR(parent.st_mode) or os.name == 'posix' and (parent.st_uid != 0 or parent.st_mode & 0o077)):
        raise MonitorError('Monitor state directory must be private to root')
    marker = json.loads(read(Path(root) / '.samurai-install.json'))
    if marker.get('deployment_id') != c['deployment_id']:raise MonitorError('Monitor installation ownership differs')
    for key in ('puid', 'pgid'):
        if type(c.get(key)) is not int or not 1 <= c[key] < 2**31:raise MonitorError('Monitor media identity is invalid')
    address = c.get('hosts', {}).get('media')
    try:ip = ipaddress.ip_address(address)
    except ValueError:raise MonitorError('Monitor endpoint must be a private IPv4 address') from None
    if ip.version != 4 or not ip.is_private or ip.is_unspecified or address != c.get('bind_address'):
        raise MonitorError('Monitor only controls its owned private local endpoint')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,62}', c.get('compose_projects', {}).get('2', '')):
        raise MonitorError('Monitor media project is invalid')
    return p


def mount_identity(path, runner):
    rows = json.loads(call(runner, ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS,UUID']))['filesystems']
    # systemd deliberately gives this observer a read-only app/media view.
    # Docker's live bind identity/RW flag is checked independently below.
    if len(rows) != 1:raise MonitorError('Monitor storage backing filesystem is not unique')
    row = rows[0]; result = {k: row[k] for k in ('target', 'source', 'fstype')}
    if row['fstype'] in {'ext4', 'xfs', 'btrfs'} and row['source'].startswith('/dev/'):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', row.get('uuid', '')):raise MonitorError('Monitor storage UUID is unavailable')
        suffix = row['source'][row['source'].find('['):] if '[' in row['source'] else ''
        result['source'] = 'UUID=' + row['uuid'] + suffix
    return result


def check_target(policy, runner=local_run):
    c, target = policy['config'], policy['target']; root = clean(c['install_root'])
    docker = ['docker', '--context', 'default']
    if not call(runner, [*docker, 'context', 'inspect', 'default', '--format', '{{.Endpoints.docker.Host}}']).startswith('unix://'):
        raise MonitorError('Monitor requires the local default Docker engine')
    ids = call(runner, [*docker, 'ps', '--filter', 'status=running', '--filter',
                       'label=com.docker.compose.project=' + c['compose_projects']['2'], '--filter',
                       'label=com.docker.compose.service=sonarr', '--format', '{{.ID}}']).split()
    if len(ids) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', ids[0]):raise MonitorError('Sonarr must be one unique owned running container')
    rows = json.loads(call(runner, [*docker, 'inspect', ids[0]]))
    image = json.loads(call(runner, [*docker, 'image', 'inspect', target['image']]))
    if len(rows) != 1 or len(image) != 1:raise MonitorError('Sonarr image or container identity is unavailable')
    row = rows[0]; image = image[0]; config = row.get('Config', {}); labels = config.get('Labels', {})
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}', target.get('image_id', ''))
            or not re.search(r'@sha256:[a-f0-9]{64}$', target.get('image', ''))
            or row.get('Image') != target['image_id'] or image.get('Id') != target['image_id']
            or labels.get('systems.samarie.deployment') != c['deployment_id']
            or labels.get('systems.samarie.stage') != '2'
            or labels.get('com.docker.compose.project') != c['compose_projects']['2']
            or labels.get('com.docker.compose.service') != 'sonarr'
            or any(config.get(k) != image.get('Config', {}).get(k) for k in ('Cmd', 'Entrypoint'))
            or (config.get('WorkingDir') or '/') != (image.get('Config', {}).get('WorkingDir') or '/')
            or dict(v.split('=', 1) for v in config.get('Env', []) if '=' in v) != target['environment']):
        raise MonitorError('Sonarr process or pinned owned image differs')
    mounts = row.get('Mounts', [])
    for destination, source in (('/config', root / 'appdata/sonarr'), ('/data', clean(c['storage']['data_root']))):
        selected = [m for m in mounts if m.get('Destination') == destination]
        if (len(selected) != 1 or selected[0].get('Type') != 'bind' or selected[0].get('RW') is not True
                or selected[0].get('Source') != str(source)):
            raise MonitorError('Sonarr data or application mount differs')
        pid = row.get('State', {}).get('Pid')
        if type(pid) is not int or pid < 1:raise MonitorError('Sonarr process is unavailable')
        host = source.stat(); live = (Path('/proc') / str(pid) / 'root' / destination.lstrip('/')).stat()
        if (host.st_dev, host.st_ino) != (live.st_dev, live.st_ino):raise MonitorError('Sonarr live bind no longer matches its source')
    if any(m.get('Destination', '').startswith(('/config/', '/data/', '/app/')) or m.get('Destination') == '/app' for m in mounts):
        raise MonitorError('Sonarr nested source or data overrides are refused')
    directory = (root / 'appdata/sonarr').stat(); saved = target['directory']
    if (directory.st_ino != saved.get('inode') or mount_identity(root / 'appdata/sonarr', runner) != saved.get('filesystem')):
        raise MonitorError('Reserved Sonarr application directory or filesystem changed')
    if mount_identity(c['storage']['data_root'], runner) != c['storage']['expected_mount']:
        raise MonitorError('Sonarr media filesystem changed; no local fallback')
    storage_marker = json.loads(read(Path(c['storage']['data_root']) / '.samurai-storage.json', private=False, owners=(0, c['puid'])))
    if (storage_marker.get('deployment_id') != c['deployment_id']
            or storage_marker.get('expected_mount') != c['storage']['expected_mount']):
        raise MonitorError('Sonarr media storage marker changed')
    address = c['hosts']['media']
    interfaces = json.loads(call(runner, ['ip', '-j', 'address', 'show']))
    if address not in {a.get('local') for i in interfaces for a in i.get('addr_info', [])}:raise MonitorError('Sonarr endpoint is not on this guest')
    ports = row.get('NetworkSettings', {}).get('Ports', {}).get('8989/tcp', [])
    if not ports or any(p.get('HostIp') != address or p.get('HostPort') != '8989' for p in ports):
        raise MonitorError('Sonarr API publication differs')
    for path, reserve in ((root, 20 * 1024**3), (clean(c['storage']['data_root']), 50 * 1024**3)):
        space = os.statvfs(path)
        if space.f_bavail * space.f_frsize < reserve:raise MonitorError('Monitor search is held by the application or media free-space reserve')
    return row


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MonitorError('Monitor API redirects are refused')


class Transport:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, method, url, *, headers, json_body=None):
        data = json.dumps(json_body).encode() if json_body is not None else None
        h = dict(headers)
        if data is not None:h['Content-Type'] = 'application/json'
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=h, method=method), timeout=8) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:raise ValueError()
            return json.loads(raw.decode('utf-8'))
        except Exception:
            raise MonitorError('Sonarr API request was not confirmed; inspect the local service') from None


def api_for(policy, request=None):
    c = policy['config']; root = Path(c['install_root'])
    key_path = root / 'secrets/sonarr_api_key'
    if key_path.exists():key = read(key_path).strip()
    else:
        raw = read(root / 'appdata/sonarr/config.xml', private=False, owners=(0, c['puid']), native_gid=c['pgid'])
        if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():raise MonitorError('Unsupported Sonarr configuration XML')
        xml = ET.fromstring(raw); keys = xml.findall('ApiKey')
        if len(keys) != 1 or (xml.findtext('UrlBase') or '').strip():raise MonitorError('Sonarr API key or base path is unsupported')
        key = keys[0].text or ''
    if not re.fullmatch(r'[a-fA-F0-9]{32}', key):raise MonitorError('Sonarr API key has an unsupported shape')
    request = request or Transport(); base = 'http://' + c['hosts']['media'] + ':8989/api/v3'
    def api(path, method='GET', payload=None):
        if method != 'GET' and not (method == 'POST' and path == '/command' and isinstance(payload, dict)
                                  and set(payload) == {'name', 'episodeIds'} and payload['name'] == 'EpisodeSearch'
                                  and isinstance(payload['episodeIds'], list) and 1 <= len(payload['episodeIds']) <= MAX_BATCH
                                  and all(positive(i) for i in payload['episodeIds'])
                                  and len(set(payload['episodeIds'])) == len(payload['episodeIds'])):
            raise MonitorError('Monitor refuses native settings or broad search writes')
        return request(method, base + path, headers={'X-Api-Key': key}, json_body=payload)
    return api


def positive(value):return type(value) is int and value > 0


def timestamp(text):
    if text is None:return None
    if not isinstance(text, str):raise MonitorError('Sonarr episode date is invalid')
    try:
        value = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if value.tzinfo is None:raise ValueError()
        return value.timestamp()
    except ValueError:raise MonitorError('Sonarr episode date is invalid') from None


def pages(api, path, *, max_rows=MAX_EPISODES):
    result = []; expected = None
    for page in range(1, max_rows // 500 + 2):
        row = api(path + ('&' if '?' in path else '?') + 'page=' + str(page) + '&pageSize=500&sortKey=id&sortDirection=ascending')
        if (not isinstance(row, dict) or type(row.get('totalRecords')) is not int or not 0 <= row['totalRecords'] <= max_rows
                or not isinstance(row.get('records'), list) or len(row['records']) > 500):
            raise MonitorError('Sonarr paged inventory is invalid or exceeds its bound')
        if expected is not None and expected != row['totalRecords']:raise MonitorError('Sonarr inventory changed during pagination; retry next poll')
        expected = row['totalRecords']; result.extend(row['records'])
        if len(result) >= expected:
            if len(result) != expected:raise MonitorError('Sonarr inventory count is inconsistent')
            return result
        if not row['records']:raise MonitorError('Sonarr inventory page is incomplete')
    raise MonitorError('Sonarr inventory pagination exceeded its bound')


def series_inventory(api):
    rows = api('/series')
    if not isinstance(rows, list) or len(rows) > 10000:raise MonitorError('Sonarr series inventory is invalid')
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not positive(row.get('id')) or not positive(row.get('tvdbId'))
                or type(row.get('monitored')) is not bool or row['id'] in result):
            raise MonitorError('Sonarr series identity or monitoring flags are invalid')
        result[row['id']] = row
    return result


def episode_row(row, series, now):
    if (not isinstance(row, dict) or not positive(row.get('id')) or row.get('seriesId') not in series
            or type(row.get('monitored')) is not bool or type(row.get('hasFile')) is not bool
            or type(row.get('episodeNumber')) is not int or row['episodeNumber'] < 0
            or type(row.get('seasonNumber')) is not int or row['seasonNumber'] < 0
            or type(row.get('tvdbId')) is not int or row['tvdbId'] < 0):
        raise MonitorError('Sonarr episode identity or monitoring flags are invalid')
    show = series[row['seriesId']]; aired = timestamp(row.get('airDateUtc'))
    return {'series_id': row['seriesId'], 'series_tvdb_id': show['tvdbId'], 'episode_tvdb_id': row['tvdbId'],
            'season': row['seasonNumber'], 'number': row['episodeNumber'],
            'effective': show['monitored'] and row['monitored'],
            'eligible': not row['hasFile'] and aired is not None and aired <= now,
            'last_search': timestamp(row.get('lastSearchTime'))}


def identities(row):return tuple(row.get(k) for k in ('series_id', 'series_tvdb_id', 'episode_tvdb_id', 'season', 'number'))


def snapshot(api, previous, now):
    raw = {}; duplicates = set()
    for monitored in ('true', 'false'):
        seen = set()
        for row in pages(api, '/wanted/missing?monitored=' + monitored):
            identifier = row.get('id') if isinstance(row, dict) else None
            if not positive(identifier) or identifier in seen:raise MonitorError('Sonarr missing inventory contains invalid or repeated IDs')
            seen.add(identifier)
            if str(identifier) in raw:duplicates.add(str(identifier))
            raw[str(identifier)] = row
    # A monitoring flip between the two filtered requests can put a known
    # episode in neither list. Re-read it instead of losing the transition.
    reconcile = sorted((set(previous) - set(raw)) | duplicates, key=int)
    selected, deferred = reconcile[:MAX_RECONCILE], reconcile[MAX_RECONCILE:]
    for start in range(0, len(selected), MAX_BATCH):
        ids = selected[start:start + MAX_BATCH]
        rows = api('/episode?' + urllib.parse.urlencode([('episodeIds', i) for i in ids]))
        if not isinstance(rows, list) or len(rows) > len(ids):raise MonitorError('Sonarr episode reconciliation is invalid')
        found = set()
        for row in rows:
            identifier = str(row.get('id')) if isinstance(row, dict) else ''
            if identifier not in ids or identifier in found:raise MonitorError('Sonarr reconciled an unexpected episode')
            found.add(identifier)
            if row.get('hasFile') is False:raw[identifier] = row
            else:raw.pop(identifier, None)
        for identifier in set(ids) - found:raw.pop(identifier, None)
    series = series_inventory(api)
    result = {identifier: episode_row(row, series, now) for identifier, row in raw.items() if identifier not in deferred}
    # Large bulk imports can remove thousands of rows at once. Carry the last
    # known observation for the remainder, then reconcile the next chunk on the
    # next tick. This neither invents an enable nor blocks progress forever.
    for identifier in deferred:
        if identifier in previous:result[identifier] = previous[identifier]
    if len(result) > MAX_EPISODES:raise MonitorError('Sonarr snapshot exceeds its bounded episode inventory')
    return result


def queued_ids(api):
    rows = pages(api, '/queue', max_rows=20000); episodes = set(); series = set()
    for row in rows:
        if not isinstance(row, dict):raise MonitorError('Sonarr queue is invalid')
        if positive(row.get('episodeId')):episodes.add(row['episodeId'])
        elif positive(row.get('seriesId')):series.add(row['seriesId'])  # Unknown season-pack extent: defer this show.
    return episodes, series


def commands(api):
    rows = api('/command')
    if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(r, dict) for r in rows):
        raise MonitorError('Sonarr command inventory is invalid')
    return rows


def blocked_by_search(row, episode_id, episode):
    if row.get('status') not in ('queued', 'started'):return False
    body = row.get('body')
    name = row.get('name')
    if name not in ('EpisodeSearch', 'SeasonSearch', 'SeriesSearch', 'MissingEpisodeSearch', 'CutoffUnmetEpisodeSearch'):return False
    if not isinstance(body, dict):return True
    if name == 'EpisodeSearch':
        ids = body.get('episodeIds')
        return not isinstance(ids, list) or episode_id in ids
    sid = body.get('seriesId')
    if sid is not None and sid != episode['series_id']:return False
    season = body.get('seasonNumber')
    return season is None or season == episode['season']


def fresh_episodes(api, ids, now):
    rows = api('/episode?' + urllib.parse.urlencode([('episodeIds', i) for i in ids]))
    if not isinstance(rows, list) or len(rows) > len(ids):raise MonitorError('Sonarr final episode readback is invalid')
    series = series_inventory(api); result = {}
    for row in rows:
        identifier = str(row.get('id')) if isinstance(row, dict) else ''
        if identifier not in ids or identifier in result:raise MonitorError('Sonarr final episode readback has unexpected IDs')
        result[identifier] = episode_row(row, series, now)
    return result


def new_state(binding, boot_id, now, current):
    return {'schema_version': 1, 'binding': binding, 'boot_id': boot_id, 'checked': now,
            'snapshot': current, 'pending': {}, 'claims': [], 'generation': 0}


def validate_state(state, binding):
    if (not isinstance(state, dict) or state.get('schema_version') != 1 or state.get('binding') != binding
            or type(state.get('checked')) not in (int, float) or not isinstance(state.get('boot_id'), str)
            or not isinstance(state.get('snapshot'), dict) or len(state['snapshot']) > MAX_EPISODES
            or not isinstance(state.get('pending'), dict) or len(state['pending']) > MAX_EPISODES
            or not isinstance(state.get('claims'), list) or len(state['claims']) > 2048
            or type(state.get('generation')) is not int or state['generation'] < 0):
        raise MonitorError('Monitor state is invalid or belongs to another policy; no automatic reset')
    for identifier, row in state['snapshot'].items():
        if (not re.fullmatch(r'[1-9][0-9]*', identifier) or not isinstance(row, dict)
                or type(row.get('effective')) is not bool or type(row.get('eligible')) is not bool
                or any(type(v) is not int or v < 0 for v in identities(row))):
            raise MonitorError('Monitor snapshot identity is invalid')
    for identifier, event in state['pending'].items():
        if (identifier not in state['snapshot'] or not isinstance(event, dict) or not positive(event.get('generation'))
                or event.get('identity') != list(identities(state['snapshot'][identifier]))
                or type(event.get('created')) not in (int, float)):
            raise MonitorError('Monitor pending transition is invalid')
    for claim in state['claims']:
        if (not isinstance(claim, dict) or claim.get('status') not in ('claimed', 'accepted', 'uncertain', 'reconciled')
                or not isinstance(claim.get('episode_ids'), list) or not 1 <= len(claim['episode_ids']) <= MAX_BATCH
                or any(not positive(i) for i in claim['episode_ids']) or type(claim.get('created')) not in (int, float)):
            raise MonitorError('Monitor durable command claim is invalid')


def tick(api, state, *, binding, boot_id, now, save, baseline=False, before_submit=lambda: None):
    """Pure native API/control-state boundary, injectable for failure tests."""
    if state is not None:validate_state(state, binding)
    if baseline and state is not None:raise MonitorError('Baseline refuses to overwrite existing durable monitor state')
    if state is not None and now < state['checked']:raise MonitorError('Monitor clock moved backwards; durable state was retained')
    current = snapshot(api, {} if state is None else state['snapshot'], now)
    if state is None:
        save(new_state(binding, boot_id, now, current))
        return {'status': 'baseline', 'observed_missing': len(current), 'commands_submitted': 0, 'episodes_submitted': 0}
    active = commands(api)
    for claim in state['claims']:
        if claim['status'] not in ('claimed', 'uncertain'):continue
        matches = [r for r in active if r.get('name') == 'EpisodeSearch' and isinstance(r.get('body'), dict)
                   and sorted(r['body'].get('episodeIds', [])) == sorted(claim['episode_ids'])
                   and timestamp(r.get('queued')) is not None and timestamp(r.get('queued')) >= claim['created'] - 5]
        claim['status'] = 'reconciled' if matches else 'uncertain'
    pending = state['pending']
    for identifier, row in current.items():
        old = state['snapshot'].get(identifier)
        if (old and identities(old) == identities(row) and not old['effective'] and row['effective'] and row['eligible']):
            state['generation'] += 1
            pending[identifier] = {'generation': state['generation'], 'created': now, 'identity': list(identities(row))}
    for identifier in list(pending):
        row = current.get(identifier)
        if (not row or not row['effective'] or not row['eligible']
                or pending[identifier]['identity'] != list(identities(row))):
            del pending[identifier]
    state.update(snapshot=current, checked=now, boot_id=boot_id)
    # Pending transitions survive deferred native commands and read failures.
    # Observations are durable before any outbound request can search.
    save(state)
    submitted = 0; searched = 0; seen_shows = set()
    budget = min(HOURLY_EPISODES - sum(len(c['episode_ids']) for c in state['claims'] if now - c['created'] < 3600),
                 DAILY_EPISODES - sum(len(c['episode_ids']) for c in state['claims'] if now - c['created'] < 86400))
    for identifier in sorted(list(pending), key=lambda i: (pending[i].get('attempted', 0), pending[i]['created'], int(i))):
        if identifier not in pending:continue
        sid = current[identifier]['series_id']
        if sid in seen_shows or len(seen_shows) >= MAX_SHOWS:continue
        if budget <= 0:break
        seen_shows.add(sid)
        ids = sorted([i for i in pending if current[i]['series_id'] == sid], key=int)[:min(MAX_BATCH, budget)]
        for i in pending:
            if current[i]['series_id'] == sid:pending[i]['attempted'] = now
        before_submit()
        fresh = fresh_episodes(api, ids, now)
        queue, unknown_queue_shows = queued_ids(api); active = commands(api)
        ready = []
        for i in ids:
            row = fresh.get(i)
            if (not row or identities(row) != identities(current[i]) or not row['eligible'] or not row['effective']):
                pending.pop(i, None); continue
            if int(i) in queue:
                pending.pop(i, None); continue  # Existing download already fulfills this transition.
            if (sid in unknown_queue_shows or any(blocked_by_search(r, int(i), row) for r in active)):
                continue
            if row['last_search'] is not None and row['last_search'] >= pending[i]['created']:
                pending.pop(i, None); continue  # A native/user search already handled this enable.
            ready.append(i)
        if not ready:
            save(state); continue
        claim = {'series_id': sid, 'episode_ids': [int(i) for i in ready], 'created': now, 'status': 'claimed',
                 'generation': [pending[i]['generation'] for i in ready]}
        # The claim is never automatically resent after ambiguous POST. Native
        # command history can prove acceptance; absence cannot prove rejection.
        state['claims'].append(claim); state['claims'] = state['claims'][-2048:]
        for i in ready:pending.pop(i)
        save(state)
        try:
            result = api('/command', 'POST', {'name': 'EpisodeSearch', 'episodeIds': claim['episode_ids']})
            if (not isinstance(result, dict) or not positive(result.get('id')) or result.get('name') != 'EpisodeSearch'
                    or not isinstance(result.get('body'), dict) or result['body'].get('episodeIds') != claim['episode_ids']):
                raise MonitorError('Search command acceptance was not confirmed')
            claim.update(status='accepted', command_id=result['id']); submitted += 1; searched += len(ready)
        except Exception:
            claim['status'] = 'uncertain'; save(state)
            raise MonitorError('Search submission is uncertain and was durably held; inspect Sonarr commands before retrying deliberately') from None
        save(state); budget -= len(ready)
    return {'status': 'observed', 'observed_missing': len(current), 'pending_episodes': len(pending),
            'commands_submitted': submitted, 'episodes_submitted': searched,
            'uncertain_commands': sum(c['status'] == 'uncertain' for c in state['claims']),
            'search_budget_remaining': max(0, budget)}


@contextmanager
def lock(path):
    import fcntl
    path = clean(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:raise MonitorError('Monitor lock must remain private to root')
        try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:raise MonitorError('Another monitor poll already owns the state lock') from None
        yield
    finally:os.close(fd)


def run_policy(path, *, runner=local_run, request=None, now=None, boot_id=None, baseline=False):
    policy = load_policy(path); root = Path(policy['config']['install_root'])
    with lock(root / 'state/sonarr-monitor-state.lock'):
        check_target(policy, runner)
        state_path = root / 'state/sonarr-monitor-state.json'
        state = json.loads(read(state_path)) if state_path.exists() else None
        instant = time.time() if now is None else now
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip() if boot_id is None else boot_id
        if not isinstance(boot, str) or not boot or len(boot) > 128:raise MonitorError('Monitor boot identity is invalid')
        return tick(api_for(policy, request), state, binding=digest(policy), boot_id=boot, now=instant,
                    save=lambda value: write(state_path, value), baseline=baseline,
                    before_submit=lambda: check_target(policy, runner))


def load_state(path, policy):
    """Installer readback only. Does not poll native APIs or dispatch commands."""
    expected = Path(policy['config']['install_root']) / 'state/sonarr-monitor-state.json'
    if clean(path).absolute() != expected.absolute():raise MonitorError('Monitor state is outside its owned installation')
    state = json.loads(read(path)); validate_state(state, digest(policy))
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--baseline', action='store_true', help='Initialize missing state without searching; existing state is retained')
    args = parser.parse_args(argv)
    try:
        if not __import__('sys').platform.startswith('linux') or os.geteuid() != 0 or Path('/etc/pve').exists():
            raise MonitorError('Run this owned monitor only as root inside its Linux media guest')
        print(json.dumps(run_policy(args.policy, baseline=args.baseline), sort_keys=True)); return 0
    except MonitorError as exc:
        print('Monitor search blocked: ' + str(exc), file=__import__('sys').stderr); return 1
    except Exception:
        print('Monitor search blocked: policy, storage, ownership, native state or API could not be verified', file=__import__('sys').stderr); return 1


if __name__ == '__main__':raise SystemExit(main())
