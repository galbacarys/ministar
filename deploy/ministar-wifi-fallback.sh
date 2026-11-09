#!/bin/sh
# Bring up the ministar access point when the Pi has no uplink.
#
# Runs from a systemd timer a short time after boot, not from the boot path itself:
# NetworkManager needs its own chance to associate with a known network first. The
# order is "known WiFi, then AP", which is the product rule. Ethernet counts as an
# uplink too -- a wired development Pi should not start an AP just because eth0 is
# not WiFi.
#
# The AP profile is deliberately not autoconnecting. This script is the only thing
# that starts it, so a failed association cannot silently take the radio away from a
# known network.
set -eu

PROFILE="${MINISTAR_AP_PROFILE:-ministar-test-ap}"
FORCE="${MINISTAR_FORCE_AP:-0}"

log() {
    logger -t ministar-wifi-fallback -- "$*" 2>/dev/null || echo "$*" >&2
}

if ! nmcli -t -f NAME con show | grep -qx "$PROFILE"; then
    log "AP profile '$PROFILE' is missing; leaving the radio alone"
    exit 0
fi

# Already connected to something (client WiFi or the AP itself)? Nothing to do.
if nmcli -t -f GENERAL.STATE dev show wlan0 | grep -q 'GENERAL.STATE:100'; then
    exit 0
fi

# An uplink exists even if it is not WiFi (eth0 on the bench, or a USB modem).
# At a dark site there is no default route, so this falls through to the AP.
if [ "$FORCE" != "1" ] && ip route show default | grep -q .; then
    log "no WiFi client, but a default route exists; not starting the AP"
    exit 0
fi

log "no uplink; activating '$PROFILE'"
nmcli con up "$PROFILE"
