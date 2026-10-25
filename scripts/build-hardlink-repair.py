#!/usr/bin/env python3
"""Package only public Python sources into a standalone existing-install repair."""
import argparse
from pathlib import Path
import zipfile


def build(destination):
    root = Path(__file__).resolve().parents[1]
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Refusing to overwrite an existing repair artifact')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('LICENSE', root.joinpath('LICENSE').read_bytes())
        archive.writestr('__main__.py', 'from samurai_stack.hardlinks import main\nraise SystemExit(main())\n')
        dependencies = ('__init__.py', 'catalog.py', 'engine.py', 'hardlinks.py',
                        'integrations.py', 'safety.py', 'storage.py')
        for name in dependencies:
            source = root / 'samurai_stack' / name
            archive.writestr(source.relative_to(root).as_posix(), source.read_bytes())
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    print(build(parser.parse_args().destination))
