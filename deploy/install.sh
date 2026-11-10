#!/usr/bin/env bash
# Install ministar on a fresh Raspberry Pi OS Lite or Debian 13 arm64 system.
#
# This is deliberately a script rather than an image for the MVP: it is the same
# provisioning core an image will call later, it is inspectable, and it can be
# re-run to update the app without reflashing. It expects a system that was just
# installed from Imager (or a minimal Debian 13 arm64 image), with SSH and, if you
# want it on your home network, WiFi configured by Imager.
#
# Usage:
#   sudo bash install.sh --country US
#   sudo bash install.sh --country US --astap-db both --reboot
#
# The one-liner is documented in deploy/README.md. Inspect it first; it runs as root.
set -euo pipefail

APP_ROOT=/opt/ministar
APP_SRC=$APP_ROOT/app
VENV=$APP_ROOT/venv
DATA_DIR=/var/lib/ministar
ETC=/etc/ministar
TLS_DIR=$ETC/tls
AP_PROFILE=ministar-ap
HOSTNAME=ministar
SERVICE=ministar.service
LOG=/var/log/ministar-install.log

REPO=${MINISTAR_REPO:-https://github.com/galbacarys/ministar.git}
REF=${MINISTAR_REF:-main}
COUNTRY=${MINISTAR_WIFI_COUNTRY:-}
AP_PSK=${MINISTAR_AP_PSK:-ministar}
ASTAP_DB=${MINISTAR_ASTAP_DB:-both}
REBOOT=0
FORCE=0
CHECK=0

ASTAP_CLI_URL=https://sourceforge.net/projects/astap-program/files/linux_installer/astap_command-line_version_Linux_aarch64.zip/download
ASTAP_CLI_SHA256=2e56d79a626c895298d49c9d841d4a449154f5764e2f39eee8bba669fdc6fa35
G05_URL=https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.deb/download
G05_SHA256=f4a93403a0c23ac3ca0e05d0fe91080b0f0a21739aa9fabd20bc0dd5e4f77099
D20_URL=https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.deb/download
D20_SHA256=9ae31902423ed51bbab431db78f703b77a44169b76a66789f5dbf65e83f3baa7

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Install ministar on a fresh Raspberry Pi OS Lite or Debian 13 arm64 system.

Options:
  --country CC       WiFi regulatory country (required; the country where the Pi
                     will operate, not a locale). It decides the legal channels,
                     transmit power and whether the radio may host an AP.
  --ap-psk PSK       AP passphrase (default: ministar; minimum 8 characters)
  --astap-db WHICH   both (default), g05, d20, or none
  --ref REF          git branch/tag/commit of the app (default: main)
  --repo URL         git repository URL
  --reboot           reboot when the installation finishes
  --no-reboot        do not reboot (default)
  --check            run the preflight checks and exit without changing anything
  --force            continue even if the astroberry dev unit is detected
  -h, --help         this text

Environment equivalents: MINISTAR_WIFI_COUNTRY, MINISTAR_AP_PSK,
MINISTAR_ASTAP_DB, MINISTAR_REF, MINISTAR_REPO.

The country code is required because the WiFi radio's legal channels, power limits
and AP support come from the regulatory domain. A missing or wrong domain can stop
the access point from starting, or put the device outside local radio rules. There
is no worldwide default; `00` (the world domain) is rejected.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --country) COUNTRY="${2:?--country needs a value}"; shift 2 ;;
        --ap-psk) AP_PSK="${2:?--ap-psk needs a value}"; shift 2 ;;
        --astap-db) ASTAP_DB="${2:?--astap-db needs a value}"; shift 2 ;;
        --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
        --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
        --reboot) REBOOT=1; shift ;;
        --no-reboot) REBOOT=0; shift ;;
        --check) CHECK=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (use --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run this as root (sudo bash install.sh --country ...)"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
trap 'printf "error: failed at line %s. Log: %s\\n" "$LINENO" "$LOG" >&2' ERR

[ -n "$COUNTRY" ] || die "--country is required: the WiFi regulatory domain decides which channels the radio may use and whether it may host an access point. Use the two-letter code where the Pi will operate, for example US."
COUNTRY=$(printf '%s' "$COUNTRY" | tr '[:lower:]' '[:upper:]')
[ "${#COUNTRY}" -eq 2 ] || die "--country must be a two-letter code, for example US"
[ "$COUNTRY" != "00" ] || die "--country 00 is the world domain, not a country; it can disable the access point. Use the country where the Pi will operate."
[ "${#AP_PSK}" -ge 8 ] || die "the AP passphrase must be at least 8 characters"
case "$ASTAP_DB" in both|g05|d20|none) ;; *) die "--astap-db must be both, g05, d20, or none" ;; esac

