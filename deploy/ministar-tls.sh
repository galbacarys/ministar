#!/bin/sh
# Create the per-device CA and leaf used by the ministar console.
#
# The CA is what makes a clean HTTPS experience possible: install it once on the
# phone and the warning disappears. Without installing it, the leaf is simply
# untrusted and the browser warns, which is the acceptable default. Never ship these
# files in an image -- every device gets its own pair at first boot.
#
# The SANs are the names a user can type:
#   DNS  ministar.local, ministar, localhost
#   IP   10.42.0.1 (AP), 127.0.0.1, every current IPv4 address
# The hostname is the stable name; LAN DHCP addresses are included as a convenience
# and are regenerated when the script is re-run with MINISTAR_TLS_FORCE=1.
set -eu

DIR="${1:-$HOME/.ministar/tls}"
FORCE="${MINISTAR_TLS_FORCE:-0}"
mkdir -p "$DIR"
chmod 700 "$DIR"

DNS="ministar.local,ministar,localhost"
IPS="10.42.0.1,127.0.0.1"
for ip in $(hostname -I 2>/dev/null || true); do
    case "$ip" in
        *:*) continue ;;  # IPv6 is deliberately not in the SAN list
    esac
    case ",$IPS," in
        *",$ip,"*) ;;
        *) IPS="$IPS,$ip" ;;
    esac
done
SAN="DNS:$(printf '%s' "$DNS" | sed 's/,/,DNS:/g'),IP:$(printf '%s' "$IPS" | sed 's/,/,IP:/g')"

if [ ! -f "$DIR/ca.crt" ] || [ ! -f "$DIR/ca.key" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$DIR/ca.key" -out "$DIR/ca.crt" \
        -subj "/CN=ministar local CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
fi

if [ "$FORCE" = "1" ] || [ ! -f "$DIR/leaf.crt" ] || [ ! -f "$DIR/leaf.key" ]; then
    openssl req -newkey rsa:2048 -nodes \
        -keyout "$DIR/leaf.key" -out "$DIR/leaf.csr" \
        -subj "/CN=ministar.local" \
        -addext "subjectAltName=$SAN"
    openssl x509 -req -in "$DIR/leaf.csr" \
        -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" -CAcreateserial \
        -days 825 -sha256 -copy_extensions copy \
        -out "$DIR/leaf.crt"
    rm -f "$DIR/leaf.csr" "$DIR/ca.srl"
fi

chmod 600 "$DIR/ca.key" "$DIR/leaf.key"
chmod 644 "$DIR/ca.crt" "$DIR/leaf.crt"
printf 'certificates in %s\n' "$DIR"
printf 'SANs: %s\n' "$SAN"
