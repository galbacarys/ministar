# 17 — Raspberry Pi port: backend, image and zero-touch provisioning

**Status:** scoping, 2026-11-10. Builds on 14 (portability) and 02 §2.2 (libgphoto2). Decides
nothing by itself; sizes the work, names the risks and lists the choices that need a decision.

**The ask:** flash a card (or run one command), power the Pi, plug in the camera, open
`http://ministar.local` on a phone, image. No SSH, no IP addresses, no terminal. If a known WiFi
network is present the Pi joins it; if not, it hosts its own.

---

## 0. Verdict

- **The astronomy is already portable.** 14 §3 was right: the `Camera`/`FocusDriver` seam and the
  pump thread are the port boundary. The work is **~15 % camera driver, ~85 % packaging and device
  operations**.
- **Every dependency has an aarch64 path.** Confirmed in this pass: ASTAP ships a single 940 KB
  aarch64 `astap_cli`; `gphoto2` wheels ship aarch64 libgphoto2 *and* camlibs; numpy/OpenCV have
  aarch64 wheels for Python 3.13; NiceGUI is pure Python. Nothing needs compiling on the device.
- **Recommended shape:** Raspberry Pi OS Lite arm64 (Trixie) + `python-gphoto2` backend + NiceGUI
  on 443 with a per-device certificate + a tiny HTTP landing page on 80 + NetworkManager
  client-or-AP + avahi (already in the image) + a systemd unit, shipped as both a prebuilt
  `.img.xz` and a one-command `install.sh` that share one provisioning core.
- **HTTPS is required for the location button.** `ministar.local` is not a secure context, so the
  geolocation code already in `screens.py` cannot work over plain HTTP. A self-signed cert fixes
  it after one warning; a per-device local CA removes the warning after a one-time install.
- **Size:** MVP 12–17 engineer-days; shippable v1 39–62 (≈8–10 calendar weeks for one person),
  excluding the 14 §4 performance work already tracked separately.
- **The real risks are physical, not architectural:** live-view latency over gphoto2 on Pi, focus
  drive on the 60D over PTP, timekeeping with no internet, and power/thermal behaviour. M0 (§14) is
  a hardware spike that retires them before the image work starts.

---

## 1. What earlier briefs already settled

Do not re-litigate these:

