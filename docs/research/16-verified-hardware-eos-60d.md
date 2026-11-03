# 16 — Verified hardware: Canon EOS 60D over EDSDK

**Status:** hardware-verified 2026-10-25 against the real body. This is the first
document in the set whose numbers came from a camera rather than from a datasheet, a
NINA source read, or an inference. Everything below was observed, not recalled.

**Scope:** the **focus path only** — enumerate, session, live view, lens drive. Capture,
Tv/ISO setting, download and mirror lockup are still unexercised (§11).

---

## 0. Summary

| Question | Answer |
|---|---|
| Can we talk to the camera from Python? | **Yes** — ctypes `WinDLL` + EDSDK, no wrapper library needed |
| Does the lens drive work? | **Yes, including on an STM lens** — this was the biggest open unknown (§3) |
| Is live view available to the PC? | **Yes** — 1056×704 JPEG, ~110–275 KB/frame |
| What does `0xA102` mean? | **"EVF not ready"**, not a failure. Retry for ~0.85 s (§4) |
| Is HFD trustworthy on a real frame? | **No, not yet** — it fabricates stars on near-uniform frames (§7) |

---

## 1. Environment that worked

| Item | Value |
|---|---|
| OS | Windows 10.0.26200 |
| Python | 3.13.15 in `C:\ministar\build\.venv` |
| Extra package needed | `pillow` (JPEG decode). numpy already present |
| DLL actually used | `C:\Program Files\N.I.N.A. ...\External\x64\Canon\EDSDK.dll` |
| Other working copies | Adobe Lightroom Classic, ON1 Photo RAW 2026 |

⚠️ **Prefer NINA's copy.** It ships the SDK's own dependency set alongside the DLL, so it
loads without `add_dll_directory` gymnastics finding sibling DLLs. The others loaded too,
but they are incidental bundles of a third-party product.

Note the DLL is Canon-proprietary and must never be committed (08 §4). Loading it from an
installed application is fine for development and matches the "borrowed EDSDK" path
already in `config.find_borrowed_edsdk`.

---

## 2. Device facts

```
EdsInitializeSDK                0x00000000 OK
EdsGetCameraList                0x00000000
EdsGetChildCount                cameras=1
EdsGetDeviceInfo
    szPortName       \\?\usb#vid_04a9&pid_3215#6&f94f717&0&1#{6ac27878-...}
    szDeviceDescription  "Canon EOS 60D"
    deviceSubType    0x00000001
EdsOpenSession                  0x00000000 OK
kEdsPropID_ProductName          "Canon EOS 60D"
```

The `EdsDeviceInfo` field order (port name, description, subtype, reserved) — which brief 08
listed as unverified — is **confirmed correct**; the raw byte dump showed the port path at
offset 0 and the description at offset 256.

Session opened on the first attempt with no retry, pacing or "wait for device" needed.

---

## 3. Lens drive works — including on STM glass

**This is the headline.** The attached lens identifies as:

```
kEdsPropID_LensName = "EF-S24mm f/2.8 STM"
```

> ⚠️ The operator referred to this as "the 28 mm". The SDK reports **24 mm**. Worth
> confirming before any focal-length arithmetic is trusted, since 24 vs 28 changes the
> field width by ~17%.

`EdsSendCommand(kEdsCameraCommand_DriveLensEvf, kEdsEvfDriveLens_Near3)` **moved the
lens**, confirmed by eye and by the image changing between frames.

What makes this important: **STM lenses are focus-by-wire**, and every assumption in brief
08 and brief 11 about the lens drive came from NINA's `EDCamera.cs`, which was written
against ring-type USM glass. A stepper motor behind a focus-by-wire controller is a
different mechanism, and there was a real possibility the 60D would refuse the manual
focus command on an STM lens. It does not.

Reliability: the user reported **"focus by wire works perfectly"** during interactive use
with the harness (§8), having driven it through repeated presses.

---

## 4. `0xA102` is not an error — it is EVF warm-up

The first live-view attempt failed:

```
EdsDownloadEvfImage -> 0x0000A102
```

Treating that as fatal was wrong. A sequence probe showed the *same* enable sequence
succeeding on **attempt 17** at 50 ms spacing — i.e. the camera needs roughly **0.85 s**
after the output device is set before the first frame is available.

