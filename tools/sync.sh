#!/usr/bin/env bash
#
# Mirror the repo to the Windows side and run it there.
#
# This is the pattern from docs/research/00-windows-interop.md: source of truth
# stays on ext4, Windows only ever sees a disposable copy, and the .bat is the
# only entry point. Never edit files in the destination — they get overwritten.
#
#   ./tools/sync.sh            # sync only
#   ./tools/sync.sh preflight  # sync, then run `ministar preflight` on Windows
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${MINISTAR_WIN_DEST:-/mnt/c/ministar/build}"

mkdir -p "$DEST"

rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    --exclude '*.egg-info' \
    --exclude 'data' \
    "$SRC"/ "$DEST"/

echo "synced $SRC -> $DEST"

if [[ $# -gt 0 ]]; then
    # Must be a /mnt/c cwd: a WSL cwd becomes a UNC path, which cmd.exe cannot
    # use, and it silently relocates to C:\Windows (00 §1).
    cd /mnt/c
    WIN_DEST="$(wslpath -w "$DEST")"
    cmd.exe /c "$WIN_DEST\\tools\\ministar.bat $*"
fi