[ -f /etc/os-release ] || die "no /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
[ "${VERSION_ID:-}" = "13" ] || die "this installer targets Debian 13 (Trixie); found ${VERSION_ID:-unknown}"
case "${ID:-}" in debian|raspbian) ;; *) die "this installer targets Debian/Raspberry Pi OS; found ${ID:-unknown}" ;; esac
[ "$(uname -m)" = "aarch64" ] || die "this installer targets arm64/aarch64; found $(uname -m)"
if [ ! -e /proc/device-tree/model ]; then
    warn "no Raspberry Pi device tree found; the AP and camera udev rules may not apply"
fi

# Refuse the dev unit explicitly. It owns ports 80/443 and the camera on the Pi this
# project was developed on; overwriting it from here would strand an active session.
if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    if systemctl cat "$SERVICE" | grep -qE 'User=astroberry|/home/astroberry'; then
        [ "$FORCE" = 1 ] || die "the astroberry dev unit is installed. Use a fresh Pi, or stop it and pass --force."
    fi
fi
if ! systemctl is-active --quiet "$SERVICE"; then
    for port in 80 443; do
        if ss -ltn 2>/dev/null | grep -qE "[:.]$port[[:space:]]"; then
            die "port $port is already in use; stop the other web server first"
        fi
    done
fi

if ! curl -fsI --max-time 15 https://github.com >/dev/null 2>&1; then
    die "no HTTPS access to github.com; connect the Pi to a network first"
fi

log "checks passed"
if [ "$CHECK" = 1 ]; then
    printf 'check mode: no changes made\n'
    exit 0
fi

log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3-venv python3-pip python3-gphoto2 gphoto2 \
    dcraw exfatprogs \
    avahi-daemon avahi-utils \
    network-manager dnsmasq-base wpasupplicant iw rfkill wireless-regdb \
    openssl ca-certificates curl git unzip usbutils iproute2
if apt-cache show raspi-config >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends raspi-config
fi

log "creating the ministar user and directories"
if ! getent group plugdev >/dev/null 2>&1; then
    groupadd -r plugdev
fi
if ! id -u ministar >/dev/null 2>&1; then
    useradd --system --home "$DATA_DIR" --create-home --shell /usr/sbin/nologin ministar
fi
usermod -aG plugdev ministar
install -d -m 0755 -o ministar -g ministar "$DATA_DIR"
install -d -m 0755 -o root -g root "$APP_ROOT"
install -d -m 0750 -o root -g ministar "$ETC"
install -d -m 0750 -o root -g ministar "$TLS_DIR"

log "configuring the boot command line"
CMDLINE=
for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$candidate" ]; then CMDLINE=$candidate; break; fi
done
[ -n "$CMDLINE" ] || die "no cmdline.txt found under /boot"
add_cmdline_token() {
    token=$1
    if grep -q -- "$token" "$CMDLINE"; then
        return 0
    fi
    [ -f "$CMDLINE.ministar.bak" ] || cp "$CMDLINE" "$CMDLINE.ministar.bak"
    sed -i "1s|[[:space:]]*\$| $token|" "$CMDLINE"
}
# The default 16 MB usbfs buffer is too small for a 19-30 MB CR2 download.
add_cmdline_token "usbcore.usbfs_memory_mb=256"
add_cmdline_token "cfg80211.ieee80211_regdom=$COUNTRY"

log "installing ASTAP and star databases ($ASTAP_DB)"
TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
install -d -m 0755 /opt/astap
download_verified() {
    url=$1
    hash=$2
    target=$3
    curl -fsSL --retry 3 --connect-timeout 20 -o "$target" "$url"
    printf '%s  %s\n' "$hash" "$target" | sha256sum -c -
}
if [ "$ASTAP_DB" != "none" ]; then
    download_verified "$ASTAP_CLI_URL" "$ASTAP_CLI_SHA256" "$TMP/astap_cli.zip"
    unzip -q -o "$TMP/astap_cli.zip" -d "$TMP/cli"
    install -m 0755 "$TMP/cli/astap_cli" /opt/astap/astap_cli
fi
for db in g05 d20; do
    case "$ASTAP_DB" in
        both|"$db") ;;
        *) continue ;;
    esac
    case "$db" in
        g05) url=$G05_URL; hash=$G05_SHA256 ;;
        d20) url=$D20_URL; hash=$D20_SHA256 ;;
    esac
    download_verified "$url" "$hash" "$TMP/$db.deb"
    dpkg-deb -x "$TMP/$db.deb" "$TMP/$db"
    if [ -d "$TMP/$db/opt/astap" ]; then
        find "$TMP/$db/opt/astap" -maxdepth 1 -type f -exec install -m 0644 {} /opt/astap/ \;
    fi
done

log "fetching the app ($REF)"
if [ -d "$APP_SRC/.git" ]; then
    git -C "$APP_SRC" fetch --depth 1 origin "$REF"
    git -C "$APP_SRC" checkout -fq FETCH_HEAD
else
    rm -rf "$APP_SRC"
    git clone --no-checkout --depth 1 "$REPO" "$APP_SRC"
    git -C "$APP_SRC" fetch --depth 1 origin "$REF"
    git -C "$APP_SRC" checkout -fq FETCH_HEAD