All of these produced frames, so the exact enable ritual is not critical:

| sequence | result |
|---|---|
| `Evf_Mode=1` then `Evf_OutputDevice=2` (PC) | OK (after 0.85 s) |
| `Evf_OutputDevice=2` alone | OK |
| `Evf_OutputDevice=3` (PC\|TFT) | OK |
| `Evf_OutputDevice=2` then sleep 1.5 s | OK |

⚠️ **Correction 2026-10-28: `Evf_Mode` is not safe to write, and is not needed.** Through
`src/ministar/camera/edsdk_ffi.py` — the binding the app actually uses —
`EdsSetPropertyData(kEdsPropID_Evf_Mode, 1)` **did not return** in two independent sessions
(the camera pump, and a standalone probe). No error, no `DEVICE_BUSY`: the thread stayed
inside the DLL. It also blocks `EdsCloseSession`, so the app could not even shut down, and
because the pump was the blocked thread the visible symptom was a permanently blank preview
with nothing to report.

The row above ("`Evf_Mode=1` then `Evf_OutputDevice=2` → OK") came from `tools/edsdk_lite.py`,
a separate targeted binding, on 2026-10-26. So the same write has been observed both
returning and never returning on this body: **the discrepancy is real and unexplained**
(an event pump — `EdsGetEvent` — runs in some harnesses and not others; body state and mode
dial may also matter). Re-measured today, output-route-only is sufficient and fast:

| sequence | result |
|---|---|
| `Evf_OutputDevice=2`, no mode write | `0x0` in **0.06 s** |
| `Evf_Mode=1` first, as before | **does not return** |
| download after output-only | `0xA102` ×10 (~1.0 s), then **180 059 bytes** |

Decision: **never write `Evf_Mode`.** It is the legacy switch, the output route alone starts
live view on this body, and a call that can hang without a code is not something a caller can
defend against. `PROP_EVF_MODE` remains in the FFI only as the name of the trap.

A second, related lesson: live view had never been exercised through the shipping binding at
all. The verification used a script beside it, so a defect in the app's own path could not
show up in that verification. Verify the code the product runs.

**Retryable codes** observed so far: `0x0000A102`, `0x000000A0`, and `0x00000081`
(`DEVICE_BUSY`) when pulses are sent faster than the body consumes them.

⚠️ The practical consequence for the real backend: **`EdsDownloadEvfImage` must never be
called in single-shot mode.** Any code that does `if code: raise` will report live view as
broken on a healthy camera. This is exactly the class of "silent wrong result" the README's
design rules exist to prevent.

---

## 5. Live view geometry

```
1056 x 704, JPEG, 110 640 / 111 449 / 111 496 bytes on consecutive frames
(275 565 bytes on a frame with more scene content)
```

So roughly **110–275 KB/frame**, and 1056×704 is ~3× smaller than the 24 MP still. That is
a useful confirmation for brief 15 §2: the live-view path is cheap, and a preview-resolution
stack has plenty of headroom.

At 1 fps this is ~1–2 Mbit/s, comfortably inside brief 14 §5's hotspot budget.

### 5.1 Live view as the app actually shows it (measured 2026-10-28)

Live view is a **focusing aid, not the imaging state**. Two consequences the app now enforces:
the frame on screen is stretched for the eye, and the body leaves live view whenever no screen is
showing a preview.

**Stretched, and cheaper for it.** Raw camera JPEG versus the STF-stretched preview the screens
display, same stationary scene, same session:

| | raw JPEG | stretched (STF, sky at 0.15) |
|---|---|---|
| frame size | 274 KB | **71 KB** |
| rate | 11.4 fps | **8.6 fps** |

So the stretch costs about a quarter of the frame rate (decode + `Stf` + re-encode: the decode
was already happening for HFD) and pays it back nearly 4× in bandwidth — the stretched frame is
almost all dark sky, which is cheap in JPEG. 8.6 fps is ample for focusing: the frame that
arrives is legible, where the raw one was a black rectangle with a few dots.

**Sky target 0.15, not the stack's 0.06.** The stack preview is watched for noise *falling* over
minutes, so its background sits at 15/255. The live view is watched for stars *existing at all*,
and at 0.06 the field is black. 0.15 puts the sky at 38/255 — still dark-adapted on the red theme
— and lifts the faint stars Canon's tone curve buries. Aggressive clipping of star cores is the
known trade: it makes stars easy to find and *less* informative about shape, which is why HFD is
measured on the raw frame and never on the display path.