| Decision | Source |
|---|---|
| libgphoto2 is the Linux backend; `manualfocusdrive` has the same three-tier structure as EDSDK | 14 §3.1 |
| `manualfocusdrive` calls must alternate with `None` (libgphoto2 #767) | 14 §3.2 |
| Live stacking at ~2K, not full resolution | 14 §4.4 |
| Solve every N frames and dead-reckon in between | 14 §4.2 |
| Frames on SSD, not SD | 14 §5 |
| Two power rails; the shutter is the brownout risk | 14 §5 |
| Phone is the runtime limit; red-only UI is a hard requirement | 14 §5–6 |
| G05 is required at 100 mm; D80 cropped an 8.78° frame | field notes 2026-11-07 |

---

## 2. Platform baseline

### 2.1 Hardware

| Board | Verdict |
|---|---|
| **Pi 5, 4 GB** | **Primary.** 4× Cortex-A76, PCIe for the SSD, battery-backed RTC header, proper power button, active cooler ecosystem. |
| Pi 4, 2 GB | Supported. Same USB2 camera link; no RTC; slower stack but 14 §4 says the camera link is the ceiling, not the CPU. |
| Pi 3 / Zero 2 W | **Not supported.** ARMv8 but too little RAM and too few cores for 2K stacking; the support burden is not worth it. |

The camera is USB 2 on every board, so USB 3 buys nothing for capture. It matters only for the
frames SSD.

### 2.2 OS

**Raspberry Pi OS Lite arm64, Trixie (Debian 13), 2026-10-06 or later.** Verified in that image's
package manifest:

- Python **3.13.5**
- NetworkManager **1.52** (+ `wpasupplicant`, `dnsmasq-base`)
- `avahi-daemon` **0.8** — already installed, so `.local` is not new work
- `openssl` **3.5.7**, `ca-certificates`, `python3-cryptography` **43** — certificate generation
  needs no new package
- `raspi-config`, `exfatprogs`, `ntfs-3g`, `gpiod`, `systemd` **257**

Not present, must be added: `gphoto2`/`python3-gphoto2`, `dcraw`, ASTAP, NiceGUI. `libcamera` is
irrelevant — this is a DSLR over PTP, not a CSI sensor.

### 2.3 Dependency availability (verified)

| Component | aarch64 / Py 3.13 | Note |
|---|---|---|
| `gphoto2` (Python) | **wheel** `cp313 manylinux_2_28 aarch64` | The wheel **vendors `libgphoto2.so` and all camlibs**; no apt libgphoto2 needed at runtime. |
| `numpy` | wheel | cp313 aarch64. |
| `opencv-python-headless` | wheel `cp37-abi3` | Works on 3.13. |
| `nicegui` | pure Python | No binary dependency. |
| ASTAP `astap_cli` | **aarch64 zip, 940 KB, single binary** | `NEEDED`: libpthread, libdl, libc only. Effectively static. |
| ASTAP star DB | G05 **101 MB**, D20 **393 MB**, D50 867 MB, D80 1.2 GB | Ship **G05 + D20**. D80 is the wrong DB for the 2–9° target. |
| `dcraw` / `dcraw_emu` | `dcraw` and `libraw-bin` in Trixie arm64 | Keep the existing subprocess + flags contract. |
| `python3-gphoto2` | Debian package exists in Trixie | Fallback if the pip wheel ever lags Python. |

Consequence: the image is **~1.3–1.6 GB compressed** (base + ~500 MB star DB + venv), ~3 GB on
disk. An 8 GB card is enough for the OS; the frames SSD is separate.

---

## 3. Workstream A — camera backend (`camera/gphoto2.py`)

### 3.1 The interface is wider than the protocol

`Camera` in `base.py` is the documented contract, but `Station` duck-types a larger surface. The
Linux driver must implement **all** of this, not just the Protocol:

| Member | Used by | Notes |
|---|---|---|
| `connect`, `disconnect`, `connected`, `name` | Station | `connect` runs on the pump thread, never the event loop. |
| `get_settings`, `apply`, `iso_options` | Station, screens | ISO/Tv/Av; snap Tv with the existing `snap_shutter`. |
| `capture_to(path)` | Station, runner | **Must write the CR2 to `path`** — the runner reads the body's file. |
| `grab_live_jpeg() -> bytes` | Station | Called on the pump thread. |
| `live_view() -> Frame` | protocol | Optional convenience; implement for parity/tests. |
| `stop_live_view()` | Station, shutdown | Mirror down / preview off. |
| `pulse(direction, count)` | Station | Focus drive; alternation quirk lives here. |
| `position`, `reset_position` | Station, UI | gphoto2 has no position readback; emulate by counting pulses, persist per session. |
| `keep_alive` | optional | No-op if gphoto2 does not need it. |
| `disposable_frames` | Station | `False`; frames are files. |

Work item: add a narrower `StationCamera` Protocol (or capability flags) so the next backend does
not rediscover this list. ~1 day, pays for itself immediately.

### 3.2 Widget mapping

| App concept | gphoto2 |
|---|---|
| ISO | `/main/imgsettings/iso` — read the choice list; do not assume the EDSDK ISO set. |
| Tv | `/main/capturesettings/shutterspeed` — string choices (`1/60`, `0.5`, `2`); map from `snap_shutter`. |
| Av | `/main/capturesettings/f-number`. |
| Capture | `gp_camera_capture` then `gp_camera_file_get`; set `capturetarget` to **Internal RAM by label**, not by guessed index. |
| Live view | `gp_camera_capture_preview` → JPEG bytes; decode with OpenCV where a `Frame` is needed. |
| Focus | `manualfocusdrive` radio choices (`Near 3` … `Far 3`); interleave `None` between every call; map `Direction`→choice. |
| Mirror lockup | probe for `mirrorlockup`; if absent, report a capability miss, do not fail the session. |
| LENR | probe for `longexpnoisereduction`; Canon PTP support varies. |
| Bulb | out of scope (08 §6). |

### 3.3 Quirks that will bite

1. **Focus alternation.** A naive `Near3 × 419` does almost nothing. Every pulse must be
   `Near3, None, Near3, None, …`. This is the single most likely silent bug in the port.
2. **Thread affinity and wedged PTP.** All libgphoto2 calls for one context happen on one thread
   (the existing pump is exactly right). A timeout is not recoverable by retrying: call
   `gp_camera_exit`, drop the context, re-enumerate, and tell the Station to rebuild. Put a
   per-call watchdog around capture/preview; libgphoto2 will otherwise sit on a dead USB pipe.
3. **USB memory.** `usbfs_memory_mb` defaults to 16 MB; a 25–30 MB CR2 download can fail with
   unhelpful I/O errors. Add `usbcore.usbfs_memory_mb=256` to `/boot/firmware/cmdline.txt`.
4. **USB autosuspend.** Disable it for the camera's device node via udev, or the body sleeps
   between frames and every third capture times out.
5. **Permissions.** Ship the libgphoto2 udev rule; run the service with `SupplementaryGroups=plugdev`.
6. **Capture target.** Internal RAM avoids depending on the card and avoids the card filling at a
   dark site. It also makes download mandatory, which is what we want (frames go to the SSD).
7. **Camera USB mode.** The body must be in PTP / "PC connection" mode. Verify the 60D's exact
   menu label and check it in preflight; a body left in the other mode looks like a bad cable.

### 3.4 What does not change

Focus maths, V-curve, motion planner, solver, stacking, session state, UI screens. 14 §3.3's table
stands. The only edits outside `camera/` are tool discovery and preflight (§4).

### 3.5 Validation that needs the body

These are the M0 questions; none can be answered by reading:

- Does the **60D** expose `manualfocusdrive` over PTP, and does one step move the same direction as
  EDSDK's `DriveLens`? (Confirmed on other Canon bodies, not this one.)
- Live-view frame rate and JPEG size on Pi 4/5; is a focus sweep still responsive?
- Capture→download time for one CR2 on the Pi's USB2 port.
- Are `mirrorlockup` and `longexpnoisereduction` present? If not, what replaces the EDSDK behaviour?
- Does a long session survive a camera sleep, or must autosuspend be disabled globally?

---

## 4. Workstream B — platform seams in the current code

| File | Change | Effort |
|---|---|---|
| `config.py` | Linux `default_home()` → `/var/lib/ministar`; per-platform `ASTAP_EXE` (`astap_cli`), `DCRAW_EXE` (`dcraw`/`dcraw_emu`); Linux search paths (`/opt/astap`, `/usr/bin`); data root may be the SSD mount. | 1 d |
| `preflight.py` | Replace the EDSDK check with a Linux suite: gphoto2 import + version, udev/permissions, `usbfs_memory_mb`, autosuspend, ASTAP + DB tiles, dcraw, disk free/type, undervoltage, throttling, time source, network mode. Keep the Windows checks behind the platform branch. | 2 d |
| `cli.py` | Linux names/paths; new `doctor` (support bundle), `capture-test`, `net` (status/AP), `time` commands. | 1 d |
| `camera/__init__.py` | Backend selection (`MINISTAR_CAMERA=edsdk|gphoto2|fake`, else platform default); stop importing EDSDK unconditionally. | 0.5 d |
| `ui/app.py` | `--no-show`/port defaults on Linux; `--ssl-certfile`/`--ssl-keyfile` pass-through to `ui.run`; session persistence (already pending — now a prerequisite); `/api/time`, `/api/site/location`, `/api/power`, `/api/wifi`; shutdown authorisation. | 2 d |
| `tests/` | Fake `gphoto2` module; driver state machine; golden NetworkManager profiles; firstboot idempotence; helper allow-list. | 2 d |

---

## 5. Workstream C — device runtime

### 5.1 Layout

```
/opt/ministar/            app + venv (read-only to the service)
/var/lib/ministar/        config, sessions, logs on SD (small, write-light)
/media/ministar/          USB SSD, label MINISTAR-DATA (frames)
/etc/ministar/            field-mode flag, image version
/etc/ministar/tls/        per-device CA + leaf key/cert (0700, not in the image)
```

User `ministar`, group `plugdev`. The service never runs as root.

### 5.2 Unit sketch

```ini
[Unit]
Description=ministar
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=ministar
Group=ministar
SupplementaryGroups=plugdev
Environment=MINISTAR_HOME=/var/lib/ministar
Environment=MINISTAR_CAMERA=gphoto2
ExecStart=/opt/ministar/venv/bin/python -m ministar.ui.app --host 0.0.0.0 --port 443 \
  --ssl-certfile /etc/ministar/tls/leaf.crt --ssl-keyfile /etc/ministar/tls/leaf.key --no-show
Restart=always
RestartSec=2
TimeoutStopSec=15
KillSignal=SIGTERM
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/ministar /media/ministar /run/ministar
[Install]
WantedBy=multi-user.target
```

- **Ports 443 and 80.** The console is HTTPS on 443; the HTTP server on 80 is a stateless landing
  page, captive-probe target and CA download (§6.6). `CAP_NET_BIND_SERVICE` covers both without
  root. Keep `--port 8080` as an escape hatch.
- **No HSTS.** It makes the self-signed warning impossible to bypass and buys nothing on a private
  AP.
- **SIGTERM handler** must stop live view, put the mirror down, close the gphoto2 context, then
  exit. This is the same teardown `/_shutdown` already performs on Windows; wire it to the signal.
- **Camera absent is not a crash.** The process starts, shows "connect camera", and retries. If the
  unit exits when the body is unplugged, boot without a camera cannot be diagnosed on the phone.
- **Watchdog:** `WatchdogSec` + `sd_notify` from the app, or a `ministar-health.timer` that restarts
  after 60 s without an HTTP 200. Prefer the former; the latter is a fallback.

### 5.3 Storage

- systemd mount unit for `LABEL=MINISTAR-DATA` at `/media/ministar`, `nofail`, `noatime`.
- Frames go to `/media/ministar/frames`; sessions and config stay on SD.
- If the SSD is absent, fall back to `/var/lib/ministar/data/frames` **and say so** on the status
  bar. Do not silently fill the card.
- Free-space guard: warn below 5 GB, stop capture below 2 GB.
- Export without Samba: a streaming zip of a session over HTTP (phone/laptop), plus "pull the SSD
  and read it on a PC". Samba is a service and an attack surface for no gain here.

### 5.4 Power, thermals, clock

- Separate rails: Pi 5 5 V/5 A PD; 60D on a dummy battery (ACK-E6). Common ground, bulk capacitance
  on the camera rail. Verify no brownout at shutter release.
- `vcgencmd get_throttled` and the SoC temperature surfaced in preflight and the status bar.
  Warn before a session if the Pi is already throttled.
- Pi 5 RTC + battery, set once from NTP or browser; **Pi 4 has no RTC**. Without internet the clock
  resets on power loss. Add `POST /api/time` (browser `Date.now()`, confirmed in the UI) and prefer
  an RTC where present. A few minutes of error is tolerable for pointing; a wrong date is not
  tolerable for filenames and `DATE-OBS`.
- `dtparam=watchdog=on` + systemd `RuntimeWatchdogSec`; power button → clean shutdown.
- Active cooler on Pi 5; document the case requirement.

---

## 6. Workstream D — networking and naming

### 6.1 Boot state machine

One radio, so client and AP are **mutually exclusive**. Do not promise concurrent AP+STA.

```
boot
 ├─ NetworkManager tries known client profiles (autoconnect-priority 100)
 ├─ 20 s timer: no default route?  → activate ministar-ap
 └─ if a known profile appears later → stay on AP; offer "join home" in the UI
```

- `ministar-wifi-fallback.service` (oneshot, `After=NetworkManager-wait-online.service`) checks
  `nmcli -t -f GENERAL.STATE dev show wlan0`; if not connected, `nmcli con up ministar-ap`.
- **Field mode** (`/etc/ministar/field-mode`) skips the 20 s wait and goes straight to AP. At a dark
  site there is no home network, and 20 s of scanning at every boot is pure annoyance. Toggle from
  the device page.
- Set the WiFi country (`raspi-config nonint do_wifi_country`) at first boot. An unset country can
  leave the radio/AP disabled.

### 6.2 AP profile

```bash
nmcli con add type wifi ifname wlan0 con-name ministar-ap \
  ssid "ministar-$(cat /sys/class/net/wlan0/address | tr -d ':' | tail -c 5)" autoconnect no \
  802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk ministar
```

- **WPA2 with the fixed PSK `ministar`.** This is a deliberate appliance decision: ease of use
  beats a per-device key. It is also honest about the threat model -- the AP is the perimeter, the
  app has no login, and anyone within range who knows the default can join. Do not rely on it on
  an untrusted LAN; a "change PSK" button can be added later if that changes.
- **SSID `ministar-<last four hex of the wlan0 MAC>`** (for example `ministar-bf42`), so two units
  at one site are visibly distinct. The hostname stays `ministar`, so the URL remains
  `ministar.local`; each AP is its own subnet, and a shared LAN is the only case where avahi has
  to rename one to `ministar-2.local`.
- 2.4 GHz, channel 6: range and phone compatibility beat throughput. Bandwidth is irrelevant
  (14 §5: ~1.2 Mbit/s).
- Shared-mode dnsmasq additions in `/etc/NetworkManager/dnsmasq-shared.d/ministar.conf`:
  `dhcp-option=114,http://10.42.0.1/` and `address=/ministar.local/10.42.0.1` for clients that do
  not do mDNS.

### 6.3 mDNS

- `avahi-daemon` is already in the Lite image. `hostnamectl set-hostname ministar` and avahi
  publishes `ministar.local` with no extra code.
- Add `/etc/avahi/services/ministar.service` (`_http._tcp`, port 80) for discovery UIs. Optional,
  cheap, does not affect browsers.
- **Caveat, test on real phones:** iOS/macOS/Windows/Linux resolve `.local` reliably. Android's
  system resolver historically does not; Chrome/Samsung may. Therefore **always print and serve
  `http://10.42.0.1` as well**, and use the captive portal sheet as the primary phone path.
  "Any browser via local DNS" is true for most clients, not all; the IP is the guarantee.
- Two units → `ministar-2.local`; avahi renames on conflict. Fine, but the label should carry the
  actual hostname if we ever ship more than one.

### 6.4 Captive portal

- Port 80 is a separate stateless landing server (§6.6), not the NiceGUI app. It answers
  `/generate_204`, `/hotspot-detect.html`, `/connecttest.txt` and `/ncsi.txt` with a plain HTML
  page that explains the next step, and serves `/ministar-ca.crt` and `/health`. Everything else
  gets a 302 to HTTPS.
- **Do not redirect the probe paths to HTTPS.** iOS and Android render them in a restricted sheet
  that handles a certificate warning badly; the sheet must get plain HTTP.
- `dhcp-option=114` points at the HTTP landing page, not the HTTPS console.
- A `/setup` page (scan, join home WiFi) lives in the console; it is also the recovery path when
  the saved home network changes.

### 6.5 Security

- The WPA2 AP is the boundary, exactly as the LAN/Tailscale boundary is today. The app has no
  login; document that plainly. The PSK is the fixed `ministar` for appliance convenience, so the
  boundary is a known default: fine at a dark site, not a public-network control plane.
- Optional 4-digit PIN later if a user asks.
- Destructive actions (`reboot`, `poweroff`, `wifi change`, `update`) are POST + CSRF token +
  confirmation; never a GET the browser can prefetch.
- `nftables` default deny inbound except 80, 443 (8080 optional) and avahi; AP clients only.
- No cloud, no telemetry, no remote access by default.

### 6.6 HTTPS, secure contexts and phone capabilities

**Why this is not cosmetic.** `navigator.geolocation` is gated on a secure context. `localhost`,
`*.localhost` and loopback are potentially trustworthy; **`ministar.local` is not** — `.local` is
just a name, and no browser treats it as secure. The "find me" button already in `screens.py`
says exactly this in its error message; on the Pi it would fail every time.

**Certificate strategy.** Generate one **per-device CA** and one **leaf** at first boot, with the
`openssl` already in the image. There are two ways to use it, and one thing never to do:

| Option | Warning | Setup | Verdict |
|---|---|---|---|
| Leaf signed by the device CA, CA **not** installed | one tap-through per browser (may repeat after a browser restart) | none | **default** |
| Same leaf, CA installed on the phone | none after install | 2–6 taps, once per phone | **offer** |
| Shared CA/leaf baked into the image | none | none | **never** — one extracted key compromises every device and every phone that trusted it |

So the "self-signed" experience is the default at zero setup cost; installing the CA is the
optional upgrade that removes the warning. Never ship keys.

- SANs: `DNS:ministar.local`, `DNS:ministar`, `DNS:ministar-<id>.local`, `IP:10.42.0.1`,
  `IP:127.0.0.1`. Client-mode DHCP addresses change, so the hostname is the stable name.
- CA 10 years, leaf 825 days; regenerate the leaf at boot when it has < 30 days left. No network,
  no ACME, no renewal service.
- Keys under `/etc/ministar/tls`, owned `ministar:ministar`, mode `0700`; the service reads them
  directly.
- Optional hardening: `nameConstraints` on the CA limited to `ministar.local` /
  `.ministar.local` and the AP subnet. Test it on iOS and Chrome; if either rejects the CA, drop
  the constraint rather than ship a CA that cannot install.

**Serving model.** NiceGUI 3.18 accepts `ssl_certfile`/`ssl_keyfile` as uvicorn kwargs (verified
in this pass) and flips its own protocol to `https`, so no reverse proxy is needed:

- **443:** NiceGUI, websockets included (`wss` follows the page origin).
- **80:** a ~60-line `http.server` landing page with no application state: probes, CA/profile
  download, health, redirect to HTTPS.
- HSTS off, as above. Do not split the app across two origins; one HTTPS origin is what makes the
  permission prompts and the websocket behave.

**Geolocation flow.** The UI already has the button; make it succeed:

1. `find me` checks `window.isSecureContext`; on plain HTTP it links to the HTTPS URL instead of
   reporting a generic refusal.
2. `getCurrentPosition` with `enableHighAccuracy: true`, `timeout: 15000`, `maximumAge: 0`.
3. POST `{lat, lon, accuracy_m, altitude_m?, timestamp}` to `/api/site/location`; validate with
   `site.validate`; store provenance (`"phone"` + accuracy) with the coordinates. Never overwrite
   a manually entered site without confirmation.
4. Show the accuracy and keep the manual fields as the fallback. GPS is good to tens of metres;
   phone altitude is unreliable, so do not feed it to the sky model.
5. Distinguish denied / unavailable / timeout in the message. Denied is a phone-settings problem,
   not a bug.

There is no reverse geocoding: the device is offline. The site name stays free text.

**Free wins from the same origin:** Screen Wake Lock (the phone not dimming mid-session), PWA
install / service worker, clipboard for logs. These are why an HTTPS-first console is a better
shape than an HTTPS-only `/locate` side page.

**Trust cost, stated plainly.** Installing a CA is a real trust decision: that CA can vouch for
any hostname to that phone unless name constraints hold. It is the right trade for a personal
appliance and the wrong default for a shared one. Keep the CA per device, keep the key on the
device, show its fingerprint in the UI, and offer rotate/forget beside the install instructions.

---

## 7. Workstream E — setup UX and delivery

### 7.1 Three paths, one provisioning core

| Path | User effort | Our effort | Verdict |
|---|---|---|---|
| Prebuilt image (`ministar-pi-*.img.xz`) | flash, boot | high first time, low ongoing | **ship this** |
| `install.sh` on stock RPi OS Lite | flash, boot, one command | low | **ship this too** |
| Imager customisation only | flash with WiFi pre-set | none | not enough (no app) |

Both paths call the same `deploy/provision.sh`: apt packages, venv, units, NetworkManager profiles,
hostname, SSID suffix, ASTAP + DB, SSD mount, firstboot marker. The image is a thin layer around it; if
the two drift, the installer is the one to trust.

### 7.2 The user journey (acceptance criteria)

1. Flash `ministar-pi-<ver>.img.xz` with Raspberry Pi Imager (or run `curl … | sudo bash` on a
   fresh Lite install).
2. Optionally, Imager's own customisation pre-sets WiFi; it writes a NetworkManager client profile
   itself, and firstboot detects that instead of re-reading it. No profile → AP mode.
3. Insert the card, connect the camera, power on.
4. Boot to serving in **≤ 40 s**; the camera connects when it is ready, not as a boot gate.
5. Phone: join `ministar-<id>` (PSK `ministar`, shown on the label / `MINISTAR.txt`). The captive sheet opens the plain
   HTTP landing page; tap "Open console" → `https://ministar.local` (or `https://10.42.0.1`).
   Accept the certificate once, or install the CA first and see no warning at all.
6. PREFLIGHT is green; the wizard behaves as it does on Windows.
7. Optional: `/setup` joins home WiFi; the AP goes down and `ministar.local` follows the device onto
   that LAN.
8. Power off from the UI; pull the SSD or download the session over HTTP.

**Bar:** no SSH, no terminal, no IP address, no config file editing.

### 7.3 First boot

`ministar-firstboot.service` (oneshot, idempotent, runs before `ministar.service`):

- set hostname, WiFi country, derive the SSID suffix from the wlan0 MAC (PSK is fixed);
- generate the per-device CA + leaf (§6.6), write `ministar-ca.crt` and `ministar.mobileconfig`
  beside the config; never bake a key into the image;
- read `/boot/firmware/ministar-wifi.txt` if present (`ssid=`/`psk=`), create a client profile,
  disable field mode; otherwise enable AP;
- write `/boot/firmware/MINISTAR.txt` (SSID, PSK, URL, hostname, CA fingerprint) — this is what the
  user reads on a PC, so it must be FAT-visible; the CA file sits next to it for offline transfer;
- enable units, mount the SSD if present, mark done.

A printed label with SSID/PSK/URL is better than a QR code for a field device: it works with no
power and no second screen. Generate it from the same values in `provision.sh`.

### 7.4 Image build

Use **`rpi-image-gen`**, not `pi-gen`:

- Official Raspberry Pi tool, active, declarative YAML layers + hooks, runs **as a normal user** on
  a Pi 5 or an arm64 Debian Trixie host, no chroot-root.
- Base: `trixie-minbase` (or the Lite layer) + firmware for Pi 4/5.
- Custom layer: apt packages, files, venv, `astap_cli` + G05/D20, units, profiles, avahi service,
  `/boot/firmware/cmdline.txt` (`usbfs_memory_mb=256`, watchdog), firstboot. TLS material is
  generated on first boot, not built into the image.
- Output `.img` → `.img.xz` + `.sha256` + `.bmap`. Pin apt and wheel versions; cache the wheelhouse
  and ASTAP downloads, or every build re-downloads 600 MB.
- First build ~30–60 min; incremental ~10–20 min.
- `pi-gen` remains the tool for reproducing official RPi OS; do not use it for this.

### 7.5 Updates

There is no internet at a dark site, so no auto-update. Two honest options:

- **Reflash (recommended).** Keep config and sessions on the SSD (`MINISTAR-DATA`); reflashing the
  SD loses nothing the user cares about. The image version is visible in the UI.
- **`ministar update` from a USB stick:** signed tarball, verify, swap `/opt/ministar`, restart.
  Worth having for a unit already in the field; not worth building before v1.

Never install app updates from the open internet while the only network is the AP.

---

## 8. Workstream F — app changes for headless/field

- **Session persistence** (already pending) becomes a prerequisite: restarts, reflashes and updates
  all assume the wizard can resume.
- **Device page:** network mode + scanner, field-mode toggle, time sync, storage, power, logs
  download, version, update.
- **Red-only theme and large touch targets** (14 §6) are a requirement, not a polish item.
- **Status bar:** network mode, AP SSID, disk free, undervoltage/throttle, camera temperature, solve
  age.
- **Failure UX:** camera absent → one clear screen with a retry and the last error; no traceback.
- **Geolocation:** rework `GEOLOCATE_JS` / `find_me` for the secure-context case, accuracy and
  provenance; keep manual entry authoritative. Add a Wake Lock toggle (HTTPS only) so the phone
  does not dim mid-session.
- **Export:** stream a session zip; show its size before starting.

---

## 9. Privileged operations

The app is unprivileged. Put root actions behind a small root-owned helper on
`/run/ministar/helper.sock` (`0660 root:ministar`) with an allow-list:

`wifi.scan`, `wifi.join`, `wifi.ap`, `time.set`, `power.reboot`, `power.off`,
`service.restart`, `update.apply`.

- Validate each argument; never interpolate into a shell. `nmcli` and `timedatectl` take argv.
- Returns structured JSON; the UI never blocks on a password prompt.
- `sudoers` NOPASSWD for specific commands is simpler but harder to validate (arguments are the
  attack surface). The helper is ~150 lines and testable.
- polkit adds D-Bus and interactive auth that this device does not have.

---

## 10. Testing and CI

- **Unit:** the existing suite runs on Linux; add a fake `gphoto2` module, driver state machine,
  golden nmcli profiles, firstboot idempotence, helper allow-list, portal path allow-list.
- **CI:** `ubuntu-24.04-arm` runners for the venv and image build. `rpi-image-gen` needs
  `CAP_SYS_ADMIN`/namespaces; if the hosted runner refuses, use a self-hosted Pi 5. Cache apt and
  wheels.
- **Image smoke test:** boot under `qemu-system-aarch64`, wait for HTTP 200 on `/`, assert
  `hostnamectl` and the avahi unit. 5–10 min, and it catches unit/ordering mistakes before a flash.
- **Hardware-in-the-loop checklist** (real Pi + 60D): boot time, AP join from iOS and Android, mDNS
  from each OS, capture latency, live-view fps, focus sweep, solve time, 2K stack time, 30-minute
  thermal run, brownout at shutter, unplug/replug camera, WiFi loss, full disk, power cut, wrong
  clock.
- **TLS/phone matrix:** iOS Safari and Android Chrome, both warning-bypass and CA-installed; CA
  install flows on current OS versions; captive sheet with the HTTP landing page; `wss` under TLS;
  leaf renewal after a clock reset; fingerprint display matches the installed CA.
- **Fault injection:** corrupt session file, missing SSD, camera busy from a killed process,
  duplicate hostname.

---

## 11. Effort

| Workstream | Days |
|---|---|
| A. gphoto2 backend + driver tests | 5–8 |
| B. Platform seams (config/preflight/CLI/backend selection) | 4–6 |
| C. systemd, storage, power, clock | 3–5 |
| D. NetworkManager client/AP, fallback, mDNS, portal | 5–8 |
| E. Image build, installer, firstboot, release pipeline | 5–8 |
| F. Device UI + session persistence | 5–8 |
| G. Privileged helper + hardening | 2–3 |
| H. CI, image smoke, HIL matrix | 4–6 |
| I. Docs, labels, field QA | 3–5 |
| J. TLS, certificate generation, landing server, geolocation | 3–5 |
| **Total** | **39–62** |

- **MVP (12–17 d):** backend + seams + systemd on stock Lite + AP fallback + avahi + `install.sh`
  + self-signed TLS and a working location button. No custom image, no portal, no device page.
  Enough for the user's own Pi this month.
- **v1 (the rest):** prebuilt image, firstboot, portal, helper, device UI, CI/HIL.
- Excludes: 14 §4 performance work (2K stack, solve cadence), the pending CALIBRATE/flats work, and
  real-frame stack quality review. Those are prerequisites for a *good* portable product but not
  for the port itself.

---

## 12. Risks, ranked

| # | Risk | Retire by |
|---|---|---|
| 1 | Live view over gphoto2 is too slow/laggy on Pi; focus UX collapses | M0 spike: measure fps and a sweep on Pi 4/5 + 60D. If < ~5 fps, investigate preview size/format or a focus routine that does not need live view. |
| 2 | 60D does not expose `manualfocusdrive` over PTP, or direction differs | Same spike. Fallbacks: other `eos*` focus actions, or declare the 60D unsupported for autofocus on Linux. |
| 3 | No usable clock without internet (Pi 4) | RTC battery on Pi 5; DS3231 or browser sync on Pi 4; measure drift over a night. |
| 4 | Brownout at shutter / Pi 5 PD negotiation | Bench test on battery, separate rails, bulk capacitance; surface `get_throttled`. |
| 5 | SD corruption on power loss | journald volatile, noatime, config/sessions on SSD, optional overlayroot, clean shutdown. |
| 6 | Image build drifts / is expensive to maintain | Pin versions, CI on arm64, cache downloads, rebuild monthly. |
| 7 | Android does not resolve `.local` | IP + captive portal fallback; test on real devices, do not promise mDNS-only. |
| 8 | AP/mDNS/dnsmasq interaction | 30-minute network matrix on the bench before image work. |
| 9 | Python 3.13 ABI/wheel drift | Wheels verified today; keep apt `python3-gphoto2` + system-site-packages as the fallback. |
| 10 | Licence surprise | libgphoto2 LGPL-2.1 via dynamic ctypes wheel (fine); gphoto2 CLI/dcraw/ASTAP are subprocesses. Do not vendor camlibs into our tree; depend on the wheel. |
| 11 | Certificate trust UX: the self-signed warning recurs (browser-dependent); CA install flows differ by OS and version | Test on current iOS/Android; per-OS steps on the HTTP landing page; keep manual coordinates as the fallback; never gate a session on geolocation. |
| 12 | CA name constraints rejected or ignored; the CA key on the device is a trust risk | Test constraints on both platforms; if rejected, ship self-signed-only mode; per-device CA, 0700 key, fingerprint and rotate in the UI. |
| 13 | The captive sheet cannot get past a certificate warning | Probe paths stay HTTP; the landing page carries the instructions; never redirect probes to HTTPS. |

---

## 13. Decisions needed

1. **Primary board:** Pi 5 4 GB (recommended) or Pi 4 2 GB as the baseline.
2. **Headline artifact:** image (recommended) or script first. Both share one core either way.
3. **Hostname:** fixed `ministar.local` (recommended) or serial-suffixed.
4. **AP security:** WPA2 + fixed PSK `ministar` (decided for appliance ease of use), with a
   `ministar-<mac4>` SSID; open + captive portal was rejected.
5. **Port:** 80 with `CAP_NET_BIND_SERVICE` (recommended) or 8080.
6. **Field mode:** explicit toggle (recommended) or always wait 20 s for a known network.
7. **Frames filesystem:** ext4 (recommended) or exFAT for Windows readability.
8. **Updates:** reflash (recommended) or USB update package in v1.
9. **Device UI scope for v1:** network + power + storage only, or the full settings page.
10. **Pi 5-only features** (RTC, power button) vs Pi 4 parity. Recommend: Pi 5 features optional,
    Pi 4 must work.
11. **Certificate strategy:** self-signed leaf only (fastest, warning) or per-device CA + leaf
    (recommended; one install removes the warning).
12. **Console scheme:** HTTPS-first console (recommended; enables location, wake lock and PWA) or
    HTTP console with an HTTPS-only `/locate` side page (less friction, more moving parts).
13. **CA install:** optional and prompted (recommended) or required before first use. Recommend
    optional: the session must work without it.

---

## 14. Milestones

- **M0 — hardware spike (3–5 d).** Pi 4/5 + 60D: gphoto2 capture, download, live-view fps, focus
  drive, ASTAP solve, 2K stack. **Go/no-go for the rest.**
- **M1 — backend + seams (2–3 wk).** `camera/gphoto2.py`, selection, preflight, CLI, fake parity.
- **M2 — appliance (2 wk).** systemd, client/AP profiles, fallback, mDNS, portal, helper, firstboot.
- **M3 — image + installer (2 wk).** `rpi-image-gen`, CI, release artifacts, labels, docs.
- **M4 — hardening (2 wk).** power/thermal/clock, fault injection, HIL matrix, a real dark-site run.

Roughly 8–10 calendar weeks for one engineer, with M0 deciding the other three.

---

## Appendix A — repository additions

```
deploy/
  provision.sh            the shared core (apt, venv, units, profiles, ASTAP, SSD)
  install.sh              curl|bash wrapper for stock RPi OS Lite
  firstboot.sh
  ministar-wifi-fallback.sh
  helper/                 the root socket helper
image/
  ministar.yaml           rpi-image-gen config
  layers/                 custom layer (files, hooks, packages)
systemd/
  ministar.service
  ministar-firstboot.service
  ministar-wifi-fallback.service
  ministar-health.timer
network/
  ministar-ap.nmconnection
  dnsmasq-shared.conf
  avahi/ministar.service
  udev/90-ministar-camera.rules
tls/
  generate.sh             per-device CA + leaf, SANs, renewal
  ministar.mobileconfig   iOS profile template (CA embedded at first boot)
  landing.py              the stateless HTTP :80 landing/portal server
docs/
  install-pi.md           the user-facing quickstart
```

## Appendix B — commands that are now verified, not guessed

```bash
# Base OS (2026-10-06 image) already has:
#   avahi-daemon 0.8, network-manager 1.52, dnsmasq-base, python3 3.13.5, raspi-config

apt install -y python3-venv libgphoto2-6t64 dcraw avahi-utils

# ASTAP: single aarch64 binary, no GTK
#   astap_command-line_version_Linux_aarch64.zip  -> astap_cli (940 KB)
#   star databases: G05 101 MB, D20 393 MB

# Python deps in a venv (all aarch64 wheels):
#   gphoto2 2.6.4 (vendors libgphoto2 + camlibs), numpy, opencv-python-headless, nicegui

# Boot firmware:
#   /boot/firmware/cmdline.txt += usbcore.usbfs_memory_mb=256

# Hostname + mDNS:
hostnamectl set-hostname ministar
systemctl enable --now avahi-daemon

# AP:
nmcli con add type wifi ifname wlan0 con-name ministar-ap \
  ssid "ministar-$(cat /sys/class/net/wlan0/address | tr -d ':' | tail -c 5)" \
  autoconnect no 802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk ministar

# TLS (per device, at first boot): the CA is optional to install; without it the leaf is
# untrusted and the browser warns, which is the acceptable default.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout ca.key -out ca.crt -subj "/CN=ministar local CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

openssl req -newkey rsa:2048 -nodes -keyout leaf.key -out leaf.csr \
  -subj "/CN=ministar.local" \
  -addext "subjectAltName=DNS:ministar.local,DNS:ministar,IP:10.42.0.1,IP:127.0.0.1"
openssl x509 -req -in leaf.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -copy_extensions copy -out leaf.crt

# NiceGUI serves TLS itself (verified in 3.18.0; kwargs go to uvicorn):
#   ui.run(host='0.0.0.0', port=443, show=False,
#          ssl_certfile='/etc/ministar/tls/leaf.crt',
#          ssl_keyfile='/etc/ministar/tls/leaf.key')
```

## Appendix C — what not to do

- **Docker on the Pi.** It adds a daemon, USB passthrough problems and no benefit for a single-app
  appliance. systemd + a venv is simpler and starts faster.
- **Simultaneous AP+STA as a requirement.** One radio, driver-dependent, and the fallback is
  enough. Offer "join home" and switch, do not promise both.
- **Cloud dependency or remote login.** The device must work with no internet, forever.
- **Auto-update over the AP.** The only network is the one the user is standing on.
- **Running the app as root** to bind port 80 or touch `/dev/bus/usb`. Use capabilities and groups.
- **A fixed default AP password.** Generate per device and print it.
- **mDNS-only instructions.** Always print the AP IP as well.
- **A shared CA or private key in the image.** Every device would share it; one extraction
  compromises all phones that trusted it. Generate per device at first boot.
- **HSTS on a self-signed cert.** It removes the bypass and locks users out after a clock reset.
- **Redirecting captive-probe paths to HTTPS.** The restricted sheet handles the warning badly;
  serve plain HTTP there and link to the console.
- **Promising a boot time before measuring it.** Pi 5 Lite + NiceGUI should land under 40 s, but M0
  measures it; the camera must not gate boot.
