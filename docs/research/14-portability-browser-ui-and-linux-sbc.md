# 14 — Portability: browser UI and a Linux SBC backend

**Status:** feasibility + plan, 2026-10-25. Two decisions taken (§2, §4). Does not change the
desktop product; adds a deployment target.

**The idea (user):** a small Linux box plus a battery pack, taken to a dark site, with a phone as
the management console. The Pi was the first thought; *any* SBC or small Linux PC is acceptable.

---

## 0. Summary

| Question | Answer |
|---|---|
| One UI codebase for desktop + browser? | **Yes — NiceGUI**, replacing the PySide6 recommendation in 09. Its native mode is `pywebview`, not Electron (§2). |
| Port the camera control to Linux? | **Cheap for everything except the backend.** `manualfocusdrive` is the same three-tier primitive (§3). |
| Is an SBC fast enough? | **Yes, if** resolution drops to ~2K, registration uses the predicted-rotation prior, stacking runs in a thread, and the solver runs every N frames rather than every frame (§4). |
| What actually limits cadence? | **The camera's USB 2 link**, ~1–1.5 s per 25–30 MB CR2. Not the CPU (§4.5). |

---

## 1. Architecture

A browser UI means the Python process must run where the camera is. So this is not "one app
rendered two ways" — it is a **client–server split**:

```
   laptop (today)                      dark site (proposed)
   ┌──────────────────────┐            ┌──────────────────────┐
   │ Python + camera      │            │ Python + camera      │  ← the SBC
   │ NiceGUI server       │            │ NiceGUI server       │
   │ pywebview window     │            │ HTTP on the hotspot  │
   └──────────────────────┘            └──────────┬───────────┘
                                                  │ WiFi
                                       ┌──────────▼───────────┐
                                       │ phone browser        │  ← the console
                                       └──────────────────────┘
```

Everything heavy stays on the device; the phone is a thin client. That is the right split for
weak hardware, because rendering happens in the client's browser.

---

## 2. UI: NiceGUI supersedes PySide6

⚠️ **Correction to my own advice:** NiceGUI's native mode is **`pywebview`**, using the OS webview
(WebView2 on Windows, WebKitGTK on Linux, WKWebView on macOS). It ships **no browser engine** —
a few MB, not Electron's ~150 MB. So the native wrapper is cheap.

**Decision: one NiceGUI codebase for both deployments.** `native=True` on a laptop, plain serving
on the SBC. This supersedes the PySide6 recommendation in brief 09.

What survives from 09 and what does not:

| 09 finding | Status |
|---|---|
| Never scale a 24 MP frame on the GUI thread; downscale with OpenCV in the worker | **stands** — more important now, on a slower client |
| `QImage.scaled` is 74 ms on 24 MP | Qt-specific; the *rule* stands |
| PySide6 onedir, 627 ms cold start, 131.8 MB | **superseded** |
| Threading model, cross-thread delivery costs | superseded; NiceGUI pushes over websocket |

**Honest cost of the change:** the app becomes a local HTTP server plus a webview window. Cold
start is worse than PySide6's measured 627 ms — expect **1–2 s** — and it binds a localhost
socket, which is where Windows Firewall prompts come from (00 §8). That is a real regression
against the desktop numbers, accepted for one frontend instead of two.

---

## 3. Linux camera backend: libgphoto2

### 3.1 The decisive finding

`manualfocusdrive` exists in libgphoto2's Canon PTP driver with **the same three-tier structure as
EDSDK** — confirmed in a real device dump (`canon-eos6d-mark2.txt`):

```
/main/actions/manualfocusdrive   Label: Drive Canon DSLR Manual focus   Type: RADIO
  Choice: 0 Near 1   Choice: 1 Near 2   Choice: 2 Near 3
  Choice: 3 None     Choice: 4 Far 1    Choice: 5 Far 2   Choice: 6 Far 3
```

So `Direction`, the motion planner, the V-curve routine and the HFD metric port **unchanged in
logic**. That was the largest single risk in this whole idea, and it is gone.

### 3.2 Two quirks that would silently break a naive port

