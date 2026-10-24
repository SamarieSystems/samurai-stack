#!/usr/bin/env python3
"""Opt-in real stage-0 run in a new scratch directory, no NAS mutations."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from samurai_stack import engine, storage
from samurai_stack.cli import default_config
from samurai_stack.safety import SafetyError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media-uid', type=int, required=True)
    parser.add_argument('--media-gid', type=int, required=True)
    parser.add_argument('--scratch-parent', default='/var/tmp', help='Existing local Linux filesystem; /tmp is often volatile tmpfs')
    parser.add_argument('--acknowledge-temporary-directories', action='store_true', required=True)
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or args.media_uid <= 0 or args.media_gid <= 0:
        parser.error('Run on Linux with non-root media UID/GID')
    if os.geteuid() != 0 and (args.media_uid != os.geteuid() or args.media_gid != os.getegid()):
        parser.error('Run as the selected media UID/GID, or use sudo to test another identity')
    scratch = Path(tempfile.mkdtemp(prefix='samurai-storage-smoke-', dir=args.scratch_parent))
    os.chown(scratch, args.media_uid, args.media_gid)
    c = default_config('127.0.0.1')
    c.update(install_root=str(scratch / 'application'), puid=args.media_uid, pgid=args.media_gid)
    identity = storage.mount_identity(scratch)
    c['storage'] = {'mode':'local', 'data_root':str(scratch / 'media'), 'allow_existing_contents':False,
                    'expected_mount':{k:identity[k] for k in ['target','source','fstype']}}
    outside = scratch / 'outside-sentinel.txt'
    outside.write_bytes(b'untouched outside fixture\n')
    os.chown(outside,args.media_uid,args.media_gid)
    before = outside.stat()
    checksum = hashlib.sha256(outside.read_bytes()).hexdigest()
    first = engine.plan(c,0)
    engine.apply(c,0,first,first['review_hash'])
    inside = Path(c['storage']['data_root']) / 'library/movies/existing-fixture.txt'
    inside.write_bytes(b'untouched library fixture\n')
    second = engine.plan(c,0)
    engine.apply(c,0,second,second['review_hash'])
    verified = engine.verify(c,0)
    after = outside.stat()
    if checksum != hashlib.sha256(outside.read_bytes()).hexdigest() or (before.st_uid,before.st_gid,before.st_mode,before.st_mtime_ns)!=(after.st_uid,after.st_gid,after.st_mode,after.st_mtime_ns):
        raise RuntimeError('Outside sentinel metadata/content changed')
    if inside.read_bytes() != b'untouched library fixture\n':
        raise RuntimeError('Existing library fixture changed')
    bad = deepcopy(c)
    fallback = scratch / 'not-mounted/data'
    bad['storage']={'mode':'nfs','data_root':str(fallback),'expected_mount':{'target':str(scratch / 'not-mounted'),'source':'nas.example:/missing-test-export','fstype':'nfs4'}}
    try:
        engine.plan(bad,0)
    except SafetyError:
        pass
    else:
        raise RuntimeError('Missing NFS identity was accepted')
    if fallback.exists() or fallback.parent.exists():
        raise RuntimeError('Missing-NAS test created a local fallback directory')
    result={'stage0':verified['status'],'real_media_identity':[args.media_uid,args.media_gid],
            'repeat_apply':'passed','outside_sentinel_metadata_and_hash':'unchanged',
            'existing_library_fixture':'unchanged','missing_mount':'refused_before_writes',
            'services_started':0,'physical_disks_changed':0,'scratch_directory':str(scratch)}
    (scratch / 'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
