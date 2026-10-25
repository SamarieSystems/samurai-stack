#!/usr/bin/env python3
"""Build a private-extracting guided installer without changing an old checkout."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile


SCRIPTS = ("scripts/bootstrap-host.sh", "scripts/launcher-common.sh", "scripts/port_sync.py")
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 256

# No application source is imported before ZIP validation and private extraction.
# This is integrity checking, not an authenticity signature. Verify the release
# asset's published SHA256 before running it, as with the ordinary source ZIP.
LAUNCHER = r'''"""Validate and privately extract the public guided-setup payload."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile

MANIFEST_SHA256 = "@MANIFEST_SHA256@"
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 256
SCRIPTS = {"scripts/bootstrap-host.sh", "scripts/launcher-common.sh", "scripts/port_sync.py"}
REQUIRED = {"LICENSE", "stack", "setup", "samurai_stack/__init__.py",
            "samurai_stack/setup.py", "samurai_stack/cli.py", *SCRIPTS}


class BundleError(RuntimeError):
    pass


def _name(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
            or str(PurePosixPath(name)) != name):
        return False
    return (name in {"LICENSE", "stack", "setup", *SCRIPTS}
            or re.fullmatch(r"samurai_stack/[A-Za-z_][A-Za-z0-9_]*\.py", name) is not None)


def _mode(name):
    return 0o700 if name in {"stack", "setup", *SCRIPTS} else 0o600


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate manifest key")
        result[key] = value
    return result


def _read(archive, info, limit):
    with archive.open(info) as stream:
        value = stream.read(limit + 1)
    if len(value) > limit or len(value) != info.file_size:
        raise BundleError("Invalid member size")
    return value


