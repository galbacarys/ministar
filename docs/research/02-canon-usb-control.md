# Controlling a Canon DSLR/mirrorless over USB from a Windows host

**Research brief for `ministar`** — untracked alt-az/3-way-head astrophotography, many short
exposures, live view for focusing, ISO/Tv/Av + bulb + intervalometer loop. Developer drives a
Windows host from WSL2 (copy files to `/mnt/c`, run `.bat`).

*Compiled 2026-10-24. Everything below is verified against primary sources unless marked
**UNVERIFIED**. Source URLs at the end of each section.*

---

## 0. Bottom line up front

1. **Canon EDSDK is the only correct answer for the core capture path.** It is the only
   stack on Windows that gives simultaneous, well-defined control of ISO/Tv/Av, true bulb
   start/stop, live-view frames, and object-event driven image download from *one* process
   on *every* current EOS body (R1/R5II/R6III incl. Rebels). Everything else is either a
   wrapper around it, Linux-only, or a decade behind on body support.
2. **EDSDK cannot disable long-exposure noise reduction (LENR).** Verified: no such property
   exists anywhere in the 13.x header set; libgphoto2 exposes `longexpnr` for Nikon only.
   LENR must be switched off in the camera menu before the session (see §4.6).
3. **Recommended shape for `ministar`:** a small native Windows x64 *camera daemon* using
   EDSDK (C# or C++), exposing a localhost JSON/HTTP or named-pipe API, driven from WSL2.
   Live view is a *focusing* tool only (≈10–30 fps, ~1 MP JPEG), not a stacking source.
4. **Fallback / dev path:** `gphoto2` inside WSL2 with `usbipd-win` (see §2.2, §5). Fine for
   prototyping and for JPEG cadence; weaker live view, no EDSDK licensing questions, but
   native Windows libgphoto2 does not exist — Canon's own README says so.
5. The single biggest operational risk is **not** the API: it is sustained 1-frame-every-2 s
   reliability for hours (USB/MTP wedge, camera sleep, LV state loss after each capture,
   sensor heating, USB power). Budget for a watchdog + session re-open + resume, and soak
   test 30–60 min before trusting a session (§4.10).

---

## 1. Canon EDSDK (official, `EDSDK` / "ED-SDK")

### 1.1 Current version, acquisition, release cadence

| Item | Value |
|---|---|
| Current version | **v13.20.21**, release note dated `12/05/2026` (DD/MM/YYYY → 2026-05-12). Canon Japan's API-package news page carries the same version with a later publication date (2026-08-05) — treat the release-note date as authoritative. |
| Predecessors in 2026 | v13.20.11 (2026-03-02), v13.20.10 (2025-09-24) |
| Where | Canon **Developer Community** portals, login required, free, per-region: Americas `developercommunity.usa.canon.com`, EMEA `developers.canon-europe.com`, Asia `asia.canon/…/developerresources`. Registration → complete profile → *apply* for the SDK → download. Community reports ("may take a few days", per `pyedsdk` install note) imply a manual approval step. |
| Editions | "EDSDK" (control + JPEG) and "**EDSDK with RAW File Processing Support**" (adds `EdsImage.dll` — RAW decode / `EdsCreateImageRef`). For `ministar` you need the plain control SDK plus optional `EdsImage` if you want thumbnails/RAW. |
| Platforms | Windows (x86 + x64), macOS, and **Linux since v13.18.30 (Intel 64-bit, 2024-07-26)**; ARMv8 Linux since 13.17.10. Linux builds are directly relevant to a WSL2-only workflow (§5). |
| OS floor | Windows 7 support dropped at 13.13.20, Windows 8.1 at 13.16.20 → Windows 10/11 only. |

Release-note items that matter to us (verified):
* 13.20.10 added `kEdsPropID_Evf_FocalLength`.
* 13.19.0 added `EdsGetPropertyDescEx`, `EdsCreateFolder` (2025-02-28).
* 13.18.30 added `kEdsPropID_ApertureLockSetting`, `kEdsPropID_LensIsSetting`,
  **`kEdsPropID_ScreenOffTime`, `kEdsPropID_ViewfinderOffTime`**, `kEdsPropID_ScreenDimmerTime`.
* 13.17.10 added `kEdsCameraCommand_SetModeDialDisable` (nice: stops the user's mode dial
  from silently invalidating your session).

### 1.2 License / redistribution terms

From Canon's SDK distribution agreement (the public v2.14 PDF is the only full text available
without login; the v13 agreement is structurally the same — **UNVERIFIED** word-for-word):

* Grant: *"non-exclusive, non-transferable, non-assignment and revocable license to use the SDK
  in **object code format** … solely for the purpose of developing the Developer Software and to
  distribute the SDK in object code format **only as part of the Developer Software** to
  end-users"*.
* Restrictions: *"The Developer shall not use, reproduce, sublicense, distribute to any third
  party or dispose of the SDK, in whole or in part, other than as permitted under this Agreement"*;
  no reverse engineering/disassembly; must not remove copyright notices; must not use Canon
  trademarks/logos in marketing; no support obligation from Canon; no warranty; Developer
  indemnifies Canon for distribution-related claims.
* Practical consequences:
  * **Do not vendor `EDSDK.dll` / headers into a public git repo** (Canon explicitly told the
    `pyedsdk` author not to redistribute; that package deliberately ships without the DLL).
    Pattern used by the ecosystem: fetch-at-build or "drop your own copy in `C:\Canon\EDSDK\`".
  * Shipping the DLL **inside your installer** alongside your app is the intended model, i.e.
    release artifacts may contain it, source repos should not.
  * No stated royalty. Header/sample code is likewise Canon-proprietary; treat as
    "all rights reserved, use as part of the app".

Sources: <https://asia.canon/en/campaign/developerresources/camera/cap/edsdk-eos-digital-camera-sdk-release-note>
· <https://developercommunity.usa.canon.com/s/> · <https://developers.canon-europe.com/developers/s/article/How-to-get-access-camera>
· <https://id.canon/media/migration/local/id/live/developer_resources/EDSDK-v2.14-Developer-Agreement.pdf>
· <https://github.com/fmargall/pyedsdk>

### 1.3 Windows package layout (verified against a mirrored v13.12.31 tree)

```
EDSDK/Header/EDSDK.h            # ~60 exported functions, doxygen-style docs inline
EDSDK/Header/EDSDKTypes.h       # all property IDs / command IDs / enums / structs
EDSDK/Header/EDSDKErrors.h      # EDS_ERR_* codes
EDSDK/Dll/EDSDK.dll             # 32-bit
EDSDK/Dll/EdsImage.dll          # 32-bit, RAW/decode (RAW-support edition)
EDSDK/Library/EDSDK.lib         # 32-bit import lib
EDSDK_64/Dll/EDSDK.dll          # 64-bit   <-- note sibling tree, not a subfolder of Dll
EDSDK_64/Dll/EdsImage.dll
EDSDK_64/Library/EDSDK.lib
EDSDK_API_EN.pdf                # the reference manual (only thorough API doc; read it)
```

* x86 and x64 builds are **separate trees with identical filenames**; you must place the
  matching pair next to your EXE (or on `PATH`). Mixing header and DLL generation is a
  classic source of `EDS_ERR_INTERNAL_ERROR` / instant crashes.
* `EdsImage.dll` is only needed if you call `EdsCreateImageRef` / `EdsGetImage*`; skip it and
  you skip the RAW-support edition.
* Must ship/redistribute `EDSDK.dll` (+`EdsImage.dll` if used) — everything else is headers.

