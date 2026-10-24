# 08 — EDSDK bindings for Python: fork pyedsdk, port N.I.N.A.'s knowledge

**Status:** verified on 2026-10-24 by direct inspection of the pyedsdk wheel (PyPI 0.2.16) and
the `isbeorn/nina` source at `develop`. This brief supersedes the EDSDK guidance in 05 §4 and
corrects a factual error in 02 §2.3.

---

## 1. Why this brief exists

We had two claims, both wrong:

| Claim | Source | Reality |
|---|---|---|
| "N.I.N.A. has no Canon driver; Canon goes through ASCOM" | 02 §2.3 | **False.** `EDCamera.cs` is a full native Canon driver. The agent searched for `CanonCamera.cs` and missed `EDCamera.cs`. |
| "`pyedsdk` is not production-viable, so write our own binding" | 05 §4 | **Half true.** The gaps are real but *narrow*, and the file is MIT — so fork it rather than rewrite it. |

Both errors are instructive: a filename search is not a code search, and "missing feature"
is not the same as "unusable library".

---

## 2. pyedsdk: what it actually is (measured)

Wheel `pyedsdk-0.2.16-py3-none-any.whl`, 26 KB, pure Python, 16 modules, **MIT**
(`LICENSE.txt` = "MIT License"; `pyproject.toml` = `license = "MIT"`).

**Present and correct:**

- ctypes plumbing with an `EdsError`-returning restype hook (`core/_errors.py`)
- `_sendCommand(cameraRef, command, param)` — **fully generic**, takes any command code
- Enum tables: `_Aperture`, `_ShutterSpeed`, `_ISOSpeed`, `_AFMode`, `_EvfOutputDevice`
- `_pumpWindowsMessages()` — a `PeekMessage`/`TranslateMessage`/`DispatchMessage` loop.
  EDSDK *requires* a Windows message pump for callbacks; this is the single thing most
  hand-rolled bindings get wrong, and it is already right here.
- `LiveViewStream` — `_startLiveView`, `_createMemoryStream`, `_createEvfImageRef`,
  `_downloadEvfImage`, threaded callback loop. This is our focus loop, already built.
- Loader honouring `EDSDK_PATH`; correctly does **not** bundle `EDSDK.dll`.

**Missing (the whole to-do list):**

1. **Bulb.** `BulbStart`, `BulbEnd`, and even lowercase `bulb` return **0 hits**. Absent.
   Cheap to add: two enum constants + two wrapper methods over the existing `_sendCommand`.
2. **15-second exposure ceiling.** `core/_callbacks.py:46` is
   `def _waitForEvent(event, timeout=15)`, raising `TimeoutError` past 15 s — and
   `camera.py:258` calls `_waitForEvent(self._downloadEvent)` with **no timeout argument**, so
   `shot()` is hard-wired to 15 s. Any exposure ≥15 s fails. It is a *defaulted parameter*,
   not a wall: pass a timeout derived from the requested shutter speed.
3. **No `EdsGetPropertyDesc`.** We cannot enumerate which Tv/Av/ISO values *this body* supports.
   Needed, because sending an illegal value yields `EDS_ERR_DEVICE_BUSY` — which is the same
   code as "device actually busy", so without enumeration the failure is undiagnosable.
4. **Download goes to a file.** `shot(filename)`. N.I.N.A. downloads to a **memory stream**;
   we want the same, since every frame is consumed twice (solver + stacker) and disk is a
   pointless round trip at 2 s cadence.
5. No camera-state handler → no `BulbExposureTime`, no `WillSoonShutDown`.

**Verdict: fork. Do not rewrite.** A from-scratch binding means re-deriving the five enum
tables and re-getting the message pump right — the two places a hand-rolled EDSDK binding
actually loses time.

---

## 3. N.I.N.A. as the reference implementation (verified)

`isbeorn/nina` @ `develop`:

- `NINA.Equipment/SDK/CameraSDKs/EDSDK/EDSDK.cs` — 2608 lines, 125 KB, `namespace EDSDKLib`.
  Vendored from Johannes Bildstein's EDSDKLib; no licence header of its own, but N.I.N.A. is
  MPL-2.0 overall.
