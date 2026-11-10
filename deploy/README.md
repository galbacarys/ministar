# ministar on a Raspberry Pi

This is the MVP appliance path: a fresh Raspberry Pi OS Lite (or Debian 13 arm64)
system, one script, then the console is served over HTTPS with its own access point
when the Pi has no known network.

The image build will call the same provisioning steps later. The script is
idempotent: re-running it updates the checkout and the units while preserving
`/var/lib/ministar`, `/etc/ministar/tls`, and any WiFi client profile.

## Before you start

- Raspberry Pi OS Lite **64-bit Trixie**, or Debian 13 arm64 on a Pi.
- SSH enabled and the Pi on a network with internet access (Imager can set this up).
- A camera is optional at install time; connect the EOS body when you are ready.
- Do not run this on the existing astroberry development Pi. It refuses when it
  finds that unit, because the dev service already owns ports 80/443 and the camera.

## Install

Inspect first:

```bash
curl -fsSLO https://raw.githubusercontent.com/galbacarys/ministar/main/deploy/install.sh
less install.sh
sudo bash install.sh --country US
```

Or the one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/galbacarys/ministar/main/deploy/install.sh \
  | sudo bash -s -- --country US --reboot
```

`--country` is required, and `US` above is only an example. The code is the WiFi
regulatory domain, not a locale: it decides which channels and transmit power the
radio may use and whether it may host an access point. A missing or wrong domain can
stop the AP from starting, or put the device outside local radio rules. There is no
worldwide default; `00` (the world domain) is rejected. Use the country where the Pi
will operate.

Add `--reboot` to apply the `cmdline.txt` changes (usbfs memory and the country) in
one step, or reboot yourself afterwards.

Useful options:

| Option | Meaning |
|---|---|
| `--country CC` | WiFi regulatory country where the Pi will operate, e.g. `US` (required; see above) |
| `--ap-psk PSK` | AP passphrase, default `ministar`, minimum 8 characters |
| `--astap-db both\|g05\|d20\|none` | Star databases; default `both` (G05 101 MB + D20 393 MB) |
| `--ref REF` | Git branch/tag/commit of the app, default `main` |
| `--repo URL` | Git repository, default the public ministar repo |
| `--check` | Run the checks and exit without changing anything |
| `--reboot` / `--no-reboot` | Whether to reboot at the end |
| `--force` | Continue even if the astroberry dev unit is detected |

The log is `/var/log/ministar-install.log`.

## What it configures

- apt: gphoto2 bindings, dcraw, exFAT, avahi, NetworkManager/dnsmasq, iw/rfkill
- ASTAP `astap_cli` + G05/D20 in `/opt/astap`
- a `ministar` system user, `/var/lib/ministar`, `/etc/ministar/tls`
- the app at `/opt/ministar/app` and its venv at `/opt/ministar/venv`
- hostname `ministar`, avahi, WiFi regulatory country
- `usbcore.usbfs_memory_mb=256` in `cmdline.txt`
- a `ministar.service` on **443** with a port-80 landing/CA/captive page
- a `ministar-<last4 of wlan0 MAC>` AP as the **on-boot default**, PSK `ministar`
- a one-shot `wlan-next-boot` path unit and the SETUP "reboot in WLAN mode" button:
  the next boot joins the known WiFi client, and the boot after returns to AP
- a boot-mode service that selects AP or WLAN before the console starts
- a udev rule that auto-mounts labelled FAT/exFAT USB sticks under `/media/<label>`
  and makes the `ministar` user their owner
- `/boot/firmware/MINISTAR.txt` with the SSID, PSK, URL and CA fingerprint

## After the reboot

The Pi boots into **AP mode by default**. It does not join a known WiFi automatically.
The SETUP screen has **"reboot in WLAN mode"**: it writes a one-shot flag, reboots,
and the next boot joins the known WiFi client. The flag is consumed at boot, so the
boot after returns to AP.

- **AP:** `ministar-xxxx` / `ministar` (the exact SSID is in `MINISTAR.txt`)
- **Console:** `https://ministar.local` (or `https://10.42.0.1` on the AP)
- **Plain HTTP:** `http://ministar.local` redirects to HTTPS and serves the CA
- **CA:** `http://10.42.0.1/ca.crt`
- **WLAN one-shot:** the next boot joins the known WiFi, the AP is down, and the
  console is at the Pi's LAN address; the following boot returns to AP

The browser will warn because this is a per-device, self-signed CA. You can continue
once (geolocation still works after that), or install the CA for a clean experience:

- **iOS:** open `http://10.42.0.1/ca.crt`, install the profile, then Settings →
  General → About → Certificate Trust Settings and enable full trust.
- **Android:** download the same file, then Settings → Security → Encryption &
  credentials → Install a certificate → CA certificate. Restart Chrome if needed.

The certificate's SANs include `ministar.local`, `ministar`, `10.42.0.1`,
`127.0.0.1` and the Pi's current IPv4 addresses.

**Phone VPN note:** Tailscale or another VPN on the phone can intercept `.local` and
local-network traffic. Turn it off while using the console; the Pi's own Tailscale
can stay for remote administration.

## USB storage

Format the stick as **exFAT** (or FAT32) with a label. Plug it in; udev mounts it at
`/media/<label>` with `uid`/`gid` set to the `ministar` user. The SETUP output
dropdown lists internal storage and every mounted stick; the first writable USB stick
is the default output root (`/media/<label>/ministar`). The service has write access
to `/media`, and the app refuses to create a `/media/...` path if the stick is not
actually mounted.

ext4 sticks are not auto-mounted by this rule (their `uid=`/`gid=` options do not
exist). Mount one manually if you need it; the app will detect it, but the service
user must be able to write to it.

## Updates

Re-run the installer. It fetches the requested ref, rebuilds the venv, refreshes the
units, and restarts the service. Session data, TLS material, the AP profile and the
WiFi client profile are preserved. Do not use `--force` to overwrite the astroberry
dev unit unless you have stopped that service.

## Troubleshooting

```bash
systemctl status ministar
journalctl -u ministar -f
tail -f /var/log/ministar-install.log
/opt/ministar/venv/bin/python -m ministar preflight
nmcli device status
journalctl -t ministar-usb
lsblk -f
```

If the AP does not appear, check the boot-mode log and the profile:

```bash
journalctl -u ministar-boot-mode -b
systemctl status ministar-boot-mode
nmcli connection show ministar-ap
iw reg get
rfkill list
```

If a one-shot WLAN boot was requested and the known WiFi is not in range, that boot
has no AP; reboot once more to return to the AP default.

If the camera is missing, check the USB cable and mode, then:

```bash
gphoto2 --auto-detect
```

## Security notes

- The AP uses a fixed PSK, `ministar`, by decision: the appliance is easier to use,
  and the network is its boundary. It has no login. Do not expose it on an untrusted
  LAN.
- `curl | sudo bash` runs a root shell from the internet. Prefer the inspect-first
  command; pin `--ref` to a tag or commit for a release.
- The CA private key stays root-only on the device; the app only reads the leaf key
  and the public certificates. No keys are committed to the repository.