### 1.4 Supported bodies

EDSDK 13.x officially supports, among others (from the CDC master compatibility sheet +
release notes): **EOS R1, R3, R5, R5 Mark II, R6, R6 Mark II, R6 Mark III, R6V, R7, R8, R10,
R50, R50V, R100, RP, R, Ra**; EOS M-series (M50/M50II/M200/M6II), **Rebels** (T5i…T8i, SL1/SL2/SL3,
T100/T7), 1D X (I/II/III), 1D C, 5D II–IV, 5DS/5DSR, 6D/6DII, 7D/7DII, 77D/80D/90D, plus
PowerShot G5XII/G7XIII/SX70HS/V10.

Rule of thumb: **new bodies require a recent SDK** — e.g. R5 Mark II / R100 needed 13.20.11,
R6 Mark III / R6V needed 13.20.21. If you support "whatever the user owns", pin the SDK as a
dependency and require ≥ 13.20.x for R-series.

Older bodies still work with 13.x (down to ~2009 DIGIC III per a Frida trace on a 450D), but
`kEdsPropID_Evf_OutputDevice` value semantics changed at 13.13.20 ("Added Value
0x08: kEdsEvfOutputDevice_PC_…"), so code written for 3.x may misbehave on 13.x.

Sources: <https://developercommunity.usa.canon.com/resource/1744392420000/CDC_EDSDK_Compat_List>
· <https://developers.canon-europe.com/developers/s/article/Latest-EOS-SDK-Version-13-x>
· <https://github.com/SharpAstro/FC.SDK/blob/main/docs/canon-windows-transports.md>

### 1.5 The call surface we actually need (verified IDs from `EDSDKTypes.h` v13.12.31)

**Lifecycle**

```
EdsInitializeSDK()  ->  EdsGetCameraList(&list)  ->  EdsGetChildCount / EdsGetChildAtIndex
                    ->  EdsGetDeviceInfo(cam,&info)   // port, product name, device sub-type
                    ->  EdsOpenSession(cam)
                    ->  [register handlers: EdsSetObjectEventHandler,
                         EdsSetPropertyEventHandler, EdsSetCameraStateEventHandler,
                         EdsSetCameraAddedHandler, EdsSetProgressCallback]
                    ->  ... main loop must call EdsGetEvent() regularly (required on Windows
                        to deliver property/object/state events) ...
                    ->  EdsCloseSession(cam); EdsTerminateSDK();
```

**Properties** (`EdsSetPropertyData(cam, propID, 0 /*param*/, sizeof(val), &val)`,
read with `EdsGetPropertyData`):

| Purpose | Property ID | Notes |
|---|---|---|
| ISO | `kEdsPropID_ISOSpeed` (0x00000402) | values are the camera's raw ISO codes; enumerate first |
| Shutter | `kEdsPropID_Tv` (0x00000406) | e.g. 1/4 s, 1/30 s, …, 30 s, plus bulb sentinel |
| Aperture | `kEdsPropID_Av` (0x00000405) | |
| Exposure comp | `kEdsPropID_ExposureCompensation` (0x00000407) | |
| Drive mode | `kEdsPropID_DriveMode` (0x00000401) | single/continuous/timer |
| AE mode | `kEdsPropID_AEMode` (0x00000400) / `…AEModeSelect` (0x00000436) | read at least; prefer leaving the dial in M and only reading it |
| Storage target | `kEdsPropID_SaveTo` (0x0000000b) | `kEdsSaveTo_Camera`=1, `kEdsSaveTo_Host`=2, `kEdsSaveTo_Both`=3 |
| Capacity report | `EdsSetCapacity(cam, {freeClusters, bytesPerSector, reset})` | after `SaveTo=Host`; `reset=TRUE` on first call, then deltas; camera uses it to show "shots left" and to refuse shooting when the host is "full" |
| LV start/stop | `kEdsPropID_Evf_OutputDevice` (0x00000500) | `kEdsEvfOutputDevice_TFT`=1, **`…_PC`=2**, `…_MOBILE`=4, `…_MOBILE2`=8, `…_PC_…`=0x08 added 13.13.20. `PC` starts LV to host; `TFT` restores camera LCD |
| LV mode | `kEdsPropID_Evf_Mode` (0x00000501) | |
| LV AF mode | `kEdsPropID_Evf_AFMode` (0x0000050E) | `Evf_AFMode_Quick/Live/LiveFace/LiveMulti` |
| LV zoom / rects / histogram | `…Evf_Zoom`(0x507), `…Evf_Histogram`(0x50A), `…Evf_ZoomRect`(0x541), `…Evf_ImageClipRect`(0x545) | metadata living on `EdsEvfImageRef`, not the camera |
| Mirror lockup | `kEdsPropID_MirrorLockUpState` (0x01000421) | `kEdsMirrorLockupStateDisable/Enable/DuringShooting` |
| Mirror up setting | (`kEdsMirrorUpSettingOff/On` enum exists) | DSLRs only; R-series has no mirror |
| Auto power off | `kEdsPropID_AutoPowerOffSetting` (0x0100045e) | **13.x only** (absent in 3.x). Value encoding not in the public header — probe with `EdsGetPropertyDesc` (**UNVERIFIED** what the numbers mean) |
| Screen / EVF off timers | `kEdsPropID_ScreenOffTime`, `…ViewfinderOffTime`, `…ScreenDimmerTime` | 13.18.30+ |
| Sensor temperature | `kEdsPropID_TempStatus` (0x01000415) | 13.x; useful to warn about LV heating (§4.9) |
| Noise reduction | `kEdsPropID_NoiseReduction` (0x00000411) | **high-ISO NR**, not LENR (§4.6) |
| Battery | `kEdsPropID_BatteryLevel` (0x08), `…BatteryQuality` (0x10) | |

**Commands** (`EdsSendCommand(cam, cmd, param)`):

| Purpose | Command ID |
|---|---|
| Fire shutter (also works in LV, mirror drops) | `kEdsCameraCommand_TakePicture` 0x00000000 |
| **Reset the auto-power-off countdown** | `kEdsCameraCommand_ExtendShutDownTimer` 0x00000001 |
| Bulb start / end | `kEdsCameraCommand_BulbStart` 0x00000002 / `kEdsCameraCommand_BulbEnd` 0x00000003 |
| Half/full press with/without AF | `kEdsCameraCommand_PressShutterButton` 0x00000004, param ∈ `kEdsCameraCommand_ShutterButton_OFF`(0), `_Halfway`(1), `_Completely`(3), `_Halfway_NonAF`(0x10001), `_Completely_NonAF`(0x10003) |
| LV autofocus (contrast) | `kEdsCameraCommand_DoEvfAf` 0x00000102, param `EvfAf_OFF/ON` |
| LV manual focus drive | `kEdsCameraCommand_DriveLensEvf` 0x00000103 |
| Mode-dial lock (13.17.10+) | `kEdsCameraCommand_SetModeDialDisable` 0x0000010f-ish (see release note) |

**Status commands** (`EdsSendStatusCommand(cam, cmd, 0)`): `kEdsCameraStatusCommand_UILock` (0x0)
/ `_UIUnLock` (0x1) — locks the body's physical controls so nobody nudges the shutter mid-run;
`_EnterDirectTransfer` / `_ExitDirectTransfer` (0x2/0x3).

**Enumerating legal values (do this, always).** `EdsGetPropertyDesc(cam, propID, &EdsPropertyDesc)`
returns `readOnly`, `numElements`, `propDesc[128]`. Call it for ISO, Tv, Av, DriveMode, Evf_AFMode
at session open; build dropdowns from it and *never* send a value that isn't in the list —
EDSDK returns **`EDS_ERR_DEVICE_BUSY` for illegal values**, which reads like "camera busy" but
is really "bad parameter" (documented-by-experiment on a 5DS: `Av=0x32` → DEVICE_BUSY forever,
`Av=0x33` → OK). 13.19.0 adds `EdsGetPropertyDescEx` for the newer/extended properties.

**Events** (via handlers registered above; `EdsGetEvent()` pumps them on Windows):

* object: `kEdsObjectEvent_DirItemCreated` (0x204), `…DirItemRequestTransfer` (0x208),
  `…DirItemRequestTransferDT` (0x209), `…VolumeInfoChanged` (0x201), `…FolderUpdateItems` (0x203)
* state: `kEdsStateEvent_Shutdown` (0x301), `…JobStatusChanged` (0x302),
  **`…WillSoonShutDown` (0x303)**, `…ShutDownTimerUpdate` (0x304), `…CaptureError` (0x305),
  `…InternalError` (0x306), `…BulbExposureTime` (0x310)
* property: `kEdsPropertyEvent_PropertyChanged` (0x101), `…PropertyDescChanged` (0x102)

`WillSoonShutDown` + `ShutDownTimerUpdate` are the intended hook to call
`ExtendShutDownTimer` and keep a long session alive.

### 1.6 Live view: format, resolution, achievable fps

* **Path:** `EdsCreateMemoryStream(0,&stream)` → `EdsCreateEvfImageRef(stream,&evfRef)` →
  `EdsDownloadEvfImage(cam, evfRef)` (reuse one stream+ref per frame, `EdsSetLength(stream,0)`
  between frames) → the stream now holds a **JPEG** frame; decode with `EdsImage`/your own
  decoder. Metadata (histogram, focus info, zoom rect, and 13.x `Evf_FocalLength`) hangs off
  `EdsGetPropertyData(evfRef, …)`.
* **There is no `EdsSetLiveViewSettings` in EDSDK.** Verified by dumping every exported
  function in the 13.12.31 `EDSDK.h` (60 functions) and by code search across GitHub (0 hits).
  "Starting live view" is exactly: `EdsSetPropertyData(kEdsPropID_Evf_OutputDevice =
  kEdsEvfOutputDevice_PC)`. Frame rate is whatever the body emits; there is no knob for it.
* **Resolution:** roughly 1 MP JPEG, model-dependent. Canon's own table (R5/R6 960×640 in
  still mode; older DSLRs report ~1056×704 / 1024×680). The exact per-body numbers come from a
  Salesforce article I could not fully render (**UNVERIFIED** beyond the R5/R6 960×640 cells).
  Do not hardcode — parse the JPEG header.