**A session can be closed and reopened in one process.** Which is not something to assume:
some bodies refuse a second `EdsOpenSession` until the port is released by the OS. Measured on
the 60D, twice in a row within one process, while live view was running: `reconnect` returns in
**1.0 s / 0.9 s**, and the preview resumes at the same rate afterwards (15 frames in 2 s, 7.5 fps,
matching the pre-reconnect measurement). So the reconnect button is a real repair rather than a
polite suggestion to restart the console -- which is the difference between a night that continues
and one that ends.

### 5.2 The SDK is initialised per *process*, not per instance (measured 2026-10-28)

`EdsInitializeSDK` is global to the process, and calling it twice is not harmless: measured on the
60D, the second call returns **`0x00000002`** (`EDS_ERR_INTERNAL_ERROR`) and *every* later attempt
in that process fails the same way. The reconnect button reported exactly that -- "initialization
failed, 0x2" -- and could never recover without restarting the console.

Two holes reached it:

* **`connect()` leaked the initialisation when it failed.** It initialised the SDK, then
  discovered there was no body (`list_cameras` empty, i.e. the camera asleep or the port held by
  another process) and raised without terminating. The SDK stayed up; the next connect initialised
  over it and got `0x2` for the rest of the process's life. A camera that fell asleep mid-session
  was therefore enough to make every subsequent reconnect fail permanently.
* **Nothing tied the lifecycle to the process.** Each `Edsdk` instance owned its own
  initialisation, so any second instance -- a reconnect, a retry -- was a second initialise.

Fixed by reference-counting the initialisation (the SDK is up while any instance is, and the last
`close()` terminates it) and by terminating on every failure path in `connect()`.

Verified on the body, in one process: a connect that fails because another process holds the port
(`0xC0`), then a retry after that process exits, connects; and a full `Station.reconnect()`
afterwards returns True and live view resumes. Measured `0x2` before, same code path, same body.

**Leaving live view drops the mirror.** `Evf_OutputDevice = 0` ends the stream and a DSLR returns
its mirror; measured round trip, `want_live_view(False)` → **0 frames** while stopped, then
`want_live_view(True)` → 8.0 fps and frames again, in the same session. This matters beyond
tidiness: live view heats the sensor and drains the battery, and the shutter wants the mirror
down. The app therefore asks for it only on FOCUS and POINT, and during a reframe inside RUN;
every other phase stands the body down. The station performs the SDK call on the pump thread,
because that is the only thread that may (see `Station` and §4).

---

## 6. Constants and property IDs, now confirmed

Transcribed from Canon's `EdsTypes.h` and cross-checked against `Canon.Eos.Framework`'s C#
bindings, where they agree exactly:

| constant | value |
|---|---|
| `kEdsCameraCommand_DoEvfAf` | `0x00000102` |
| `kEdsCameraCommand_DriveLensEvf` | `0x00000103` |
| `kEdsCameraCommand_DoClickWBEvf` | `0x00000104` |
| `kEdsEvfDriveLens_Near1..3` | `0x0001`, `0x0002`, `0x0003` |
| `kEdsEvfDriveLens_Far1..3` | `0x8001`, `0x8002`, `0x8003` |
| `kEdsPropID_Evf_Mode` | `0x00000501` |
| `kEdsPropID_Evf_OutputDevice` | `0x00000500` |
| `kEdsEvfOutputDevice_PC` | `0x00000002` |

**Far is Near with bit `0x8000` set.** That maps onto `focus.driver.Direction` as
`param = |d| if near else 0x8000 | |d|`, which is a two-line adapter and confirms the
`Direction` enum was shaped correctly.

### 6.1 A property ID that cost a debugging round

`kEdsPropID_AvailableShots` is **`0x0000040A`**. `0x0000000D` is `CurrentFolder`, and
reading it as a UInt32 returns `0x64`. Do not infer property IDs from the ordering in an
old header.

---

## 7. ⚠️ HFD is not yet trustworthy: it fabricates stars

This is the most important *negative* result, and it affects product code rather than the
harness.

