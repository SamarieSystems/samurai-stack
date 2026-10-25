#!/usr/bin/env bash
# Maintainer entry point. Builds local artifacts; never tags or publishes.
set -euo pipefail
samurai_source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if ! python3 -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  printf '%s\n' 'Release builds require Python 3.11 or newer and Git.' >&2
  exit 1
fi
exec python3 -I "$samurai_source_root/scripts/build-release.py" "$@"
