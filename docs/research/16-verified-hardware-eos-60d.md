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

1. **Capture**: Tv/ISO/Av setting, `TakePicture`, download to memory, mirror lockup state.
2. **`EdsGetPropertyDesc`** — legal Tv/Av/ISO values for this body (gap 1 in brief 08).
3. **`WillSoonShutDown`** handling over a long session (gap 4).
4. Whether `AvailableShots` reads sensibly with the correct ID.
5. The 100–400 mm lens, once fitted: does drive behave the same at that focal length, and
   does autofocus drive differ from the STM behaviour seen here?
6. Whether the 24 vs 28 mm discrepancy is a mislabel or a different lens.