* **Frame rate:** ≤ 30 fps on paper (`kEdsStateEvent_BulbExposureTime` etc. suggest a 30 Hz
  event cadence). Measured reality: **~29 fps on macOS, ~9–10 fps on Windows** for the same
  Rebel-class body with EDSDK 3.2 (ofxEdsdk issue #42), and **~10 fps** for EDSDK on Windows in
  an independent 2026 Frida trace (450D). Practical planning number: **10–30 fps, assume 10**.
  Slow LV on Windows is a *host-side* coding artefact (blocking `EdsDownloadEvfImage` +
  missing `EdsGetEvent` pumping), not an SDK limit — a correct threaded implementation gets
  ~30 fps.
* **Cost per frame:** EDSDK over-requests 2 MiB per LV frame (no size query; short read), i.e.
  ~30 ms/frame at 10 fps ≈ 0.8 MiB/s of USB traffic. Negligible vs. RAW download.
* `EdsSetPropertyData(…Evf_OutputDevice = TFT)` (or `PC|TFT`) gives you framing on both the
  host and the camera LCD; leaving it on `PC` only blanks the camera screen.

### 1.7 Image retrieval

Two equivalent routes — pick one per design and be strict about it:

* **A. `SaveTo=Host` (event-driven, recommended for `ministar`).** Set
  `kEdsPropID_SaveTo = kEdsSaveTo_Host`, `EdsSetCapacity(...)`, then each exposure raises
  `DirItemRequestTransfer`. In the handler: `EdsGetDirectoryItemInfo` → `EdsCreateFileStream`
  (use `EdsCreateFileStreamEx` for >260-char paths) → `EdsDownload` → `EdsDownloadComplete` →
  free. You **must** either download or `EdsDownloadCancel` every requested item, or the body
  stalls and stops taking pictures.
* **B. `SaveTo=Both`/`Camera`, then pull on `DirItemCreated`.** Extra card writes (slower
  cadence) but a card copy survives a host crash. Useful for "download later" mode.
* Speeds: RAW/JPEG size dominates. Measured in the wild: 12 MB CR2 and 19 MB CR2 over USB2
  complete through the normal three-phase PTP read; a 20–30 MB CR3 at 1/2 s cadence is *not*
  sustainable on USB2 (§4.10).

### 1.8 Does a still download interrupt live view? — **Yes, assume yes**

* On a DSLR the mirror must drop for the exposure, so the LV stream stops; after the capture
  `EdsDownloadEvfImage` returns `EDS_ERR_OBJECT_NOTREADY` (or `EDS_ERR_DEVICE_BUSY`) until LV
  is re-armed. Known failure mode: after `PressShutterButton`+download the LV stream *never*
  comes back until `kEdsPropID_Evf_OutputDevice` is re-set (and on some bodies the LV
  session must be closed/reopened). Plan for an explicit `lv_restart()` after every still in
  the focusing loop, and treat "LV still dead after N retries" as a session-reopen trigger.
* On mirrorless bodies LV is also unavailable during the sensor readout/exposure, same
  re-arm dance; expect it, don't special-case.
* Therefore: **focus with LV → stop LV → run the capture loop → restart LV only on request.**
  Do not interleave a 2 s cadence with a live LV stream all night; it doubles the moving parts
  and cooks the sensor (§4.9).

Source (LV breaks after capture): <https://stackoverflow.com/questions/42388263/canon-edsdk-taking-photo-in-liveview-breaks-up-the-lv-forever>
· <https://stackoverflow.com/questions/56804264/why-edsdownloadevfimage-always-return-eds-err-object-notready>

### 1.9 Bulb

* Requires the body to be in **bulb exposure mode** (`kEdsAEMode_Bulb` = 4 — i.e. mode dial on
  **B**, or **M** with shutter dialled past 30 s to "Bulb", body-dependent) *and*
  `kEdsPropID_Tv` at the bulb sentinel. Then `BulbStart` … wait … `BulbEnd`.
* `kEdsStateEvent_BulbExposureTime` (0x310) reports elapsed bulb time — use it for the UI and to
  verify the shutter actually opened.
* Always send `BulbEnd` in a `finally` path (crash/exception → otherwise the shutter stays open
  and the body is unusable until power-cycled). Add a host-side watchdog: if the process dies,
  nothing closes the shutter.
* Same feature exists in libgphoto2 (`--set-config bulb=1 … bulb=0`) and in PTP as
  `PTP_OC_CANON_EOS_BulbStart`, so it is not SDK-specific magic.

### 1.10 Disabling LENR / auto power off / sleep — what is and isn't exposed

| Want | EDSDK | Verdict |
|---|---|---|
| Auto power off | `kEdsPropID_AutoPowerOffSetting` (13.x only) + `kEdsCameraCommand_ExtendShutDownTimer` in response to `WillSoonShutDown` | Do both. Relying on the *command* works on every generation; the *property* only on 13.x, and its value encoding is undocumented (**UNVERIFIED**). |
| Screen/EVF timeout | `kEdsPropID_ScreenOffTime`, `kEdsPropID_ViewfinderOffTime` (13.18.30+) | Set to "never/long" if the body offers it (`EdsGetPropertyDesc` will say). |
| **LENR (long-exposure NR)** | **not exposed** — no `*LongExposureNoiseReduction*` property in 13.12.31 headers, and none announced through 13.20.21 | **Set on the camera menu before the session.** LENR doubles every sub by shooting a dark frame of equal length (§4.6). |
| High-ISO NR | `kEdsPropID_NoiseReduction` (0x411) | Exposed; semantics are the high-ISO-NR menu item on most bodies (do not confuse with LENR). digiCamControl documents a `camera.Long_exp_nr` switch, so *some* Canon bodies may surface LENR through 0x411 — verify per body (**UNVERIFIED**). |
| Mirror lockup | `kEdsPropID_MirrorLockUpState` | Works on DSLRs; irrelevant on R bodies. Adds a 2 s (or menu-configured) delay per frame — usually wrong for a 2 s cadence, right for 1–3 s moon shots. |

---

## 2. Open-source / alternative paths on Windows

Status dates are GitHub API `pushed_at` as of 2026-10-24.

### 2.1 digiCamControl (`dukus/digiCamControl`) — C#/.NET, MIT, actively maintained

| | |
|---|---|
| License | **MIT** (`LICENSE` = MIT text; `© 2014 Duka Istvan`). Note the repo also carries GPLv3 text files for bundled third-party tools (enblend/exiv2) — the app itself is MIT. |
| Status | Active: 733★, last push **2026-10-22**, recent merges about focus stacking & parallel Tcl scripting. Stable installer **v2.1.7 (2025-05-30, 86.5 MB)** on digicamcontrol.com; GitHub "releases" are stale (2.0.26/2016) — use the website. |
| Language | C# / .NET (WPF) + `Canon.Eos.Framework` + bundled `EDSDK.dll` |
| Headless? | **Partly.** Three automation surfaces: (a) **`CameraControlCmd.exe`** one-shot CLI; (b) **`CameraControlRemoteCmd.exe`/single-command system** talking to a *running* GUI; (c) **built-in web server on port 5513** (`GET /?CMD=Capture`, `/liveview.jpg`, `/preview.jpg`), plus Tcl scripting, WorkFlow actions/events and a .NET "standalone library". |
| Hard limit | *"Command line utility can't be used when digiCamControl is started even when is minimized"* — the CLI is a separate process that opens its own camera session; and the Remote/Web routes need the GUI resident. Either way you inherit a WPF app. |
| Features we care about | Live view (incl. focus-stacking helpers), **Bulb/Astro module** with `BulbWnd_Show`, `BulbCaptureStart`, `BulbCaptureStop`, `BulbScriptStart`, `Bulb_CaptureTime_<t>` remote commands, PHD-guiding integration, dark astro theme, multi-camera. |
| Reliability | Widely used in photobooths; the Photobooth Project docs now *steer people away* ("the corresponding windows tool digicamcontrol … does its job, but is not maintained anymore and has many open issues") — that is the project's opinion, contradicted by the 2026 commit activity. Treat as "works, but a moving target with GUI coupling". |
| R-series | Camera-name/icon DB in the repo predates the R series; the bundled EDSDK is old-generation. R-body support is **UNVERIFIED** and likely poor. |

CLI/protocol snippets (literal, from the docs):

```
"C:\Program Files\digiCamControl\CameraControlCmd.exe" /capture /iso 1600 /shutter 1/4 /aperture 5.6 ^
     /folder C:\out /filenametemplate shot_#####
CameraControlRemoteCmd.exe /c "set iso 1600|set shutterspeed bulb|camera.Bulb_CaptureTime_60"
curl "http://localhost:5513/?CMD=Capture"
curl "http://localhost:5513/?CMD=LiveViewWnd_Show"    # then: curl -o lv.jpg http://localhost:5513/liveview.jpg
```

Sources: <https://github.com/dukus/digiCamControl> · <https://www.digicamcontrol.com/doc/userguide/cmd>
· <https://www.digicamcontrol.com/doc/userguide/web> · <https://www.digicamcontrol.com/doc/userguide/singlecmd>
· <https://www.digicamcontrol.com/doc/userguide/interface/astromod> · <https://www.digicamcontrol.com/download>
· <https://photoboothproject.github.io/install/install-windows/>

### 2.2 libgphoto2 / gphoto2 — LGPL-2.1, very active, **Linux/macOS only**

| | |
|---|---|
| License | libgphoto2 **LGPL-2.1**; `gphoto2` CLI **GPL-2.0** (linking: fine as a subprocess; static/embedded use triggers LGPL relink obligations). |
| Status | `gphoto/libgphoto2` 1396★, last push **2026-10-24**; latest release **v2.5.34 (2026-05-30)**. CLI repo `gphoto/gphoto2` last release v2.5.32 (2025-08-11). |
| **Native Windows** | **Not supported.** README (master, 2026-10-24): *"libgphoto2 is designed to compile and run on most Unix-like platforms. **Support for operating systems from Microsoft is not currently available.**"* No MSYS2/mingw package that anyone supports; Cygwin builds are unofficial and stale; the `libusbK`/Zadig route (replacing the camera driver with WinUSB) is exactly what breaks Windows-side tethering and is unnecessary. Python wheels confirm it: `gphoto2` 2.6.4 ships `manylinux` + macOS wheels only, **no `win_amd64`**. |
| **WSL2 route** | The supported-in-practice pattern: attach the body to WSL2 with `usbipd-win` (GPL-3.0, v5.3.0, 6254★, 2025-10-11) and run stock Ubuntu `gphoto2`/`python-gphoto2`. Works for stills, config, bulb and preview. See §5 for the mechanics and gotchas. |
| Canon support | `camlibs/ptp2` with Canon vendor extensions. Config keys verified in `config.c`: `/main/capturesettings/{iso, aperture, shutterspeed, bulb, eosremoterelease, viewfinder, output, mirrorlock, autopoweroff, highisonr, picturestyle, eoszoom, eoszoomposition, movierecordtarget}`. |
| Live view | `--capture-preview` (one preview JPEG, downloaded), `--show-preview` (ASCII art), `--capture-movie Ns` (stream of preview frames as fast as the body emits — i.e. a de-facto MJPEG focus loop). `viewfinder=1` enables the LV stream. |
| Bulb | `--set-config bulb=1 --wait-event=<N>s --set-config bulb=0`, mapped to `PTP_OC_CANON_EOS_BulbStart`. `eosremoterelease` gives the half-press/full-press primitive. |
| Intervalometer | Native: `-F/--frames COUNT -I/--interval SECONDS`, plus `--keep` / `--keep-raw` / `--no-keep`, `--capture-image-and-download`. `--reset` resets the USB port of the camera — the practical un-wedge button. |
| LENR | **Not exposed for Canon** (`longexpnr` is Nikon-only in `config.c`; Canon has only `highisonr`) → same menu-only restriction as EDSDK. |
| Headless | Yes, fully — it's a CLI/library. From another process: subprocess + `--list-config/--get-config/--set-config`, or `python-gphoto2` bindings. |

```
gphoto2 --auto-detect
gphoto2 --list-config
gphoto2 --get-config /main/capturesettings/iso
gphoto2 --set-config /main/capturesettings/iso=1600 \
         --set-config /main/capturesettings/shutterspeed=1/4 \
         --set-config /main/capturesettings/aperture=5.6
gphoto2 --capture-image-and-download --keep --filename shot-%05n.cr3
gphoto2 --capture-preview --filename lv.jpg
gphoto2 --capture-movie 10s --filename lv-%03n.jpg
gphoto2 --set-config viewfinder=1
gphoto2 --set-config bulb=1 --wait-event=120s --set-config bulb=0
gphoto2 --frames 1800 --interval 2 --capture-image-and-download --keep
gphoto2 --reset        # recover a wedged PTP session
```

Sources: <https://github.com/gphoto/libgphoto2> · <https://github.com/gphoto/libgphoto2/blob/master/camlibs/ptp2/config.c>
· <https://github.com/gphoto/gphoto2/blob/master/doc/gphoto2.1> · <https://pypi.org/project/gphoto2/>
· <https://github.com/gphoto/libgphoto2/issues/995> (WSL/usbipd discussion) · <https://github.com/gphoto/libgphoto2/issues/1200> (R6 III support, 2026-01)

### 2.3 N.I.N.A. (`isbeorn/nina`) — MPL-2.0, very active, **native Canon driver: YES**

> **CORRECTION (2026-10-24).** This section originally claimed N.I.N.A. had "no first-party
> Canon driver" and that Canon bodies only worked through ASCOM. **That was wrong.** The claim
> came from searching for a file named `CanonCamera.cs`; the actual driver is `EDCamera.cs`
> ("ED" = EDSDK), so a filename search missed it. Re-verified directly against `develop`.
> Details in [08-edsdk-binding-plan.md](08-edsdk-binding-plan.md).

* Status: 255★, last push **2026-10-24**, latest release **3.2 (2025-11-27)**; C#/.NET, Windows.
* Canon support: **first-party and substantial.** Two layers:
  * `NINA.Equipment/SDK/CameraSDKs/EDSDK/EDSDK.cs` — 2608 lines / 125 KB of P/Invoke in namespace
    `EDSDKLib` (Johannes Bildstein's EDSDK wrapper), plus `EDSDKLocal.cs`.
  * `NINA.Equipment/Equipment/MyCamera/EDCamera.cs` — 45 KB, `class EDCamera : ICamera`,
    `Category = "Canon"`. Full bulb (both dialects, with per-body quirks), live view, image
    download, property enumeration.
* Consequence: **N.I.N.A. is the reference implementation to port from**, not an ASCOM escape
  hatch. See 08 for the specifics worth stealing.
* NINA is a full sequencer (platesolving, meridian flip, dithering, autofocus) — most of its
  value is irrelevant to an untracked, no-mount rig, and its model of "one long exposure per
  target" fights a 2 s cadence. Not the right dependency for `ministar`, but the best reference
  implementation to read for ASCOM/Alpaca plumbing.

Source: <https://github.com/isbeorn/nina>

### 2.4 Commercial astro tools (all use EDSDK internally, all Windows-only)

| Tool | Licence / price | Status 2026 | Canon notes |
|---|---|---|---|
| **APT — Astro Photography Tool** v4.80 (2026-06-04) | Commercial; **time-unlimited, feature-limited demo**; paid licence | Actively developed; ships **Canon SDK 13.18.40**, claims EOS "eight generations", R1/R5 MkII in PointCraft | Closest analogue to `ministar`: bulb, live view, plan editor, per-model info matrix. Windows 7–11. Driving it headlessly = GUI automation (not recommended). |
| **BackyardEOS** | Commercial, BinaryRivers; BYE 3.2 stable, **3.5.0.RC9** pre-release (2025-04-05); Classic CAD 49.76 / Premium CAD 71.09 / Trial free | Maintained but slow; open forum thread (2026) asking whether newer **EDSDK 13.x** support is coming for R100 — i.e. currently behind | Windows only, "purpose built for astrophotography", 40+ bodies, LV + bulb + sensor-temp readout (serial cable needed for some models). |
| **SharpCap** 4.1 (4.2 beta) | Free core / **Pro £14/yr** | Active | Supported-camera list has **no Canon DSLR** entry — Canon only via **ASCOM.DSLR** as an ASCOM camera. Not a good primary path for us. |

Sources: <https://www.astrophotography.app/> · <https://www.astrophotography.app/usersguide/what_is_new_.htm>
· <https://www.otelescope.com/store/category/2-backyardeos/> · <https://www.otelescope.com/forums/topic/13069-support-for-the-latest-canon-eos-sdk/>
· <https://www.sharpcap.co.uk/>

### 2.5 KStars/Ekos + INDI on Windows

* KStars **3.8.5 (2026-10-03)** has an official **Windows** download; INDI builds on Windows
  (CI: MSVC + MinGW workflows). So "Ekos on Windows" exists.
* **But the gphoto CCD driver is not usable there**: `indi-gphoto` (in `indilib/indi-3rdparty`,
  LGPL-2.1, active) is a thin wrapper over libgphoto2, which has no Windows support.
  Consequence: on Windows, INDI gets you simulator/ASCOM/Alpaca/ZWO-class drivers, not Canon
  EOS. Canon + INDI ⇒ **Linux only** (which you *can* get inside WSL2 via usbipd, §5).
* If you ever want INDI's ecosystem, the cheaper trick is: run `indi_gphoto_ccd` in WSL2 and
  point Ekos/Alpaca at `localhost:7624`. Adds a whole daemon layer for no gain over `gphoto2`
  CLI at our scale.

Sources: <https://kstars.kde.org/download/> · <https://github.com/indilib/indi> · <https://github.com/indilib/indi-3rdparty>

### 2.6 ASCOM (Windows-native astronomy interop)

* **ASCOM.DSLR** (`vtorkalo/ASCOM.DSLR`, GPL-3.0, C#, last push **2023-11-05**, 47★) — the live
  fork of the old `ASCOM.DSLR`; provides an **ASCOM Camera** (+focuser) device for Canon (EDSDK),
  Nikon (SDK/legacy) and Pentax (pktriggercord). Supports bulb ("Fixed 2020-06 Nikon BulbMode"),
  LV for SharpCap-style hosts, LibRaw-based CR3 preview. GPL-3 → **infects your app if you link
  or fork it**; using it as a separate out-of-process driver (the normal ASCOM pattern) is fine.
* **ASCOM Alpaca** wrapping is the modern variant; several Alpaca camera drivers exist, none
  Canon-specific and well-maintained that I could verify (**UNVERIFIED**).
* **PHD2** — irrelevant here: no mount, no guiding. Do not build on it.
* Practical value for `ministar`: ASCOM.DSLR is the *escape hatch* that makes NINA/SharpCap/
  SGP able to see a Canon. If `ministar` already speaks EDSDK directly, ASCOM buys nothing and
  costs a COM registration + a 2023-era dependency.

Sources: <https://github.com/vtorkalo/ASCOM.DSLR> · <https://github.com/RiaanDeWinnaar/ASCOM.DSLR.2025> (revival attempt, 0★, 2025-09)

### 2.7 Language bindings (all of them wrap `EDSDK.dll` or libgphoto2)

| Binding | Lang | Licence | Status (2026-10-24) | Notes |
|---|---|---|---|---|
| **`pyedsdk`** (`fmargall/pyedsdk`) | Python | MIT | **0.2.16, 2026-06-09**, 5★ — small but current | Windows-only; ships **no** `EDSDK.dll` (licensing); point `PATH` at `…\EDSDK_64\Dll`. Pure-python wheel (`py3-none-any`). Modern alternative to the dead `edsdk4py`. |
| `edsdk4py` | Python | — | **gone**: PyPI JSON → **HTTP 404** (2026-10-24); no matching GitHub repo in search | Do not depend on it. |
| `python-edscdk` / `edscdk` | Python | — | **not on PyPI** (404) | Nonexistent as published. |
| `EDSDK.NET` (`thrixton/EDSDK.NET`, originally `edsdknet`) | C# | MIT-ish (UNVERIFIED) | 2021-11-22, 40★ — **stale** | Clean P/Invoke + `Camera`/`PhotoSession` API; predates 13.x properties (no `AutoPowerOffSetting`, no `TempStatus`). Fine as a *reference*, bad as a dependency. |
| `Canon.Eos.Framework` (`jossef/…` / shipped inside digiCamControl) | C# | MIT | 2014–2020 era, effectively unmaintained | This is what digiCamControl still uses. Old EDSDK generation. |
| `jossef/edsdk-wrapper` | C# | — | 2020-11-06, 46★ | same generation |
| `kritzikratzi/edsdk4j` | Java | — | 2023-02-15, 117★ | JNA |
| **`Blackdread/canon-sdk-java`** | Java | — | **2026-10-01, 117★ — actively tracking EDSDK 13.x** | Best-maintained non-C#/C++ binding; useful as a *constant table* even if you don't use Java. |
| `intradeus/edsdk-wrapper-nodejs` (fork of `napi-canon-cameras`) | Node | — | 2023-03-28, 11★ | N-API; node 14–16 claim, unmaintained. |
| `stavenko/canon-edsdk` | Rust | — | 2025-01-28, 1★ | toy/experimental |
| `jim-easterbrook/python-gphoto2` → PyPI `gphoto2` | Python | LGPL-3.0 | **2.6.4, 2026-06-09**, 429★ | **No Windows wheels** (manylinux/macOS only) → WSL2/Linux only. Excellent quality otherwise. |
| `ofxEdsdk` / `EDSDK-helper` | C++ | — | 2018 / — | Good read-only references for the LV loop and bulb handling. |

---

## 3. Capability matrix (as of 2026-10)

| Path | Host | ISO/Tv/Av | Bulb > 30 s | LV frames to host | Still download | Interval loop over hours | Licence | Maintenance | Binding |
|---|---|---|---|---|---|---|---|---|---|
| **EDSDK, own process** | Win x64 / (Linux 13.18.30+) | ✅ full enum via `EdsGetPropertyDesc` | ✅ `BulbStart/BulbEnd` (dial must be B/M-bulb) | ✅ JPEG ~1 MP, 10–30 fps | ✅ event-driven, `EdsDownload`, ~2–10 s per RAW over USB2 | ✅ best case — you own the state machine; needs watchdog | Canon proprietary; redistribute only inside your app | Current: 13.20.21 (2026-05) | C/C++ direct, C#, Java, Python |
| **EDSDK via digiCamControl** | Win | ✅ | ✅ (Astro module) | ✅ `liveview.jpg` on :5513 | ✅ + optional card copy | ⚠️ GUI must stay resident; single-session CLI vs GUI conflict | MIT (app) + Canon SDK | Active (2026-10) | C# / CLI / HTTP |
| **libgphoto2 in WSL2 + usbipd** | Win→WSL2 | ✅ `/main/capturesettings/*` | ✅ `bulb=1/0` | ✅ `--capture-preview`, `--capture-movie` (lower fps, per-frame subprocess cost) | ✅ `--capture-image-and-download` | ⚠️ good, but needs udev/root, re-attach after sleep, USB reset on wedge | LGPL-2.1 / GPL-2.0 | Very active (2026-10) | C lib, CLI, Python (no Win wheels) |
| **libgphoto2 native Windows** | — | ❌ | ❌ | ❌ | ❌ | ❌ | — | **not supported by upstream** | — |
| **NINA 3.2 + ASCOM.DSLR** | Win | ✅ (via ASCOM) | ✅ | ✅ (ASCOM `StartExposure`/`ImageReady`) | ✅ | ✅ NINA sequencer, huge feature weight | MPL-2.0 + GPL-3.0 driver | Active / 2023 | C# / ASCOM COM |
| **APT 4.80** | Win | ✅ | ✅ | ✅ | ✅ | ✅ (purpose-built) | Commercial | Active 2026-06 | GUI only |
| **BackyardEOS 3.2/3.5RC** | Win | ✅ | ✅ | ✅ | ✅ | ✅ | Commercial | Slow, behind on EDSDK 13.x | GUI only |
| **SharpCap + ASCOM.DSLR** | Win | ✅ | ✅ (ASCOM) | ✅ | ✅ | ⚠️ | Free/Pro | Active | GUI |
| **KStars/Ekos + INDI gphoto** | Linux (WSL2 ok) / **not Win** | ✅ | ✅ | ✅ (INDI `--capture-preview`?) | ✅ | ✅ | GPL-2.0+ | Active | INDI protocol |
| **Canon CCAPI (Wi-Fi/HTTP)** | any (not USB) | ✅ | ✅ (bulb endpoint) | ⚠️ lower fps, Wi-Fi only | ✅ | ⚠️ Wi-Fi dropouts | Proprietary, free SDK | 1.4.0f (2026-08) | HTTP/JSON |

Notes on the CCAPI row: it is the *other* Canon SDK (`CCAPI`), HTTP/JSON over Wi-Fi, no USB; it
**does** expose `longexposurenoisereduction` in its settings namespace (**UNVERIFIED** — I could
not render Canon's CCAPI reference; treat as "worth checking if menu-only LENR control bites").
Not a fit for a tethered USB rig.

---

## 4. Practical gotchas

1. **Mode dial must be M (or B/Bulb).** Writing Tv/Av/ISO, and *especially* `BulbStart`, is
   rejected or silently ignored in P/Auto/green modes. Verify by reading back
   `kEdsPropID_AEMode` on connect and refusing to start the intervalometer otherwise. On
   mirrorless, "bulb" lives past 30 s in the M shutter-speed list, not on a dial.
   13.17.10+'s `kEdsCameraCommand_SetModeDialDisable` prevents mid-session dial accidents.
2. **`EDS_ERR_DEVICE_BUSY` usually means "illegal parameter", not "busy".** Reproducible on a
   5DS: `Av=0x32` ⇒ DEVICE_BUSY forever; `Av=0x33` ⇒ success. Always validate against
   `EdsGetPropertyDesc`. Other real meanings: another app holds the device (§4.5).
3. **Live view does not survive a still** (`EDS_ERR_OBJECT_NOTREADY` after capture) → re-arm
   `kEdsPropID_Evf_OutputDevice` (sometimes close/reopen LV) before the next frame. See §1.8.
4. **USB tethering battery drain.** A tethered EOS with LV running pulls ~400–600 mA:
   a genuine LP-E6N is good for ~1.5–3 h of continuous LV, not a whole night. **Use a dummy
   battery + AC adapter** (ACK-E6/ACK-E17/ACK-E8 class) or an external USB-PD→dummy pack.
   Keep `ExtendShutDownTimer` in the loop and poll `kEdsPropID_BatteryLevel` to abort
   gracefully rather than lose a session at 03:00. **Third-party grips** that feed power
   through the grip's own DC input are a known source of brown-outs and mid-exposure resets
   (**UNVERIFIED** for any specific model — but test yours with a 30 min soak).
5. **Camera sleep = dead session.** Set auto-power-off to Off (menu; or 13.x
   `AutoPowerOffSetting`), and treat `kEdsStateEvent_WillSoonShutDown` as mandatory work: call
   `ExtendShutDownTimer`, and if `Shutdown(0x301)` still arrives, plan for `CloseSession` +
   `OpenSession` + full re-init (LV, SaveTo, capacity, Tv/Av/ISO re-apply from your cached
   state). Also: **WSL2 `usbipd` attachments do not survive host sleep / re-plug** — re-attach
   is required (§5).
6. **LENR doubles your cadence and is menu-only.** With LENR ON, a 10 s sub costs 20 s of
   shutter time plus readout, so a "2 s interval" is nonsense and your dark subtraction is
   being done by the camera (badly, at 1 dark per light). For untracked short subs you want
   **LENR OFF** and a master dark library later. No EDSDK property exists (§1.10); set it in
   the menu. `kEdsPropID_NoiseReduction` (0x411) is the *high-ISO* NR item on most bodies.
7. **Windows auto-play / MTP / EOS Utility port conflicts.** EDSDK does **not** install a
   driver: it opens the stock Windows MTP/PTP device node and drives it with `DeviceIoControl`
   on the WPD interface GUID `{6AC27878-A6FA-4155-BA85-F98F491D4F33}` (verified by Frida trace,
   2026-07-30). Therefore: the body stays visible to Explorer, *and* anything that opens the
   device — EOS Utility (even its tray agent), Windows Photos/AutoPlay import, Capture One,
   Lightroom tethering, another instance of your own app — causes connect failures / DEVICE_BUSY.
   Mitigations: disable AutoPlay for the camera, don't leave EOS Utility installed on the
   imaging box, one process owns the camera, and retry connect with backoff.
8. **Multiple cameras on one hub.** `EdsGetCameraList` handles N bodies, but each needs its own
   session, its own event pumping and its own download worker; USB power is the real limit
   (bus-powered hub + 2 bodies + dummy batteries = brown-outs). Use a **mains-powered** hub,
   short cables, and stagger exposures so only one body is transferring at a time.
   **UNVERIFIED**: EDSDK's behaviour when two bodies are on the same USB2 root port during
   simultaneous LV — assume it degrades.
9. **Sensor heating from live view.** An EOS sensor at ambient+10–15 °C after an hour of
   continuous LV has visibly worse dark current and hot pixels on untracked 2 s subs. Disable
   LV except while focusing; on 13.x poll `kEdsPropID_TempStatus` (0x01000415) and warn; take
   darks at the *end* of the session at the same temperature, not at the start.
10. **Is 1 shot every 2 s for hours realistic?** Arithmetic over USB2 (the realistic link for
    DSLRs, and often for R bodies too — **UNVERIFIED** which R models enumerate as USB3 for
    PTP): a 25–30 MB CR3 needs ~5–15 s of wall clock (transfer + write + camera housekeeping),
    a 3–8 MB JPEG needs ~0.5–1.5 s. So:
    * **RAW at 2 s cadence: not sustainable** over USB2 without dropping frames. Either accept
      ~5–8 s cadence, shoot JPEG (fine for focusing/wide-field short subs), or let the body
      write to card and download in a background queue (cadence then limited by buffer depth,
      ~10–20 frames, then card write speed).
    * **JPEG at 2 s: sustainable**, and this is what 10 fps LV tests plus 0.8 MiB/s LV
      bandwidth suggest is comfortable.
    * Reliability for *hours* is a different axis: expect sporadic `EDS_ERR_COMM_*` /
      DEVICE_BUSY / a silent LV death every few hundred frames. Build: per-frame timeout,
      `EdsCloseSession`/`OpenSession` re-init, `--reset`-equivalent (re-enumerate the camera
      list; on Windows, a device restart may need `pnputil`/unplug or libgphoto2's `--reset`
      in the WSL2 path), and an append-only run log so a resume doesn't lose frames. Soak-test
      30–60 min before a real night.
11. **Cables.** Charge-only USB-C cables and damaged micro-USB cables are the #1 cause of
    "works for 20 minutes then disconnects". Use short, data-rated cables; tape them down;
    a right-angle strain relief on the camera port saves the connector.
12. **Mirror lockup** adds a fixed delay per frame and (on older bodies) disables LV during the
    sequence — wrong default for a 2 s cadence, right for 1–3 s lunar/planetary bursts.
13. **`EdsSetCapacity` is not optional** when `SaveTo=Host`: without it the body may report
    "card full"/refuse to shoot or report nonsense shot counts. Pass `reset=TRUE` once, then
    deltas of free clusters × bytes/sector.

---

## 5. WSL2 specifics (this project's host model)

**Option A (recommended): native Windows camera daemon + WSL2 client.**
Run a tiny `.exe` (C# or C++) that owns EDSDK, exposes `GET /status`, `POST /capture`,
`POST /bulb`, `GET /lv.jpg`(or a WebSocket of JPEG frames), `POST /settings` on `127.0.0.1:8765`.
WSL2 reaches it either through the host IP (`$(ip route show default | awk '{print $3}')`) or,
with Windows 11 22H2+ **mirrored networking** (`networkingMode=mirrored` in `.wslconfig`),
directly via `localhost`. Your existing "copy to `/mnt/c` and run a `.bat`" flow then becomes a
one-line `curl`. This is the only design that keeps EDSDK (i.e. correct R-series support) *and*
a Linux-side codebase.

**Option B: EDSDK Linux inside WSL2 (13.18.30+, Intel 64-bit) + `usbipd attach`.**
Attractive because it removes the Windows/`.bat` layer entirely and keeps one toolchain. Cost:
you fight the device handoff (§below) and Canon's Linux packaging; you also must ship/compile a
Linux binary of the app for the user. Viable, less battle-tested than A for Canon.

**Option C: `gphoto2` inside WSL2 + `usbipd attach`.** Cheapest to prototype; LGPL; loses
high-fps LV, loses LENR, but gains a clean CLI/JSON surface and `--reset`.

**usbipd-win mechanics and gotchas** (v5.3.0, GPL-3.0):

```
# Windows, elevated, once per device (persists across replugs, not across uninstall):
usbipd list
usbipd bind --busid 2-6
# Windows, non-elevated, every attach (dies on reboot / replug / device reset / host sleep):
usbipd attach --wsl --busid 2-6
```

```
# WSL2 side
sudo apt install gphoto2 usbutils
lsusb | grep -i canon           # expect 04a9:xxxx
gphoto2 --auto-detect
```

* Requires **Windows 11 22H2+** (build 22000) for the supported path; Windows 10 is
  best-effort. `wsl --update` first — the kernel must carry the USB/IP client modules.
* Attach is **non-persistent**: reboot, replug, device reset or Windows sleep all drop it.
  Your tool needs a re-attach step (and an elevated `usbipd bind` only once).
* In WSL2 the device appears as root-owned `/dev/bus/usb/…`; either run as root, add a udev
  rule, or use the `usbipd`-documented WSL udev/driver notes.
* While attached to WSL, **Windows loses the camera** — so nothing on the Windows side (EOS
  Utility, Explorer, your own daemon) may be running. Conversely, un-attaching mid-exposure
  wedges the body; `gphoto2 --reset` or a replug fixes it.
* Known-good precedents: the Photobooth Project documents gphoto2-in-WSL as its *recommended*
  Windows install route; several blogs do Sony/Canon in WSL2 with `usbipd`+`gphoto2`. libgphoto2
  issue #995 is the canonical "does this work?" thread.

Sources: <https://github.com/dorssel/usbipd-win> · <https://learn.microsoft.com/en-us/windows/WSL/connect-usb>
· <https://github.com/dorssel/usbipd-win/wiki/WSL-support> · <https://github.com/gphoto/libgphoto2/issues/995>

---

## 6. Recommendation for `ministar`

1. **Ship a Windows x64 EDSDK daemon** (C++ or C#, 64-bit, DLLs from `EDSDK_64/Dll`).
   Session open → enumerate ISO/Tv/Av/DriveMode via `EdsGetPropertyDesc` → read `AEMode` and
   refuse to run unless M/B → `SaveTo=Host` + `EdsSetCapacity` → install handlers → pump
   `EdsGetEvent` → command surface over localhost HTTP/WS.
2. **Focus workflow:** temporary LV (`Evf_OutputDevice=PC`) at 10–30 fps streamed as JPEG over
   WebSocket, manual focus via `DriveLensEvf` if the lens is AF, then `Evf_OutputDevice=TFT`
   and *stop* LV for the run.
3. **Capture loop:** `TakePicture` (or `BulbStart`/`BulbEnd` for >30 s), download on
   `DirItemRequestTransfer`, per-frame timeout + `Close/OpenSession` recovery, `ExtendShutDownTimer`
   on `WillSoonShutDown`, battery + `TempStatus` telemetry, run log for resume.
4. **Config pre-flight checklist printed to the user:** LENR **Off**, High-ISO NR Low/Off,
   auto-power-off Off, mode dial M (or B), card inserted (needed for buffer even in Host mode on
   some bodies — **UNVERIFIED** per body), dummy battery connected, EOS Utility uninstalled/
   disabled, AutoPlay disabled, one process owns the camera.
5. **Keep a `gphoto2`-in-WSL2 adapter** behind the same interface for dev/CI and as a fallback
   for non-Canon bodies. The CLI surface maps 1:1 onto the daemon API.
6. **Licensing hygiene:** don't commit `EDSDK.dll`/headers; document "obtain EDSDK, drop into
   `vendor/edsdk/`"; include the DLL only in installers/releases.

---

## 7. Source index

**Canon**
- EDSDK release note (version history): https://asia.canon/en/campaign/developerresources/camera/cap/edsdk-eos-digital-camera-sdk-release-note
- Developer Community (downloads, compat sheet): https://developercommunity.usa.canon.com/s/ · sheet: https://developercommunity.usa.canon.com/resource/1744392420000/CDC_EDSDK_Compat_List
- SDK access walkthrough: https://developers.canon-europe.com/developers/s/article/How-to-get-access-camera
- "Latest EOS SDK (Version 13.x)" per-body table: https://developers.canon-europe.com/developers/s/article/Latest-EOS-SDK-Version-13-x
- Distribution agreement (v2.14 PDF): https://id.canon/media/migration/local/id/live/developer_resources/EDSDK-v2.14-Developer-Agreement.pdf
- Live-view resolutions (partially readable): https://developercommunity.usa.canon.com/s/article/Liveview-Resolutions-with-EDSDK-and-CCAPI
- CCAPI/EDSDK package overview: https://asia.canon/en/campaign/developerresources/camera/cap

**Headers/API surface used for verification (mirrors of the real SDK)**
- EDSDK 13.12.31 headers: https://github.com/celstark/OpenNebulosity/tree/main/Win_Libraries/EDSDK_v131231
- EDSDK 3.x headers (diffing old→new): https://github.com/kylemcdonald/ofxEdsdk/tree/master/libs/EDSDK/include

**EDSDK behaviour reports**
- LV performance Win vs macOS: https://github.com/kylemcdonald/ofxEdsdk/issues/42
- LV dies after capture: https://stackoverflow.com/questions/42388263/ · OBJECT_NOTREADY: https://stackoverflow.com/questions/56804264/
- DEVICE_BUSY = bad parameter: https://stackoverflow.com/questions/16101332/
- LV setting disabled in camera / EOS Utility holds device: https://stackoverflow.com/questions/73288443/ · https://community.usa.canon.com/t5/Camera-Software/Unable-to-connect-Canon-1500D-with-EOS-Utility-and-My/td-p/378976
- How EDSDK actually talks to the body on Windows (Frida trace, 2026-07): https://github.com/SharpAstro/FC.SDK/blob/main/docs/canon-windows-transports.md
- EOS Utility tray conflicts: https://www.boothjunkie.co.uk/support/troubleshooting/canon-eos-utility-is-still-running-in-the-background/

**Open source**
- digiCamControl: https://github.com/dukus/digiCamControl · docs `/doc/userguide/{cmd,web,singlecmd,remoteutil,interface/astromod}` · https://www.digicamcontrol.com/download
- libgphoto2: https://github.com/gphoto/libgphoto2 (`README.md` platforms section; `camlibs/ptp2/config.c`)
- gphoto2 CLI man page: https://github.com/gphoto/gphoto2/blob/master/doc/gphoto2.1
- usbipd-win: https://github.com/dorssel/usbipd-win · WSL USB guide: https://learn.microsoft.com/en-us/windows/WSL/connect-usb
- NINA: https://github.com/isbeorn/nina
- INDI / indi-3rdparty: https://github.com/indilib/indi · https://github.com/indilib/indi-3rdparty
- KStars downloads: https://kstars.kde.org/download/
- ASCOM.DSLR: https://github.com/vtorkalo/ASCOM.DSLR
- pyedsdk: https://github.com/fmargall/pyedsdk · PyPI `pyedsdk` 0.2.16
- python-gphoto2: https://github.com/jim-easterbrook/python-gphoto2 · PyPI `gphoto2` 2.6.4
- canon-sdk-java (13.x constants): https://github.com/Blackdread/canon-sdk-java
- EDSDK.NET: https://github.com/thrixton/EDSDK.NET

**Commercial**
- APT: https://www.astrophotography.app/ · what's new: https://www.astrophotography.app/usersguide/what_is_new_.htm
- BackyardEOS: https://www.otelescope.com/store/category/2-backyardeos/ · EDSDK-13 thread: https://www.otelescope.com/forums/topic/13069-support-for-the-latest-canon-eos-sdk/
- SharpCap: https://www.sharpcap.co.uk/

**Noise reduction (LENR) behaviour**
- Canon USA "In-Camera Noise Reduction": https://www.usa.canon.com/learning/training-articles/training-articles-list/in-camera-noise-reduction
- Canon Europe "Noise reduction options explained": https://www.canon-europe.com/pro/stories/noise-reduction-low-light/
- Cloudy Nights thread on LENR doubling subs: https://www.cloudynights.com/forums/topic/697200-canon-long-exposure-noise-reduction/

---

### Appendix — things I could not verify

* Exact wording of the **v13** EDSDK licence (only the v2.14 PDF is public).
* Value encoding of `kEdsPropID_AutoPowerOffSetting` (0x0100045e) and whether every 13.x body
  accepts a write to it.
* Per-body live-view resolutions beyond R5/R6 960×640 (Canon's article is a JS-only page).
* Whether `kEdsPropID_NoiseReduction` (0x411) maps to LENR on *any* body (digiCamControl's
  documented `camera.Long_exp_nr` hints at some mapping).
* USB generation (2.0 vs 3.x) actually used by PTP on current R bodies, and hence real download
  throughput per model.
* CCAPI's `longexposurenoisereduction` setting name/values.
* NINA's intent for its partial EDSDK P/Invoke layer.
* Sustained multi-hour 2 s-cadence behaviour on any specific body — nobody publishes soak tests;
  all numbers above are arithmetic + single-session measurements.