def payload(archive_path):
    """Validate every archive member before creating an extraction directory."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBERS:
                raise BundleError("Invalid member count")
            members, total = {}, 0
            for info in infos:
                name = info.filename
                if (name != info.orig_filename or name in members
                        or (name not in {"__main__.py", "MANIFEST.json"} and not _name(name))):
                    raise BundleError("Duplicate or unsafe member path")
                mode = (info.external_attr >> 16) & 0xffff
                expected = _mode(name)
                if (info.is_dir() or not stat.S_ISREG(mode) or stat.S_IMODE(mode) != expected
                        or info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise BundleError("Unsupported archive member")
                limit = 128 * 1024 if name == "MANIFEST.json" else 64 * 1024 if name == "__main__.py" else MAX_MEMBER_BYTES
                if not 0 <= info.file_size <= limit or not 0 <= info.compress_size <= limit + 64 * 1024:
                    raise BundleError("Archive member is too large")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise BundleError("Archive payload is too large")
                members[name] = info
            if "MANIFEST.json" not in members or "__main__.py" not in members:
                raise BundleError("Missing archive metadata")
            encoded = _read(archive, members["MANIFEST.json"], 128 * 1024)
            if hashlib.sha256(encoded).hexdigest() != MANIFEST_SHA256:
                raise BundleError("Manifest hash differs")
            manifest = json.loads(encoded, object_pairs_hook=_unique)
            if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files"}
                    or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                    or not isinstance(manifest["files"], dict) or not REQUIRED <= set(manifest["files"])
                    or set(members) != set(manifest["files"]) | {"__main__.py", "MANIFEST.json"}):
                raise BundleError("Invalid manifest structure")
            values = {}
            for name, record in manifest["files"].items():
                if (not _name(name) or not isinstance(record, dict) or set(record) != {"sha256", "size", "mode"}
                        or type(record["size"]) is not int or record["size"] != members[name].file_size
                        or type(record["mode"]) is not int or record["mode"] != _mode(name)
                        or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                    raise BundleError("Invalid manifest file identity")
                value = _read(archive, members[name], MAX_MEMBER_BYTES)
                if hashlib.sha256(value).hexdigest() != record["sha256"]:
                    raise BundleError("Payload hash differs")
                values[name] = value
            return values
    except BundleError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, UnicodeError):
        raise BundleError("Setup bundle validation failed") from None


def _invoke(repo, arguments):
    sys.path.insert(0, str(repo))
    try:
        from samurai_stack import setup
        if Path(setup.__file__).resolve() != (repo / "samurai_stack/setup.py").resolve():
            raise BundleError("An unrelated setup module is already loaded")
        return setup.main(arguments)
    finally:
        sys.path.pop(0)


def run_bundle(archive_path, arguments, *, invoke=None):
    values = payload(archive_path)
    # TemporaryDirectory owns this one unique directory. Application state,
    # credentials and site.json remain beside the caller's ORIGINAL config.
    with tempfile.TemporaryDirectory(prefix="samurai-setup-") as directory:
        repo = Path(directory)
        if os.name == "posix":
            repo.chmod(0o700)
        for name, value in values.items():
            target = repo.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(value)
            if os.name == "posix":
                target.chmod(_mode(name))
        # Do not chdir. setup.main resolves relative --config (or site.json)
        # against the original working directory before spawning its CLI.
        return (invoke or _invoke)(repo, list(arguments))


def main():
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required", file=sys.stderr)
        return 1
    try:
        return run_bundle(Path(sys.argv[0]), sys.argv[1:])
    except BundleError:
        print("Setup bundle integrity checks failed. Download and verify the release asset again.", file=sys.stderr)
        return 1
    except Exception:
        print("Guided setup did not finish. Keep the original site file and private state for retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _read_source(root, name):
    path = root.joinpath(*name.split("/"))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Bundle sources cannot be symbolic links")
        if parent == root:
            break
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MEMBER_BYTES:
        raise ValueError("Bundle source must be a bounded regular public file")
    value = path.read_bytes()
    if len(value) > MAX_MEMBER_BYTES:
        raise ValueError("Bundle source exceeded its size limit")
    return value


def _zip_member(archive, name, value, mode=0o600):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, value)


def _bundle_bytes(*, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    if root.is_symlink() or (root / "samurai_stack").is_symlink():
        raise ValueError("Bundle source directories cannot be symbolic links")
    root = root.absolute()
    names = {"LICENSE", "stack", "setup", *SCRIPTS}
    for path in (root / "samurai_stack").glob("*.py"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", path.name):
            raise ValueError("Unexpected production module filename")
        names.add("samurai_stack/" + path.name)
    required = {"samurai_stack/__init__.py", "samurai_stack/setup.py", "samurai_stack/cli.py"}
    if not required <= names or len(names) + 2 > MAX_MEMBERS:
        raise ValueError("Required production setup modules are missing")
    values = {name: _read_source(root, name) for name in sorted(names)}
    if sum(map(len, values.values())) > MAX_TOTAL_BYTES - 192 * 1024:
        raise ValueError("Public setup payload exceeds its total size limit")
    manifest = {"schema_version": 1, "files": {name: {
        "sha256": hashlib.sha256(value).hexdigest(), "size": len(value),
        "mode": 0o700 if name in {"stack", "setup", *SCRIPTS} else 0o600} for name, value in values.items()}}
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    launcher = LAUNCHER.replace("@MANIFEST_SHA256@", hashlib.sha256(encoded).hexdigest()).encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        _zip_member(archive, "__main__.py", launcher)
        _zip_member(archive, "MANIFEST.json", encoded)
        for name, value in values.items():
            _zip_member(archive, name, value, manifest["files"][name]["mode"])
    return data.getvalue()


def _publish(destination, value, mode):
    destination = Path(destination)
    # O_EXCL refuses existing files, symlinks and broken symlinks alike.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
    return destination


def build(destination, *, root=None):
    """Build the Python compatibility asset, retaining its existing API."""
    return _publish(destination, _bundle_bytes(root=root), 0o600)


SHELL_HEADER = b'''#!/usr/bin/env bash
# Self-contained Samurai Stack installer. No runtime code downloads.
# Verify the published release SHA256 before running this file.
set -euo pipefail
'''

SHELL_RUNNER = r'''
case "${1:-}" in
  --help|-h) samurai_setup_help ./samurai-setup.sh; exit 0 ;;
  --version) printf '%s\n' '@VERSION@'; exit 0 ;;
esac
samurai_launcher_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")"
samurai_guest_preflight "$samurai_launcher_path" "$@"
for samurai_tool in base64 sha256sum mktemp wc rm rmdir; do
  if ! command -v "$samurai_tool" >/dev/null 2>&1; then
    printf 'Required OS utility is missing: %s. No installation step was started.\n' "$samurai_tool" >&2
    exit 1
  fi
done
umask 077
samurai_bundle_dir="$(mktemp -d -- "${TMPDIR:-/tmp}/samurai-setup.XXXXXXXXXX")"
samurai_payload="$samurai_bundle_dir/setup.pyz"
samurai_bundle_cleanup() {
  # Only this invocation's private payload and empty directory are removed.
  rm -f -- "$samurai_payload" || :
  rmdir -- "$samurai_bundle_dir" 2>/dev/null || :
}
trap samurai_bundle_cleanup EXIT
base64 --decode > "$samurai_payload" <<'__SAMURAI_SETUP_PAYLOAD__'
@PAYLOAD@
__SAMURAI_SETUP_PAYLOAD__
samurai_payload_size="$(wc -c < "$samurai_payload")"
samurai_payload_hash="$(sha256sum < "$samurai_payload")"
if [[ "$samurai_payload_size" -ne @SIZE@ || "${samurai_payload_hash%% *}" != '@SHA256@' ]]; then
  printf '%s\n' 'Installer integrity check failed. No installation step was started. Download and verify the release again.' >&2
  exit 1
fi
# Replace Bash with Python so signals, terminal input and exit status reach the
# installer directly. Its finally block removes only this owned payload.
exec python3 -I -c 'import os, runpy, sys
archive = sys.argv.pop(1)
try:
    runpy.run_path(archive, run_name="__main__")
finally:
    try:
        os.unlink(archive)
    except OSError:
        pass
    try:
        os.rmdir(os.path.dirname(archive))
    except OSError:
        pass
' "$samurai_payload" "$@"
'''


def build_shell(destination, *, root=None):
    """Embed the validated Python asset and canonical Bash preflight offline."""
    payload = _bundle_bytes(root=root)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        common = archive.read("scripts/launcher-common.sh")
        version_source = archive.read("samurai_stack/__init__.py")
    version = re.search(rb'^__version__ = "([A-Za-z0-9.+-]+)"\r?$', version_source, re.MULTILINE)
    if version is None:
        raise ValueError("A literal public release version is required")
    runner = (SHELL_RUNNER.replace("@VERSION@", version.group(1).decode("ascii"))
              .replace("@SIZE@", str(len(payload)))
              .replace("@SHA256@", hashlib.sha256(payload).hexdigest())
              .replace("@PAYLOAD@", base64.encodebytes(payload).decode("ascii").rstrip("\n")))
    value = SHELL_HEADER + common + b"\n" + runner.encode("ascii")
    return _publish(destination, value, 0o700)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("pyz", "shell"), default="pyz",
                        help="shell builds one executable Bash installer; pyz preserves the Python compatibility asset")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print((build_shell if args.format == "shell" else build)(args.destination))
