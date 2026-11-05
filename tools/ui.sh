#!/usr/bin/env bash
#
# Start the ministar console on Windows, from WSL. This is the "just run it" path.
#
# It does the three things that are easy to get wrong and expensive to rediscover
# (00 §3, §5, §8):
#
#   1. mirror the repo to C:\ministar\build -- Windows cannot open ext4 paths,
#   2. launch it in the background with its output in a log file, so this shell
#      does not block on a server that is designed to keep running. Do not use
#      `start` here: WSL interop re-quotes the argv and start's "title then
#      command" parsing does not survive it -- the failure mode is a stray
#      Explorer window instead of the app (00 §5), and a window's traceback is
#      unreadable from WSL anyway. The log is readable; that is the point.
#   3. verify from the *Windows* stack, not from WSL. WSL2 runs in mirrored
#      networking here, so a curl to localhost from bash can miss the Windows
#      listener entirely and report a failure that is not real (00 §5).
#
#   ./tools/ui.sh              # sync, then launch against the real camera
#   ./tools/ui.sh --fake       # synthetic stars, no hardware attached
#   ./tools/ui.sh --stop       # stop whatever is listening on the port
#   ./tools/ui.sh --restart    # stop, then launch
#
# Stop servers by PID, never by killing the launcher: a NiceGUI process that has
# bound a socket outlives the bash process group that started it (00 §8), and the
# next launch then fails with "only one usage of each socket address".
#
# The app runs without a console window. Watch it with `tail -f <log>`, or run
# C:\ministar\build\tools\ui.bat from a Windows command prompt if you want the
# live window back.
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${MINISTAR_WIN_DEST:-/mnt/c/ministar/build}"
PORT="${MINISTAR_PORT:-8080}"
LOG_DIR="/mnt/c/ministar/data/logs"
LOG="$LOG_DIR/ui.log"

usage() {
    # The leading comment block only: stop at the first non-comment line so the
    # shell directives below do not leak into --help.
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
}

# PID listening on $PORT, or empty. CRLF is stripped: Windows tools emit it (00 §7).
#
# awk must read to EOF: exiting on the first match closes the pipe, netstat dies of
# SIGPIPE, and `set -o pipefail` turns a perfectly good answer into exit 141.
pid_on_port() {
    local listing
    listing="$(netstat.exe -ano 2>/dev/null | tr -d '\r' || true)"
    printf '%s\n' "$listing" | awk -v port=":$PORT" '
        found { next }
        $2 ~ port"$" && $4 == "LISTENING" { print $5; found = 1 }
    '
}

stop() {
    local pid
    pid="$(pid_on_port)"
    if [[ -z "$pid" ]]; then
        echo "nothing listening on port $PORT"
        return 0
    fi
    echo "stopping PID $pid on port $PORT"
    taskkill.exe /F /PID "$pid" >/dev/null 2>&1 || {
        echo "error: taskkill failed for PID $pid" >&2
        return 1
    }
    # Wait for the socket to actually close before a relaunch tries to bind it.
    for _ in $(seq 1 40); do
        [[ -z "$(pid_on_port)" ]] && { echo "stopped"; return 0; }
        sleep 0.25
    done
    echo "error: port $PORT still held after stop" >&2
    return 1
}

args=()
action="start"
for arg in "$@"; do
    case "$arg" in
        -h|--help) usage; exit 0 ;;
        --stop) action="stop" ;;
        --restart) action="restart" ;;
        *) args+=("$arg") ;;
    esac
done

if [[ "$action" != "start" ]]; then
    stop || exit $?
    [[ "$action" == "stop" ]] && exit 0
fi

existing="$(pid_on_port)"
if [[ -n "$existing" ]]; then
    echo "already running on port $PORT (PID $existing); use --stop or --restart"
    echo "  log: $LOG"
    exit 0
fi

"$SRC/tools/sync.sh"

mkdir -p "$LOG_DIR"
: >"$LOG"

cd /mnt/c
WIN_DEST="$(wslpath -w "$DEST")"

# Background + redirect: this shell must return while the server keeps running.
# Without both, WSL interop holds the pipe open and the call looks hung even though
# the app started fine. The .bat owns the argument quoting, as 00 §5 recommends.
cmd.exe /c "$WIN_DEST\\tools\\ui.bat $*" >>"$LOG" 2>&1 &

# Report the truth, from Windows. A refused connection is expected while NiceGUI
# boots, so retry: `|| true` matters because curl exits 7 on a refusal and
# `set -e` would abort the script on the first attempt instead of waiting.
code=""
for _ in $(seq 1 40); do
    sleep 0.5
    code="$( (cd /mnt/c && cmd.exe /c "curl.exe -s -o NUL -w %{http_code} http://127.0.0.1:$PORT/" 2>/dev/null) || true )"
    code="$(printf '%s' "$code" | tr -d '\r\n')"
    [[ "$code" == "200" ]] && break
done

if [[ "$code" == "200" ]]; then
    echo "ministar is up: http://localhost:$PORT/  (and http://<this-machine-ip>:$PORT/ from a phone)"
else
    echo "error: no answer from port $PORT (last HTTP code: '${code:-none}')" >&2
    echo "       log: $LOG" >&2
    [[ -s "$LOG" ]] && tail -25 "$LOG" >&2
    exit 1
fi