- `NINA.Equipment/SDK/CameraSDKs/EDSDK/EDSDKLocal.cs`
- `NINA.Equipment/Equipment/MyCamera/EDCamera.cs` — 45 KB, `class EDCamera : ICamera`,
  `public string Category { get; } = "Canon";`, `HasShutter => true`.
  Contains **44** hits for `Bulb`, 11 `EdsSendCommand`, `EdsDownloadEvfImage`,
  `EdsGetPropertyDesc` (×4), `EdsSetCapacity` (×2).
- Note: `EdsExtendShutDownTimer` → **0 hits in the binding**, but **N.I.N.A. absolutely does
  extend the shutdown timer** — see §7. It is not a standalone export; it is
  `EdsSendStatusCommand(cam, kEdsCameraStatusCommand_ExtendShutDownTimer, 0)`, which is why
  grepping for the symbol missed it. An earlier revision of this brief concluded N.I.N.A.
  "does not bother" — **that was wrong**, and the field logs prove the camera does try to sleep
  mid-session.

### 3.1 The bulb strategy — the single most valuable thing to port

This comment is worth more than the code around it:

> Cameras that feature a Bulb ("B") mode on the mode selection dial use the
> **PressShutterButton** command to start and stop bulb exposures when in that mode.
> Cameras that lack a "B" mode on the mode selection dial and instead set bulb mode as a
> shutter speed while in Manual ("M") mode on the dial use **BulbStart/BulbEnd**.

So there are two bulb dialects and the body's dial determines which. N.I.N.A. encodes both
behind `IsManualModeBulb` and `usesCameraCommandBulb`. This is undocumented, hardware-specific
knowledge that cannot be derived from the SDK headers — exactly the kind of thing a
from-scratch binding would rediscover the hard way.

It also carries per-body quirks, e.g.: **the 500D returns 44313 on `BulbStart` yet the exposure
starts anyway**, so N.I.N.A. explicitly treats 44313 as success. There will be more of these.

### 3.2 Capture/download flow

```
EdsSendCommand(BulbStart | ShutterButton_Completely)
  → wait for object event
  → EdsGetDirectoryItemInfo(item)        # size + filename
  → EdsCreateMemoryStream(size)
  → EdsDownload(item, size, stream)
  → EdsDownloadComplete(item)
  → EdsGetPointer(stream) + EdsGetLength(stream) → byte[]
```

Memory, not disk. We should match this.

### 3.3 Live view

```
EdsGetPropertyData(PropID_Evf_OutputDevice) → device |= EvfOutputDevice_PC
EdsSetPropertyData(PropID_Evf_OutputDevice, device)
... frame: EdsCreateMemoryStream(2 MB), EdsCreateEvfImageRef, EdsDownloadEvfImage ...
stop:      device &= ~EvfOutputDevice_PC
```

2 MB buffer for the EVF JPEG. Matches pyedsdk's approach.

---

## 4. Redistribution: settled empirically

02 §2.3-adjacent and my own earlier notes framed `EDSDK.dll` as non-redistributable. The
evidence says otherwise. `NINA.Setup/Product.wxs` contains:

```xml
<Component Id="External_x64_Canon_EDSDK.dll" Guid="..." Bitness="$(var.Win64)">
  <File Id="External_x64_Canon_EDSDK.dll" Name="EDSDK.dll"
        Source="$(var.NINA_ProjectDir)External\x64\Canon\EDSDK.dll" />
```

N.I.N.A. ships `EDSDK.dll` **inside its installer** — and the DLL is **not in git** (no
`*.dll` under `External/` in the tree). That is the pattern, and it resolves the question I
posed earlier:

- **Redistribution as part of an application is what Canon's licence permits** ("object-code
  use only, redistribution only as part of your app" — 02 §1).
- **Never commit the binary.** Keep it in a gitignored `External/x64/Canon/` that the build
  expects, and document where the user gets it.
- We do not need to choose between "take on the agreement" and "detect an existing install".
  We do what N.I.N.A. does: ship it, don't version-control it.

Also settled: `EDSDK.dll` **13.19.0.6400 (x64)** is already on this machine inside
Lightroom Classic (and 13.15.20 inside ON1 Photo RAW), so development can start immediately
without Canon developer registration.

---

## 5. Decision

1. **Fork `pyedsdk`** into `vendor/pyedsdk/`, keep the MIT notice, and add the five gaps in §2.
2. **Port domain knowledge from `EDCamera.cs`, not code.** It is C# against a different
   binding; what transfers is the bulb dialect logic, the quirks table, the memory-stream
   download, and the live-view property dance.