With the **lens cap on** — a near-uniform, low-noise frame — `measure_hfd` reported:

```
frame 0: tenengrad=0.27  hfd=6.257  stars=40
frame 1: tenengrad=0.27  hfd=3.750  stars=40
frame 2: tenengrad=0.28  hfd=7.810  stars=40
after drive: hfd=4.529 stars=40
```

Forty "stars", with a median HFD that swings between 1.3 and 7.8 px on consecutive frames
of an unchanging scene. That is noise, not stars. By contrast, an *un*-capped but dim frame
returned `hfd=nan, stars=0` — correctly rejecting.

**Mechanism:** `find_peaks` thresholds at `background + k·noise`, and `measure_star` accepts
anything above `min_snr × local_noise`. On a near-uniform frame both the global and local
noise estimates collapse toward zero, so ordinary read-noise excursions clear a "5 sigma"
gate that is now measuring almost nothing. The gate is *relative* with no absolute floor.

**Consequences:**
- Any autofocus sweep run on a dark frame, a capped lens, or a very clean sky will chase
  fabricated stars. Brief 11's whole V-curve fit is built on HFD.
- A relative-only gate makes the metric *less* reliable the better the conditions, which is
  backwards.

**Candidate fix** (not yet implemented): require an absolute minimum star flux and/or a
minimum frame contrast before accepting any peak, and reject the frame outright when the
estimated background noise falls below a plausible sensor floor.

---

## 8. Bench harness in `tools/`

Deliberately **not** product code. Brief 08 still governs the real backend.

| file | purpose |
|---|---|
| `tools/edsdk_lite.py` | targeted ctypes binding: init, enumerate, session, live view, drive, keep-alive |
| `tools/focus_ui.py` | Tkinter test UI; `--selftest`, `--diagnose`, `--stress` modes |
| `tools/probe_camera.py` | enumeration probe that dumps raw `EdsDeviceInfo` bytes |
| `tools/focus.bat` | CRLF launcher |

Two design points in the UI worth keeping when this becomes real UI:

1. **Never call EDSDK on the GUI thread.** The first version did, holding the camera lock
   while sleeping 60 ms per pulse; a burst of clicks froze the window and starved live view.
   The fix is a pulse deque drained a few per worker tick, so the preview keeps updating
   *during* a long sweep. Clicks now enqueue and return in microseconds.
2. **A scene-agnostic sharpness signal is necessary on a bench.** Tenengrad was added
   alongside HFD precisely because HFD cannot work without stars. Measured contrast between
   cap-on and cap-off was **0.27 vs 20.0** — two orders of magnitude, so scene contrast is
   also a usable gate for §7.

---

## 9. Practical focal-length ceiling: ~250 mm

Recorded at the user's direction: **any lens up to roughly 250 mm is workable**; the 24 mm
is a fine wide-field option.

This is a *confirmation*, not a new assumption. Brief 10 §2.1e gives `fl_max = 219 mm` at
dec +69° and a 1.5 s cadence, and 111 mm at dec 45°. So ~250 mm is already the optimistic
edge of the envelope rather than a comfortable operating point. Anything longer than that
fails on cadence, not on optics or on what the camera can do.

---

## 10. What this changes elsewhere

| Doc | Change |
|---|---|
| 08 (EDSDK plan) | focus path verified; the five gaps it lists are still real, but the *constants and sequences* are now known rather than inferred |
| 11 (autofocus) | HFD needs an absolute gate before any sweep is trusted (§7) |
| 15 (stacking) | live-view size confirms the preview path is cheap |
| 14 (SBC) | JPEG frames are far smaller than assumed, so upload is even less of a constraint |

---

## 11. Not yet verified — the next hardware session

1. **Capture**: Tv/ISO/Av *setting* and mirror lockup state. (`TakePicture` and the
   download itself are now verified — see §11.1.)
2. **`EdsGetPropertyDesc`** — legal Tv/Av/ISO values for this body (gap 1 in brief 08).
3. **`WillSoonShutDown`** handling over a long session (gap 4).
4. Whether `AvailableShots` reads sensibly with the correct ID.
5. The 100–400 mm lens, once fitted: does drive behave the same at that focal length, and
   does autofocus drive differ from the STM behaviour seen here?
6. Whether the 24 vs 28 mm discrepancy is a mislabel or a different lens.

