#!/usr/bin/env bash
# This is run only after the user explicitly grants Termux control and taps Start.
# The two markers are replaced by the APK's embedded, checksum-verified source.
set -euo pipefail
umask 077

if [[ "${PREFIX:-}" != /data/data/com.termux/* ]] || ! command -v pkg >/dev/null 2>&1; then
  echo 'Pocket Stash requires the regular Termux app. Open it once and finish its initial setup.' >&2
  exit 1
fi
if [[ "${TERMUX_APP_PACKAGE_MANAGER:-apt}" == pacman ]] || ! command -v apt >/dev/null 2>&1; then
  echo 'The APK currently supports an APT-based Termux, not the experimental pacman build.' >&2
  exit 1
fi

WORK="$HOME/.pocket-stash-android"
LOCK="$WORK/start.lock"
mkdir -p "$WORK"
if ! mkdir "$LOCK" 2>/dev/null; then
  # A killed install may leave a lock. Give a concurrent starter time to write its PID.
  sleep 2
  old_pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  case "$old_pid" in
    ''|*[!0-9]*) ;;
    *) if kill -0 "$old_pid" 2>/dev/null; then
         echo 'A Pocket Stash startup is already running in Termux. Wait and check the connection.' >&2
         exit 75
       fi ;;
  esac
  rm -rf -- "$LOCK"
  if ! mkdir "$LOCK" 2>/dev/null; then
    echo 'Another Pocket Stash startup started at the same time. Try checking the connection.' >&2
    exit 75
  fi
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf -- "$LOCK"' EXIT

echo 'Preparing the local Pocket Stash companion in Termux…'
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Installing Python from the configured Termux APT repository…'
  # Do not let a package prompt consume the remaining script from stdin.
  pkg install -y --no-install-recommends python </dev/null
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo 'Python 3.8 or newer is required. Update Python in Termux and try again.' >&2
  exit 1
fi

# Only the fixed list of companion files can be written. Nothing is installed to
# ~/pocket-stash and existing Stash config/media are never overwritten.
python3 - "$WORK" '__POCKET_SHA256__' <<'PY'
import base64
import hashlib
import io
import os
from pathlib import Path
import sys
import zipfile

work = Path(sys.argv[1])
expected_digest = sys.argv[2]
raw = base64.b64decode('''__POCKET_B64__''', validate=True)
if hashlib.sha256(raw).hexdigest() != expected_digest:
    raise SystemExit('Embedded companion checksum did not match. Reinstall the APK.')
allowed = {'server.py', 'performer_workflow.py', 'web/index.html', 'web/style.css', 'web/app.js', 'web/favicon.svg'}
target = work / 'versions' / expected_digest
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    entries = [item for item in archive.infolist() if not item.is_dir()]
    if len(entries) != len(allowed) or {item.filename for item in entries} != allowed:
        raise SystemExit('Embedded companion has an unexpected file list.')
    if any(item.file_size > 1024 * 1024 for item in entries):
        raise SystemExit('Embedded companion file is unexpectedly large.')
    for item in entries:
        destination = target / item.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.tmp-' + str(os.getpid()))
        temporary.write_bytes(archive.read(item))
        os.replace(temporary, destination)
print('Companion files verified. Starting the local setup server…', flush=True)
PY

rm -rf -- "$LOCK"
trap - EXIT
exec python3 "$WORK/versions/__POCKET_SHA256__/server.py"