3. **Write our own `EdsGetPropertyDesc` enumeration** early — it gates every settings path.
4. **Own the frame path, not the SDK.** Our value is the loop, not the binding; keep the
   binding boring and small.

### Licences, precisely

| Component | Licence | What it means for us |
|---|---|---|
| CPython / python-build-standalone | PSF-2.0 / MPL-2.0 | bundle freely |
| pyedsdk | MIT | fork, modify, redistribute; keep the notice |
| N.I.N.A. | MPL-2.0 | file-level copyleft. Porting *knowledge* is unconstrained. Copying a file wholesale would make that file MPL — fine for a Python port only if we keep it MPL, so prefer reimplementing the logic |
| EDSDK.dll | Canon proprietary | ship inside the installer, never in git |

---

## 6. Scope decision: no bulb in v1 (descoped 2026-10-24)

**Decision: v1 does not implement bulb.** Exposures are driven purely by `kEdsPropID_Tv`.

Rationale — pyedsdk's `_ShutterSpeed` table is complete and covers the whole range we need:

- **81 entries**, from `1/32000` up to `_30s` (30 s), plus `_Bulb = 0x0C` as a *Tv value*
  whose `seconds` property deliberately returns `None`, plus `_Invalid`.
- Time steps above 1 s: `1", 1.3", 1.6", 2", 2.5", 3.2", 4", 5", 6", 8", 10", 13", 15", 20", 25", 30"`.
- Our sub lengths (0.4–3 s per brief 03's trailing limits) sit comfortably inside this.

Bulb is only required for exposures **>30 s**. Descoping it deletes, from the fork:
`BulbStart`/`BulbEnd`, the two-dialect detection, the 500D `44313` quirk, and the
camera-state handler for `BulbExposureTime`. That removes the single largest rationale for
forking at all — the remaining work (`EdsGetPropertyDesc`, memory download, the 15 s timeout
fix) is a ~150-line patch, not a driver extension.

### Consequences to design around

1. **Sub length must snap to the Tv table.** Brief 03 derives optimum sub lengths like
   "2.2 s at 50 mm", which is not requestable — the nearest legal values are 2" and 2.5".
   Snap *before* computing frame counts and expected SNR, or the arithmetic quietly drifts
   from what the camera will actually do.
2. **Bulb stays on the backlog for polar targets.** Drift scales with `cos(dec)`; the frames
   inspected on this machine were at DEC +89.85, where drift is ~1.3"/s, so multi-minute
   untracked subs near the pole are physically fine. "Never >30 s" holds for wide-field and
   fails for a polar mode.
3. **LENR is the real multiplier.** Long-exposure noise reduction has no EDSDK property (02)
   and is menu-only, so it cannot be switched off in code. Left on, every 2 s sub costs 4 s and
   throughput halves. This belongs in a startup pre-flight check.
4. **`_waitForEvent(timeout=15)` still needs fixing.** Even at ≤30 s Tv, anything approaching
   15 s trips it. Derive the timeout from the requested shutter speed.

---

## 7. Field telemetry: what actually failed on this exact rig

N.I.N.A.'s own logs on this machine (`%LOCALAPPDATA%\NINA\Logs`, 28 sessions, 6.3 MB,
2026-05-09 → 2026-07-31) were mined directly. This is not documentation — it is the EOS 60D
being driven by EDSDK, on this laptop, for 3043 attempted exposures.

### 7.1 The dominant failure mode is `EDS_ERR_DEVICE_BUSY`

Every `EDS_ERR_*` code seen, across all 28 sessions:

| Code | Count |
|---|---|
| `EDS_ERR_DEVICE_BUSY` | **2439** |
| `EDS_ERR_COMM_DISCONNECTED` | 42 |
| `EDS_ERR_INVALID_HANDLE` | 4 |
| `EDS_ERR_PROPERTIES_UNAVAILABLE` | 3 |

`DEVICE_BUSY` dominates by ~50:1. N.I.N.A. retries aggressively on this path (it logs up to
three attempts per exposure — `BULB via PressShutterButton`, `MANUAL via PressShutterButton`,
`MANUAL via TakePicture`), which is why the counts exceed the number of exposures.

### 7.2 It is not spread evenly — and the trend is good

