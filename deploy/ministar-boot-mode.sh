#!/bin/sh
# Choose the network mode for this boot.
#
# AP is the default. A one-shot flag (written by the SETUP screen) makes this boot
# join the known WiFi client instead, and is consumed so the *next* boot returns to
# AP. This replaces the old "known WiFi first, otherwise AP" fallback: the field
# appliance must be reachable at a known AP address without guessing whether the
# home network is in range.
set -eu

AP="${MINISTAR_AP_PROFILE:-ministar-ap}"
HOME_DIR="${MINISTAR_HOME:-/var/lib/ministar}"
FLAG="$HOME_DIR/wlan-next-boot"

log() {
    logger -t ministar-boot-mode -- "$*" 2>/dev/null || echo "$*" >&2
}

wifi_clients() {
    nmcli -t -f NAME,TYPE,802-11-wireless.mode con show 2>/dev/null |
        awk -F: -v ap="$AP" -v hot="Hotspot" '
            $2 == "802-11-wireless" && $3 != "ap" && $1 != ap && $1 != hot { print $1 }
        '
}

if [ -f "$FLAG" ]; then
    rm -f "$FLAG"
    log "one-shot WLAN boot"
    nmcli con mod "$AP" connection.autoconnect no || true
    nmcli con down "$AP" >/dev/null 2>&1 || true

    clients=$(wifi_clients)
    first=""
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        nmcli con mod "$name" connection.autoconnect yes || true
        [ -z "$first" ] && first=$name
    done <<EOF
$clients
EOF
    if [ -n "$first" ]; then
        nmcli con up "$first" || log "could not activate $first"
    else
        log "no known WiFi client profile; this boot has no network"
    fi
    exit 0
fi

log "AP default boot"
clients=$(wifi_clients)
while IFS= read -r name; do
    [ -n "$name" ] || continue
    nmcli con mod "$name" connection.autoconnect no || true
    nmcli con down "$name" >/dev/null 2>&1 || true
done <<EOF
$clients
EOF

nmcli con mod "$AP" connection.autoconnect yes connection.autoconnect-priority 100 || true
nmcli con up "$AP" || log "could not activate $AP"
