#!/usr/bin/env python3
"""Package only public Python sources into a standalone existing-install repair."""
import argparse
import io
import os
from pathlib import Path
import stat
import zipfile


def build(destination, *, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    destination = Path(destination)
    dependencies = ('__init__.py', 'catalog.py', 'engine.py', 'hardlinks.py',
                    'integrations.py', 'naming.py', 'safety.py', 'storage.py', 'runtime_guard.py')
    values = {'__main__.py': b'from samurai_stack.hardlinks import main\nraise SystemExit(main())\n'}
    for name in ('LICENSE', *('samurai_stack/' + item for item in dependencies)):
        source = root / name
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError('Repair sources cannot be symbolic links')
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
            raise ValueError('Repair sources must be bounded regular public files')
        values[name] = source.read_bytes()
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(values.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data.getbuffer())
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    print(build(parser.parse_args().destination))