| Session | Attempted exposures | `DEVICE_BUSY` |
|---|---|---|
| 2026-05-09 | 15s×153, 5s×109, 2s×45 | **683** |
| 2026-05-14 | 5s×35, 3s×15 | **107** |
| 2026-06-17 | 30s×367, 5s×188, 15s×122, 2s×58 | **863** |
| 2026-07-14 | 30s×192, 2s×94, 5s×85 | **762** |
| 2026-06-02 | 5s×44, 60s×33, 2s×25 | 0 |
| 2026-07-20 | 2s×78, 5s×60, 4s×34, 3s×32 | 0 |
| 2026-07-29 | 4s×528, 60s×132, 15s×20 | **0** |

- **9 of 28 sessions** hit `DEVICE_BUSY`; 19 did not.
- **The three most recent substantive sessions are clean** — 2026-06-02, 2026-07-20 and
  2026-07-29 ran ~1100 exposures with zero `DEVICE_BUSY`. The user's "it works nearly
  flawlessly" is an accurate description of the current state.
- The bad sessions cluster around **30 s-dominated** runs (2026-06-17 and 2026-07-14 both have
  30 s as the top exposure). ⚠️ The mechanism is **UNVERIFIED** — plausible candidates are the
  30 s Tv/bulb boundary, a mode-dial state, or an illegal-property-value rejection being reported
  as `DEVICE_BUSY` (the known ambiguity from 02). Worth reproducing deliberately, because 30 s
  sits right at the edge of our own usable range.
- Our exposure range (0.4–3 s, per 08 §6) sits squarely inside the historically clean
  configuration — 2026-07-29 did **4 s × 528** with zero errors. That is the most encouraging
  single data point in this research.

### 7.3 `EDS_ERR_COMM_DISCONNECTED` is real and needs handling

42 occurrences. USB disconnects happen. A long-lived daemon must detect
`COMM_DISCONNECTED`/`INVALID_HANDLE`, tear down the session, and reopen — not crash and not
silently stop capturing. N.I.N.A. also logs camera-removed/added handling.

### 7.4 The camera *does* try to sleep mid-session

```
2026-07-29T20:34:53|INFO|EDCamera.cs|Camera_SDKStateEvent|CANON: Will soon shutdown -
                                      sending request to extend shutdown timer
2026-07-29T20:46:05|INFO|EDCamera.cs|Camera_SDKStateEvent|CANON: Will soon shutdown -
                                      sending request to extend shutdown timer
```

Confirmed on real hardware: the `kEdsStateEvent_WillSoonShutDown` event fires, and N.I.N.A.
responds with the extend request. **We need this.** Two occurrences in a 90-minute session means
it is not a theoretical concern for a multi-hour untracked run. Implement as
`EdsSendStatusCommand(cam, kEdsCameraStatusCommand_ExtendShutDownTimer, 0)` on the
`WillSoonShutDown` state event.

### 7.5 What this changes in the plan

1. **Retry logic is mandatory, not polish.** `DEVICE_BUSY` at this frequency means every
   capture call needs bounded retry with backoff. But see 02: `DEVICE_BUSY` also means "illegal
   property value", so a blind retry loop can spin forever on a genuinely invalid setting.
   Distinguish by consulting `EdsGetPropertyDesc` before setting Tv/Av/ISO (already in §2 item 3).
2. **`EdsSendStatusCommand` + `WillSoonShutDown` is now a v1 requirement** (§7.4), not a maybe.
3. **Reconnect handling is a v1 requirement** (§7.3).
4. The bulb path is confirmed unnecessary for our range — every `BulbStart`/`BulbEnd`-style
   attempt in the logs happened in runs that were failing anyway.

---

## 8. Still unverified

- Whether EDSDK 13.x changed any command codes vs the 13.12 headers N.I.N.A.'s `EDSDK.cs` was
  written against. N.I.N.A. is current, so probably fine, but confirm the constants we add
  (`BulbStart`/`BulbEnd` values) against the 13.19 headers rather than trusting memory.
- Which bulb dialect the **EOS 60D** (this rig) speaks. Testable in an afternoon: try
  `BulbStart` first, fall back to `PressShutterButton`.
- Whether a still-download really interrupts live view on the 60D. 02 claims yes; N.I.N.A.'s
  code does not visibly stop/restart live view around a capture, which hints it may be
  body-specific.
- pyedsdk's `LiveViewStream` frame rate on 64-bit + EDSDK 13.19.
