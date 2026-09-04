#!/usr/bin/env bash
# Idempotent setup for the ride-slurp plugin.
# Creates a venv on local disk and installs fitdecode (+ bleak for live HR).
# Safe to run on every invocation: a no-op once dependencies are present.
#
# Prints "READY <python-path>" on success, and warns if libmtp is missing
# (libmtp is only needed for `detect` / `sync`, not for reading the mirror).
set -euo pipefail

# Keep the venv on local disk. Inside a synced folder (iCloud Drive, Dropbox)
# every module read goes through the sync provider and imports crawl.
VENV="${RIDE_SLURP_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/ride-slurp/venv}"
PY="$VENV/bin/python"

pick_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      ver="$("$c" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      case "$ver" in 3.9|3.10|3.11|3.12|3.13) echo "$c"; return 0;; esac
    fi
  done
  command -v python3
}

if [ ! -x "$PY" ]; then
  BASE_PY="$(pick_python)"
  echo "Creating venv at $VENV using $BASE_PY ..." >&2
  mkdir -p "$(dirname "$VENV")"
  "$BASE_PY" -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
fi

if ! "$PY" -c "import fitdecode" >/dev/null 2>&1; then
  echo "Installing fitdecode ..." >&2
  "$PY" -m pip install --quiet --upgrade fitdecode
fi

# bleak is only needed for the live heart-rate display; don't fail without it.
if ! "$PY" -c "import bleak" >/dev/null 2>&1; then
  "$PY" -m pip install --quiet --upgrade bleak >/dev/null 2>&1 \
    || echo "note: bleak could not be installed - live HR will be unavailable." >&2
fi

if ! command -v mtp-detect >/dev/null 2>&1 \
   && [ ! -x /opt/homebrew/bin/mtp-detect ] && [ ! -x /usr/local/bin/mtp-detect ]; then
  echo "note: libmtp not found - 'detect' and 'sync' need it. Install with:" >&2
  echo "        macOS:         brew install libmtp" >&2
  echo "        Debian/Ubuntu: sudo apt install mtp-tools" >&2
fi

echo "READY $PY"