### 11.1 Verified 2026-10-27 — the still-capture path

`ministar probe` (driven through `tools/sync.sh`, results in `rig-probe.json` on NTFS):

- **Capture + download works.** `EdsSetPropertyData(SaveTo=Host)` → `EdsSetCapacity` →
  `EdsSetObjectEventHandler(DirItemRequestTransfer)` → `SendCommand(TakePicture)` →
  `EdsGetEvent` pump → `EdsGetDirectoryItemInfo` → `EdsCreateFileStream` → `EdsDownload` →
  `EdsDownloadComplete`. A 20 100 177-byte CR2 landed on disk, 5184 x 3456.
- **Every bound symbol exists** in 13.19.0.6400 (N.I.N.A.'s bundled copy): `EdsSetCapacity`,
  `EdsSetObjectEventHandler`, `EdsGetEvent`, `EdsGetDirectoryItemInfo`,
  `EdsCreateFileStream`, `EdsDownload`, `EdsDownloadComplete`, `EdsDownloadCancel`.
- **Live view survived the still download**, twice: 112 738 and 113 454 bytes readable
  immediately after each capture, from two consecutive runs (two CR2s, 20 100 177 and
  20 101 128 bytes). The differing JPEG sizes rule out a stale buffer, so this is a real
  frame each time. It **contradicts 02 §1.8 ("assume yes", LV is interrupted)**: the
  "LV never comes back" failure mode did not occur on this body. Capture can therefore run
  without a stop/restart of the live-view stream — worth re-checking on the long end of the
  lens, where a 30 s download is a bigger window for it to bite.
- ⚠️ **CORRECTION (2026-10-27, later the same day): the 72 dpi was a bug in our reader, not
  a quirk of the body.** The CR2 carries two different tag groups:

  | tag | where | value | meaning |
  |---|---|---|---|
  | `0x011A`/`0x011B`/`0x0128` | IFD0 | `72/1` | `XResolution` — the TIFF **print** resolution |
  | `0xA20E`/`0xA20F`/`0xA210` | Exif IFD | `5184000/905`, `3456000/595`, unit 2 | `FocalPlaneXResolution`, i.e. **5728 / 5808 ppi** |

  The first version of `io/tiff.py` read `0x011A` and called it a sensor pitch, producing
  352.8 µm and a 1828 x 1219 mm sensor: an 82x error. The real tags give **4.434 µm**, a
  22.99 x 15.11 mm sensor, and a plate scale good to ~3% of the nominal 4.302 µm. So the
  60D's EXIF pitch **is** usable; the reader was reading the print DPI. The plausibility
  guard stays — a sentinel must never be multiplied by — but it is no longer the main
gate on this body.

  Cross-brand checks on real files from raw.pixls.us, same day:

  | body | container | `0xA20E` | print `0x011A` |
  |---|---|---|---|
  | Canon EOS 60D | TIFF (CR2) | **5728 ppi**, unit inches | 72 |
  | Fujifilm X-E5 | proprietary `FUJIFILMCCD-RAW` + EXIF blob at offset 154 | **1879/cm** (5.32 µm) | 72 |
  | Nikon 1 AW1 | TIFF (NEF) | **absent** | 300 |
  | Sony RX100M7A | TIFF (ARW) | **absent** | 350 |

  So the tags are standard across makers but the *population* is not: two of the four
  bodies omit the focal-plane resolution entirely and offer only the print DPI, which is
  exactly the trap. That is the real argument for plate scale as the root quantity — it
  is a solve away and needs no pitch, whereas the pitch is sometimes missing.
  ⚠️ And that fallback is not free: the installed ASTAP database is all `.1476` tiles (max
  **5.14°** field, per `preflight`), while this 24 mm frame is ~34° tall. A wide-field frame
  cannot be solved until the G05/W08 wide-field tiles are installed.
- Frame size is **5184 x 3456** (17.9 MP), not the 5202 x 3464 used in briefs 01/10's tables.
  Those numbers came from an earlier solved frame; the envelope maths is unaffected, but the
  megapixel tables are off by ~0.5%.
- The capture also confirms 02 §1.7's warning in the flesh: the body offers the file by
  *event*, so a host that never pumps `EdsGetEvent` would wait forever with no error.