fi

log "building the Python environment"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install --no-cache-dir -e "$APP_SRC[ui]" "nicegui==3.18.0"

log "setting hostname and mDNS"
hostnamectl set-hostname "$HOSTNAME"
if ! grep -qE "^127\\.0\\.1\\.1[[:space:]]+$HOSTNAME([[:space:]]|\$)" /etc/hosts; then
    printf '127.0.1.1 %s\n' "$HOSTNAME" >> /etc/hosts
fi
systemctl restart avahi-daemon

log "setting the WiFi country to $COUNTRY"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_wifi_country "$COUNTRY" || warn "raspi-config could not set the country"
else
    iw reg set "$COUNTRY" || warn "iw reg set failed; the cmdline regdom applies after reboot"
fi

log "generating this device's CA and TLS certificate"
sh "$APP_SRC/deploy/ministar-tls.sh" "$TLS_DIR"
# The app needs the leaf key and both public certificates. The CA private key is
# only used to sign this device's leaf; keep it root-only.
chown root:root "$TLS_DIR/ca.key"
chmod 0600 "$TLS_DIR/ca.key"
chown ministar:ministar "$TLS_DIR/leaf.key" "$TLS_DIR/ca.crt" "$TLS_DIR/leaf.crt"
chmod 0600 "$TLS_DIR/leaf.key"
chmod 0644 "$TLS_DIR/ca.crt" "$TLS_DIR/leaf.crt"
chown root:ministar "$TLS_DIR"
chmod 0750 "$TLS_DIR"

log "creating the access-point profile"
if [ -e /sys/class/net/wlan0 ]; then
    MAC=$(cat /sys/class/net/wlan0/address)
    SUFFIX=$(printf '%s' "$MAC" | tr -d ':' | tail -c 4)
    SSID="ministar-$SUFFIX"
    nmcli connection delete "$AP_PROFILE" >/dev/null 2>&1 || true
    nmcli connection add type wifi ifname wlan0 con-name "$AP_PROFILE" ssid "$SSID" \
        autoconnect yes connection.autoconnect-priority 100 \
        802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
        ipv4.method shared ipv4.addresses 10.42.0.1/24 \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PSK"
    nmcli connection modify "$AP_PROFILE" wifi.powersave 2 >/dev/null 2>&1 || true
else
    SSID=""
    warn "no wlan0; skipping the access point. The console will use the wired network."
fi

log "installing services and the USB auto-mount rule"
install -m 0644 "$APP_SRC/deploy/ministar.service" /etc/systemd/system/ministar.service
install -m 0755 "$APP_SRC/deploy/ministar-boot-mode.sh" /usr/local/sbin/ministar-boot-mode
install -m 0644 "$APP_SRC/deploy/ministar-boot-mode.service" /etc/systemd/system/ministar-boot-mode.service
install -m 0644 "$APP_SRC/deploy/ministar-wlan-next-boot.path" /etc/systemd/system/ministar-wlan-next-boot.path
install -m 0644 "$APP_SRC/deploy/ministar-wlan-next-boot.service" /etc/systemd/system/ministar-wlan-next-boot.service
install -m 0755 "$APP_SRC/deploy/ministar-usb-mount.sh" /usr/local/sbin/ministar-usb-mount
install -m 0644 "$APP_SRC/deploy/99-ministar-usb.rules" /etc/udev/rules.d/99-ministar-usb.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=block || true
systemctl daemon-reload
systemctl enable "$SERVICE" ministar-boot-mode.service ministar-wlan-next-boot.path >/dev/null
systemctl restart "$SERVICE" || true
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    die "ministar.service did not stay up; see journalctl -u ministar"
fi

log "writing the card for the case"
CA_FP=$(openssl x509 -in "$TLS_DIR/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)
BOOT_DIR=/boot/firmware
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
if [ -w "$BOOT_DIR" ]; then
    cat >"$BOOT_DIR/MINISTAR.txt" <<EOF
ministar

SSID:     ${SSID:-not configured}
PSK:      $AP_PSK
URL:      https://ministar.local
Hostname: $HOSTNAME
CA SHA256: $CA_FP

Join the access point if there is no known WiFi network, then open the URL.
If the browser warns, either continue once or install the CA from:
  http://10.42.0.1/ca.crt
EOF
    chmod 0644 "$BOOT_DIR/MINISTAR.txt"
fi

log "done"
printf '  console:  https://ministar.local (or https://10.42.0.1 on the AP)\n'
printf '  AP:       %s / %s\n' "${SSID:-not configured}" "$AP_PSK"
printf '  CA:       %s/ca.crt\n' "$TLS_DIR"
printf '  data:     %s\n' "$DATA_DIR"
printf '  log:      %s\n' "$LOG"
if [ "$REBOOT" = 1 ]; then
    log "rebooting to apply the cmdline and services"
    systemctl reboot
else
    printf '\nReboot when convenient: the usbfs buffer and the WiFi country take effect then.\n'
fi