1. **Repeat calls do not stack.** Setting `Far 2` twice only takes effect once — the value must be
   alternated with `None` between calls (libgphoto2 issue #767). Our motion planner emits
   `Near3 × 419`; against this API that would do almost nothing. The driver has to interleave
   `Near3, None, Near3, None, …`.
2. **Live-view only**, same as `DriveLensEvf`. Already assumed (11 §1).

### 3.3 Port cost, module by module

| Module | Cost |
|---|---|
| `solver/astap.py` | **none** — ASTAP ships Linux builds; identical flags and `.ini` contract |
| `focus/*` | **none in logic** — new driver only |
| `session/state.py`, `io/fits.py` | none |
| `config.py`, `preflight.py` | small — Linux paths; detect libgphoto2 instead of `EDSDK.dll` |
| camera backend | **the work** — a new driver behind the existing `FocusDriver` / `Camera` protocols |
| UI | the other work |

The protocols paid for themselves: `FocusDriver` and `Camera` were written so the routine is
testable without hardware, and that same seam is now the port boundary.

### 3.4 Also confirmed available
`autofocusdrive` (an action), `eoszoom`, ISO/Tv/Av as `/main/capturesettings/*`, preview via
`--capture-preview`, and bulb — which we descoped anyway (08 §6). Python bindings:
`python-gphoto2` (ctypes, maintained, with live-view and config-GUI examples to crib from).

---

## 4. Compute: why an SBC is enough after all

Brief 06 measured **150–250 ms/frame** for live stacking on a laptop. Naively an SBC is 5–10×
slower, which would push cadence past 3 s and — via 10 §2.1e — cut the usable focal length at
δ=45° from ~55 mm to ~28 mm. Wide-field only.

That pessimism assumed **serial, full-resolution, solve-every-frame**. Three decisions already
taken remove most of it.

### 4.1 Cadence is a maximum, not a sum

If stacking runs in a worker thread, it overlaps the time spent waiting on the camera and USB:

```
t_frame ≈ max(exposure + download, stack) + overhead
```

Exposure is 0.3–3 s and a 25–30 MB CR2 over the 60D's USB 2 takes ~1.0–1.5 s, so there is
**1.5–4.5 s per frame of camera-wait time** for the stacker to hide inside.

- stacking at 2K with predicted rotation, SBC: **~0.25 s** → completely hidden
- stacking at 2K with full star matching, SBC: **~1.5 s** → hidden, but with no margin
- stacking at full resolution, SBC: several seconds → **not** hidden, and cadence collapses

So threading converts a sum into a max, and it only works because the two activities use
different resources — USB I/O versus CPU.

⚠️ **Requires numpy to release the GIL.** Brief 05 measured `ctypes.windll` releasing it (0.94×
parallel) but *pure-Python* threads serialising at 1.84×. So the stacker must stay in vectorised
numpy/OpenCV, with no per-pixel Python loops. Otherwise the thread buys nothing.

### 4.2 Solving every N frames is the bigger win

A solve is a **subprocess spawn** plus a 36 MB image read. Hinted on the laptop: 0.74 s. On an SBC
plausibly **1.5–3 s** — comparable to the entire frame cadence, and it cannot be hidden because
the result is needed immediately.

Brief 10 §3 already specifies solving once a minute and **dead-reckoning** the target position in
between, using the fixed-camera model. At a 2 s cadence that is roughly **one solve per 30 frames**.

Effect on cadence:

| strategy | cadence on SBC | fl_max at δ=45° (10 §2.1e) |
|---|---|---|
| solve every frame | ~3–4.5 s | ~40–55 mm |
| solve every N, dead-reckon | **~1.5–2 s** | **~83–111 mm** |

So of the two interventions, **the solve cadence is the larger win** — it removes a serial critical
path, whereas threading only hides work that was already off the critical path.

### 4.3 They are the same decision, not two

This is the part worth stating plainly: **predicted-rotation registration *requires* a trustworthy
drift prediction, which only exists because we solve periodically and dead-reckon in between.**

```
solve every N ──► known ψ and camera alt/az ──► exact predicted rotation (10 §3.2)
                                             └─► phase-correlate only the translation residual
                                                 → no per-frame star detection (the expensive part)
```

Without the solve cadence there is no prediction, and registration falls back to full star
matching at ~6× the cost. So the user's two instincts are not independent optimisations — they are
one architecture, and each is necessary for the other to pay off.

### 4.4 Resolution strategy: 2K for the live stack

Decision taken: **~2K for live stacking.** Beyond raw speed this also keeps the SBC's memory
footprint sane, and it matches what the phone can display anyway.

Two consequences to keep straight:

1. **There are now two megapixel numbers** — ~18 MP captured, ~2.8 MP stacked. Brief 10's "usable
   megapixels" readout is about the *captured* field. The UI must not conflate them, or users will
   think they are losing data they are not.
2. **The 2K preview is lossy, so the originals must be kept.** Binning 24 MP → ~2.8 MP is ~2.9×
   linearly, taking the ~1.75 px PSF down to ~0.6 px — undersampled (03 §3). That is fine for
   *seeing* the image and wrong for *keeping* it. The full-resolution stack happens later, from
   saved frames, which is why the SBC needs SSD rather than an SD card.

### 4.5 What the bottleneck actually becomes

With 2K, predicted rotation, a worker thread and one solve per N frames, nothing CPU-bound sits on
the critical path — and the frame time is set by **USB 2 transfer**: ~1.0–1.5 s for a 25–30 MB CR2.

That is the best available outcome, because it is a *fixed* cost:

- it does not improve if you buy a faster box;
- and, importantly, it does not get worse on a cheap one.

So the portable build's focal-length ceiling is set by the camera's USB link, not by the SBC. That
is the answer to "is the portable version crippled?" — no, provided all four decisions hold.

---

## 5. Deployment

| Concern | Approach |
|---|---|
| Networking | SBC as WiFi hotspot; phone joins. Bandwidth is a non-issue: 1 fps × ~150 KB ≈ **1.2 Mbit/s**. 2.4 GHz for range. |
| Addressing | mDNS (`ministar.local`) via avahi, so nobody types an IP |
| Captive portal | Phones pop a "no internet" dialog on an uplink-less hotspot. Serve a redirect to the app so it lands in the right place |
| Storage | SSD, not SD — thousands of 25 MB frames, and card wear |
| Power | Two rails: SBC (5 V, 3 A class) and camera (dummy battery). Brownout risk when the shutter fires, so keep them separate |
| **Runtime limit** | **The phone.** Hours of screen-on console is roughly a full phone charge — the phone dies before the SBC does |

---

## 6. A constraint this deployment creates that no brief covers

**Dark adaptation.** A phone at normal brightness ruins the night for everyone present, and this is
the difference between a pleasant remote session and a miserable one. Required:

- red-only palette, no white flashes, no bright loading spinners;
- very high contrast, because the screen will be dimmed to near minimum;
- large touch targets, readable at arm's length while holding a red torch;
- assuming the phone is *already* at its lowest brightness, and possibly behind a red film.

This is arguably more important than the widget library choice, and it should be treated as a hard
requirement of the UI rather than a theme option.

---

## 7. What changes elsewhere

| Brief | Change |
|---|---|
| 09 (GUI) | PySide6 recommendation **superseded** by §2; its measurements still inform the image path |
| 08 (EDSDK) | unchanged for Windows; Linux gets a parallel backend (§3) |
| 11, 12, 13 | unchanged — the focus logic, wizard and head calibration are platform-neutral |
| 10 | "usable megapixels" must be distinguished from the 2K *stacked* resolution (§4.4) |
| 06 | the predicted-rotation optimisation moves from "nice" to **required for the portable target** |

---

## 8. What to measure before committing

1. **Actual USB 2 transfer time** for one CR2 from the 60D. Everything in §4.5 rests on 1.0–1.5 s,
   which is an estimate.
2. **Stacking cost at 2K on the target box** with the predicted-rotation path. If it is under
   ~0.5 s, threading hides it and the design holds.
3. **ASTAP solve time on the SBC** — sets N in "solve every N frames".
4. **Live-view frame rate over gphoto2 on the SBC.** Forum reports of significant lag on Pi-class
   hardware; our focus hunt needs it responsive (12 §4.2).
5. **`manualfocusdrive` behaviour on a 60D specifically** — the option is confirmed present for
   other Canon bodies, not for this one.

---

## 9. Open questions

- Does NiceGUI's native mode start fast enough on a laptop to not feel heavy? 1–2 s is a guess.
- Whether the phone should be able to trigger capture and reframe actions, or only observe. Touch
  latency over WiFi plus a human holding a phone means accidental taps matter.
- Whether to support a **second client** (e.g. a laptop alongside the phone) for a richer view.
  NiceGUI serves multiple sessions naturally, but two operators is a scope question.
- Is 2K the right stack resolution, or should it follow the display (a phone is ~1.2K wide)? Worth
  tying to the client rather than fixing at 2K.
