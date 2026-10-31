#!/usr/bin/env bash
# Pocket Stash bootstrap. Run in an APT-based Termux installation.
set -euo pipefail

APP_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ "${PREFIX:-}" != /data/data/com.termux/* ]] || ! command -v pkg >/dev/null 2>&1; then
  echo "This installer needs Termux. Open the Termux app and run it there."
  exit 1
fi
if [[ "${TERMUX_APP_PACKAGE_MANAGER:-apt}" == pacman ]] || ! command -v apt >/dev/null 2>&1; then
  echo "This installer currently supports the APT-based Termux build, not the pacman build."
  echo "Use the F-Droid build or the official GitHub APT APK."
  exit 1
fi
if [[ ! -f "$APP_DIR/server.py" || ! -f "$APP_DIR/performer_workflow.py" || ! -f "$APP_DIR/web/index.html" ]]; then
  echo "The Pocket Stash project files are incomplete. Extract the entire ZIP and try again."
  exit 1
fi

printf '\n  Pocket Stash — first-time bootstrap\n\n'
echo "This installs Python if needed, then starts the local setup page."
echo "Stash, FFmpeg and the community repository are installed only when you click Install in that page."
printf '\n'

if ! command -v python3 >/dev/null 2>&1; then
  pkg install -y --no-install-recommends python
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Python 3.8 or newer is required. Update Python in Termux and retry."
  exit 1
fi

LAUNCHER="$PREFIX/bin/pocket-stash"
if [[ -e "$LAUNCHER" ]] && ! grep -q '^# Pocket Stash launcher' "$LAUNCHER"; then
  echo "Refusing to overwrite an existing program at $LAUNCHER."
  echo "You can still start the companion with: python3 \"$APP_DIR/server.py\""
  exit 1
fi
TMP_LAUNCHER="$PREFIX/bin/.pocket-stash-launcher-$$"
trap 'rm -f "$TMP_LAUNCHER"' EXIT
{
  printf '#!%s/bin/bash\n' "$PREFIX"
  printf '# Pocket Stash launcher\n'
  cat <<'EOF'
# Open the default local URL for normal launches. The server still works if
# Android prevents the browser from opening automatically.
if [ "$#" -eq 0 ] && command -v termux-open-url >/dev/null 2>&1; then
  ( sleep 2; termux-open-url 'http://127.0.0.1:8765/' >/dev/null 2>&1 || true ) &
fi
EOF
  printf 'exec %q %q "$@"\n' "$(command -v python3)" "$APP_DIR/server.py"
} > "$TMP_LAUNCHER"
chmod 700 "$TMP_LAUNCHER"
mv -f "$TMP_LAUNCHER" "$LAUNCHER"
trap - EXIT

printf '\nReady. Opening the setup page at http://127.0.0.1:8765/\n'
echo "Keep Termux open. Next time, start it with: pocket-stash"
printf '\n'
exec "$LAUNCHER"
