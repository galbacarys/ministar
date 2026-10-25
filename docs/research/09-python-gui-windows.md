# 09 — Python GUI on Windows: what to build ministar's operator console with

> ⚠️ **Superseded in part, 2026-10-25.** The *toolkit choice* below (PySide6) is superseded by
> [14-portability-browser-ui-and-linux-sbc.md](14-portability-browser-ui-and-linux-sbc.md) §2:
> the product now also targets a headless Linux box with a phone browser as the console, so one
> NiceGUI codebase serves both deployments instead of one PySide6 frontend. The **measurements**
> here remain valid and still inform the image path — in particular "never scale a 24 MP frame on
> the GUI thread", the 1 fps budget, and the 24 MP downscale costs. Read §2 of brief 14 first;
> read this brief for the numbers.

**Scope.** Pick one desktop GUI toolkit for a Windows-only Python astrophotography app, and justify it
with numbers. Every toolkit below was installed into a **vendored python-build-standalone 3.13.15**
tree on this machine (the exact shipping contract from brief 05), then made to show a realistic
ministar window so that cold start, bundle size, and the 24 MP preview path could be measured rather
than guessed.

**Labelling convention** (consistent with briefs 00–08)

| Tag | Meaning |
|---|---|
| **[M]** | **Measured** on this machine in this pass, 2026-10-24. Wall-clock numbers are medians of the last 5 of 7 runs (2 warm-ups discarded); micro-benchmarks are medians of 10–15 iterations after 3 warm-ups. All timings taken on the native Windows side with a Windows Python parent — no WSL launch overhead in the numbers. |
| **[V]** | **Verified** fact — command output, file listing, or source read. Not a benchmark. |
| **[C]** | **Cited** from upstream docs/source, URL in §16. |
| **[U]** | **UNVERIFIED** — plausible, not tested here. (Every such claim also carries the literal word `UNVERIFIED` where it is load-bearing; anything else marked `[U]` is a figure we estimated rather than measured.) |

Rig: Windows 10.0.26200.9457, AMD Ryzen AI 7 350 (16 threads), 15 GB RAM. Session was **locked /
RDP-disconnected** during the run, so *raster conversion* costs are trustworthy but *on-screen present*
costs are not — flagged where it matters. Test tree: `C:\Users\username\guiresearch`, venv on
`cpython-3.13.15-windows-x86_64-none` from `uv python install 3.13` (i.e. python-build-standalone).

---

## 0. Executive summary

1. **Build it with PySide6 6.12 / Qt 6.12 LTS, Qt Widgets, LGPL-3, shipped as a PyInstaller *onedir*
   tree alongside the vendored interpreter.** Measured on this machine: a frozen onedir Qt app with a
   24 MP preview goes from process spawn to first painted frame in **627 ms wall / 396 ms in-process
   [M]**, installs at **131.8 MB [M]**, and gives a genuinely native Windows 11 window: the active
   `QStyle` on this box reports `objectName() == "windows11"` and the app inherits the system dark
   colour scheme and the user's accent colour **without a single line of styling code [M]**.
2. **Runner-up is Tkinter + `sv-ttk`, and the honest gap is smaller than the folklore says** — 450 ms
   and 73 MB [M], `sv-ttk` is 10 ms of import time and looks like Windows 11 *content* [C]. It loses on
   three things only: light window chrome around dark content, no smooth arbitrary-ratio image scaling
   in Tk itself, and no real widget-styling vocabulary for the "nudge" panel we want. If the preview
   were an 8-bit downscale and the UI were 6 controls, Tk would win on launch cost and bundle.
3. **The GUI thread must never touch a 24 MP frame.** Measured on 6000×4000 uint16: `QImage` view is
   free (0.00 ms) but the *smooth* downscale to 1280 px costs **74.6 ms in Qt** and **6.1 ms in
   OpenCV `INTER_AREA`** [M]. Pipeline: worker thread does `cv2.resize` → wrap in `QImage` → emit →
   GUI thread does `QPixmap.fromImage` (**0.65 ms**) → `setPixmap`. GUI thread occupancy ≈ 2 ms per
   frame, i.e. 0.2 % of a 1 fps budget.
4. **Qt will not protect you from the "never touch widgets from a worker thread" rule — this is
   measured, not doctrinal.** 300 illegal `setPixmap`/`repaint` calls from a Python `threading.Thread`
   produced **no exception, no qWarning, no crash** [M]. `QPixmap.fromImage` off-GUI-thread likewise:
   no error [M]. So the rule has to be enforced by architecture, not by the framework.
5. **Cross-thread delivery is cheap and boring:** 200 emits from a plain non-Qt thread, delivered
   200/200 to a `@Slot(object)` in the GUI thread, **median latency 0.62 ms, p99 1.00 ms [M]**.
   No `QThread` subclass is required — a daemon `threading.Thread` + `Signal(object)` + AutoConnection
   (auto-upgrades to `QueuedConnection` across threads) is the whole mechanism.
6. **`QImage(numpy.data, …)` is a landmine and we reproduced the failure mode.** Delete the numpy
   buffer while a `QImage` view is alive and you do not get an exception — `pixel(10,10)` returned
   **4293585642** on a `Grayscale16` image [M]. Silent garbage, or a crash under the right allocator
   pressure. `.copy()` costs 7.65 ms [M] and is the boring, correct answer.
7. **The LGPL is four clerk chores, not a blocker.** Ship Qt's DLLs unmodified and side-by-side
   (onedir), ship the LGPLv3 text + a Qt acknowledgement, ship/offer Qt's corresponding source, and
   don't static-link or patch Qt [C]. PySide6 is `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` [V
   PyPI metadata]; **PyQt6 is GPLv3-or-commercial, i.e. unusable in a closed app without paying
   Riverbank** — which is why the Qt answer here is PySide6 and not PyQt6.
8. **Rejected, with reasons:** wxPython (native and fine, but 61 MB of install for a look that is
   *older* Windows, and a 0.x-level plugin/packaging story), Dear PyGui (float-32-RGBA-only textures
   would make a 24 MP frame a **384 MB** array; and `dpg.get_dearpygui_version()` hard-crashed with
   0xC0000005 in our probe [M]), Flet/NiceGUI/pywebview/Electron (a webview plus a local server to
   display one image offline), Kivy (no release since 2024-12-26 [V]), GTK4/PyGObject (**no Windows
   wheels exist on PyPI — MSYS2 only** [V]).

---

## 1. What the UI has to do, stated as constraints

| Requirement | Consequence |
|---|---|
| Live-stack preview, 24 MP frame, ~1 fps | Need ≤ ~10 ms/frame of *GUI-thread* time, and a resample path that is not the toolkit's naive path. Rules out anything that copies a 24 MP buffer per frame into a browser/JS bridge. |
| Exposure / ISO / aperture controls | Ordinary widgets. Rules out immediate-mode-only for text entry comfort. |
| Plate-solve status panel (numbers + state) | Rich text/labels, monospace alignment. Needs a real layout engine that survives DPI changes. |
| Directional nudge indicator + per-axis degrees | Custom painting or large glyph labels; must not look like a Win95 group box. |
| Log pane | Selectable, scrollable, monospace, incremental append. |
| Long-lived process; camera + solver + stacker threads | Must have a *defined* cross-thread repaint mechanism, not "call the widget and hope". |
| Must look good on Windows 11, dark | Native-or-convincing controls, dark title bar, 150 % scaling. |
| Ships to end users; vendored pbs tree; bundles cleanly | No PyPI-at-runtime; must survive PyInstaller/Nuitka under a copied interpreter; installer size matters but is not the top constraint. |
| Cold start matters (brief 05: users double-click and expect a window) | Target ≤ ~1 s to first paint. |

Nobody in this market ships a webview for the live image. §12 documents that.

---

## 2. Versions in play (all resolved from PyPI on 2026-10-24) [V]

| Package | Version | Uploaded | Licence (PyPI metadata) | Notes |
|---|---|---|---|---|
| PySide6 / PySide6-Essentials | **6.12.0** | 2026-10-08 | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` | Qt 6.12 **LTS** (released 2026-09-30, 5-year maintenance) [C] |
| shiboken6 | 6.12.0 | 2026-10-08 | same | binding runtime |
| PySide6-Addons | 6.12.0 | 2026-10-08 | same | 40.5 MB win wheel — **not needed** (Charts/WebEngine/3D) |
| PyQt6 | 6.11.0 | 2026-03-30 | GPLv3 / commercial (Riverbank) [C] | excluded on licence |
| wxPython | **4.3.2** | 2026-10-13 | wxWindows Library Licence (LGPL + exception) [C] | 66 MB win wheel; 4.3.3 alphas nightly |
| ttkbootstrap | **2.2.4** | 2026-10-24 | MIT | 30 themes, `bootstyle` keywords [C] |
| sv-ttk | **2.6.1** | 2025-06-08 | MIT | Windows 11 look; Tk 9 compatible [C] |
| customtkinter | **6.0.0** | 2026-06-24 | MIT (metadata says CC0 — wrong, and that is itself a smell) | no GitHub releases |
| dearpygui | **2.3.1** | 2026-05-02 | MIT | textures are float32 RGBA only [V] |
| flet | **1.0.5** | 2026-10-21 | Apache-2.0 | Flutter client + Python backend |
| nicegui | **3.18.0** | 2026-10-07 | MIT | FastAPI + uvicorn |
| pywebview | **6.2.1** | 2026-04-15 | BSD-3 | WebView2 on Windows |
| kivy | **2.3.1** | **2024-12-26** | MIT | repo active, no release in ~22 months |
| pygobject (GTK) | 3.58.1 | — | LGPL | **sdist only — no Windows wheel** [V] |
| Python | **3.13.15** (pbs, `uv python install 3.13`) | — | PSF-2.0 | Tcl/Tk **8.6.12** bundled [M] |
| PyInstaller | 6.22.3 | 2026-09-12 | GPLv2+exception | |
| Nuitka | 4.3.1 | 2026-10-17 | AGPLv3 (tool) | |

---

## 3. Cold start, measured three ways

**(a) Import cost in a warm filesystem cache** — `python -c "import X"` in a fresh process, median of 5 [M]:

| Import | ms | Δ vs bare |
|---|---|---|
| `pass` (bare interpreter) | **68.7** | — |
| `import numpy` | 238.4 | +170 |
| `import tkinter, tkinter.ttk` | **102.5** | +34 |
| `import tkinter, sv_ttk` | 113.1 | +44 |
| `import tkinter, ttkbootstrap` | **342.5** | **+274** |
| `import PySide6.QtCore` | 219.1 | +150 |
| `import PySide6.QtWidgets` | **228.2** | +160 |
| `import PySide6.QtQuick, PySide6.QtQml` | 244.7 | +176 |
| `import wx` | 112.7 | +44 |
| `import dearpygui.dearpygui` | 120.0 | +51 |
| `import numpy, PySide6.QtWidgets` | 342.6 | +274 |

**(b) Spawn → first painted frame, in the venv (unfrozen)**, realistic window (preview + sliders +
button + log) [M]:

| GUI | wall ms | spawn→paint ms | breakdown (in-script, ms since interpreter start) |
|---|---|---|---|
| Qt Widgets | 744 | **583** | numpy+Qt import 263 · `QApplication()` +40 · 24 MP smooth scale→pixmap +100 · `show()` 178 |
| Tk 8.6 + ttk | 577 | **471** | import 216 · `Tk()` +98 · PIL LANCZOS resize+PhotoImage 98 |
| Tk + sv-ttk | 570 | 455 | theme import is ~10 ms |
| wxPython | 615 | 502 | import 188 · `wx.App` 17 · bitmap build 226 |
| Dear PyGui | — | crashed in probe (see §8) | |

**(c) Spawn → first painted frame, frozen onedir** (`PyInstaller --onedir`, python-build-standalone
3.13.15, numpy + toolkit, `--console` build so we could read the timestamp) [M]:

| Bundle | build time | dist size | wall ms | spawn→paint ms |
|---|---|---|---|---|
| **Qt Widgets onedir** | 65 s | **131.8 MB** | **627** | **396** |
| Tk + ttk onedir | 20 s | **73.3 MB** | **450** | **304** |
| Qt Widgets **onefile** | 81 s | 50.5 MB (single exe) | **2442** | 662 |

**Reading.** Qt costs **+177 ms wall** and **+58 MB** over Tk frozen. Freezing *helped* Qt (744 → 627 ms)
because the venv layer disappears. `--onefile` costs **+1.8 s of pure extraction** for a 50 MB exe —
same conclusion as brief 05 §3, so **onedir is settled regardless of toolkit**. `ttkbootstrap`'s
274 ms import is a real, avoidable launch tax; `sv-ttk`'s 10 ms is not.

---

## 4. Bundle size, measured

| Component | Installed (site-packages) | Frozen onedir contribution |
|---|---|---|
| PySide6 (Essentials, Widgets-only app) | **218.7 MB** + shiboken6 3.1 MB | **77.2 MB** (PyInstaller prunes unused modules) |
| ↳ `plugins/` (all) | 10.5 MB | 5.8 MB |
| ↳ `translations/` | 14.1 MB | 7.0 MB (deletable — we ship one language) |
| ↳ `qml/` | 13.9 MB | 0 MB (**not collected for a Widgets app** [V]) |
| ↳ `metatypes/` | 7.0 MB | 0 |
| numpy 2.5.4 + `numpy.libs` | 41.8 MB | 27.6 MB |
| Pillow 13.0.0 | 14.8 MB | ~14 MB (only if we use it — **we should not need PIL at all with Qt**) |
| wxPython | **60.9 MB** | ~45 MB (UNVERIFIED) [U] |
| dearpygui | 5.4 MB | ~5 MB |
| Tcl/Tk (inside pbs) | 8.3 MB `tcl/` + 2 DLLs | ~12 MB |
| ttkbootstrap | 2.1 MB | 2 MB |
| sv-ttk | 0.1 MB | 0.1 MB |

Largest files in the measured Qt onedir tree [M] — the trimming table:

| MB | file | verdict |
|---|---|---|
| 20.6 | `PySide6/opengl32sw.dll` | software OpenGL fallback; removable for a raster Widgets app, **test first** |
| 20.6 | `numpy.libs/libscipy_openblas64_*.dll` | keep — that *is* the stacking math |
| 10.5 | `PySide6/Qt6Core.dll` | keep |
| 9.7 | `PySide6/Qt6Gui.dll` | keep |
| 8.0 | `libcrypto-3-x64.dll` | only needed via Qt6Network; prunable if unused |
| 6.5 | `PySide6/Qt6Widgets.dll` | keep |
| 6.2 | `python313.dll` | keep |
| 7.0 | `PySide6/translations/*` | delete (keep `qtbase_*.qm` only if we want translated standard dialogs) |

So the honest Qt number is **~132 MB out of the box, ~105 MB after deleting translations +
`opengl32sw.dll` + unused crypto**, versus **~73 MB** for Tk. Delta ≈ 60 MB, on top of a 45 MB
interpreter + ~230 MB of numpy/cv2 from brief 05. **Bundle size is not a decision-maker at this
scale**; it is a footnote in the installer.

---

## 5. Option 1 — PySide6 / Qt for Python (recommended)

### 5.1 Status and licence

- **Qt 6.12 is an LTS** release: *"6.12 is a Long-Term Support (Qt LTS) release, meaning it gets
  maintenance over a 5-year period"* [C]. Released 2026-09-30; PySide6 6.12.0 wheels 2026-10-08 [V].
  This is the first LTS since 6.8 — the *reason to adopt now* rather than wait.
- Wheels are `cp310-abi3-win_amd64` — one wheel covers Python 3.10→3.15 [V], so the vendored
  interpreter version is a free variable. Confirmed working on **3.13.15 pbs** [M].
- Licence: PySide6 is **LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only** [V pyproject metadata].
  Take the LGPL-3 branch (§5.8).
- Commercial escape hatch exists (Qt for Application Development Professional/Enterprise) — pricing
  is per-seat/per-year and **not quoted here [U]**; only relevant if legal review rejects LGPL.

### 5.2 Does it look native on Windows? Measured, not asserted

```
STYLE: windows11 | hints: ColorScheme.Dark
after setColorScheme(Dark): ColorScheme.Dark
```

- `QApplication.style().objectName()` returns **`windows11`** on this Windows 11 machine with Qt
  6.12 [M]. The style is a *plugin*: `PySide6/plugins/styles/qmodernwindowsstyle.dll` (0.24 MB) [V],
  and the docs confirm *"The platform styles, such as `windows11` on Windows and `macos` on macOS, are
  plugins that are available on their platform only"* [C].
- **Dark mode is inherited, not painted by us.** `styleHints().colorScheme()` reported `Dark` because
  the OS is dark; `setColorScheme()` can force it [M]. Qt has supported Windows dark mode since 6.5,
  with the caveat that the *native* UxTheme path cannot draw dark, which is precisely why Qt grew the
  `windows11` style: *"We do support dark mode on Windows, but not if the native (UxTheme based) style
  is used"* [C].
- The screenshot taken with `QWidget.grab()` (real render, not a mock-up) shows: dark panels, the
  user's **accent colour on slider grooves**, `QGroupBox` frames, a flat `QPlainTextEdit` log, all
  matching Windows 11 metrics. That is the "looks good on Windows" bar met with **zero** custom
  styling. A stylesheet (`QSS`) can then add the astro-specific bits without fighting the platform.
- **Gotcha worth writing down:** if `qmodernwindowsstyle.dll` is missing from the bundle, Qt does not
  fail — it silently falls back to `Fusion`, and the app looks like a 2015 Linux app. Plugin
  collection is therefore a *visual* correctness issue, not just a startup issue (§5.7).

### 5.3 High DPI

- *"Qt 6 is Per-Monitor DPI Aware V2 by default"* and *"Qt uses the Windows display scale settings
  automatically; no specific settings are required… at 175 % scale Qt apps see a device pixel ratio of
  1.75"* [C]. That is the whole story: we do not set `AA_EnableHighDpiScaling` (a no-op in Qt 6) or any
  manifest.
- Consequence for the preview: pre-scale the frame to **widget size × `devicePixelRatio()`** physical
  pixels and call `pixmap.setDevicePixelRatio(dpr)`, otherwise the image is resampled twice (blurry at
  150 %, which is the default on most laptops). At 150 %, the resample target grows 1280 → 1920 px;
  `cv2 INTER_AREA` of 24 MP to that is still single-digit milliseconds [UNVERIFIED for the exact figure — only
  the 1280 target was measured, 6.06 ms].
- `QT_SCALE_FACTOR_ROUNDING_POLICY=PassThrough` is the default [C], so fractional scaling is honest.

### 5.4 The 24 MP frame path — the numbers (6000×4000 uint16 = 24.0 MP)

All [M], Qt 6.12.0, single thread unless stated:

| Operation | median | min |
|---|---|---|
| `QImage(arr.data, w, h, arr.strides[0], Format_Grayscale16)` (view, no copy) | **0.00 ms** | 0.00 |
| `view.copy()` (Qt owns the pixels) | 7.65 | 5.66 |
| `QPixmap.fromImage(24 MP)` | 17.22 | 9.09 |
| `QImage.scaled(1280, Smooth)` — **16-bit mono** | **74.58** | 73.84 |
| `QImage.scaled(1280, Fast)` — 16-bit mono | 1.53 | 1.37 |
| `QImage.scaled(1280, Smooth)` — 8-bit mono | 28.61 | 28.21 |
| `QImage.scaled(1280, Smooth)` — RGB888 | 40.24 | 38.41 |
| numpy `arr >> 8` → uint8 (24 MP) | 26.59 | 25.54 |
| numpy 4×4 box bin (24 MP → 1500×1000) | 81.60 | 71.69 |
| **`cv2.resize(INTER_AREA)` 24 MP → 1280 (16-bit)** | **6.06** | 5.28 |
| `cv2.resize(INTER_LINEAR)` 24 MP → 1280 | 0.94 | 0.52 |
| `QPixmap.fromImage(QImage(1280 mono16))` | 0.65 | 0.43 |
| `setPixmap(1280) + repaint()` | 0.68 | 0.51 |
| `QImage.scaled` executed on a **worker thread** (thread-safety check) | 96.35 | 92.93 |

**The path to use (concrete):**

```
worker thread (never touches Qt widgets):
    frame: np.uint16 (h, w)                 # straight off the stacker
    target = widget_px * devicePixelRatio   # e.g. 1280x854 * 1.5
    small  = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)   # 6 ms
    img    = QImage(small.data, w, h, small.strides[0], Format_Grayscale16).copy()
    #                                ^ strided, and .copy() so numpy can GC
    signal.emit(img)                        # QImage is a reentrant value type → safe to hand over
GUI thread:
    pix = QPixmap.fromImage(img)            # 0.65 ms
    pix.setDevicePixelRatio(dpr)
    label.setPixmap(pix)                    # ~0.7 ms incl. repaint
```

Total GUI-thread cost ≈ **1.4 ms/frame**, i.e. 0.14 % of a 1 fps budget. Total worker cost ≈ 14 ms.
There is no reason to use a `QGraphicsView`, a `QOpenGLWidget`, or QML at this frame rate.

**The gotchas, all reproduced here:**

1. **`QImage` does not take ownership and does not copy.** Docs: *"The buffer must remain valid
   throughout the life of the QImage and all copies that have not been modified or otherwise detached
   from the original buffer"* [C]. Reproduced failure [M]: build a `QImage` view over a numpy array,
   `del` the array, `gc.collect()`, churn the heap — no exception, and `pixel(10, 10)` then returned
   **4293585642** for a `Grayscale16` image (i.e. > 65535: garbage). `.copy()` (7.65 ms) or keeping a
   reference is mandatory.
2. **Stride must be explicit and 32-bit aligned.** Docs: *"data must be 32-bit aligned, and each
   scanline of data in the image must also be 32-bit aligned"* [C]. 6000 px × 2 B = 12 000 B/line is
   fine; a 4001-px-wide mono8 frame (4001 B/line) is **not**, and you get a skewed image rather than an
   error. Always pass `arr.strides[0]`; never let Qt assume `width × depth/8`.
3. **Format matters more than people think.** `Format_Grayscale16` is what the stacker already
   produces — do not convert to RGB "for safety" (numpy `dstack` of 24 MP 8-bit = 72 MB, measured
   +40 ms in the scale step). If we ever go colour, `cv2` gives **BGR**, so use
   `QImage.Format_BGR888` and skip a channel swap entirely.
4. **`SmoothTransformation` on 16-bit is a trap**: 74.6 ms, 12× slower than `INTER_AREA` and 50×
   slower than `FastTransformation`. Qt's smooth scaler is only competitive for 8-bit RGB at small
   sizes. Nobody should call `QImage.scaled(Smooth)` on a raw 16-bit frame in the GUI thread.
5. `QImage.scaled()` is thread-safe and gives the same cost off the GUI thread (96 ms vs 75 ms — the
   difference is GIL/CPU contention, not a correctness problem) [M]. Docs agree that *"When using
   QPainter on a QImage, the painting can be performed in another thread than the current GUI
   thread"* [C]. **`QPixmap` is the thing with the affinity rule, not `QImage`.**

### 5.5 QLabel vs QGraphicsView vs QQuick

| Option | Verdict |
|---|---|
| `QLabel.setPixmap` + `setScaledContents(False)`, pre-scaled pixmap | **Recommended.** ~1 ms/frame measured. No zoom, no pan — but our preview is a *status display*, not an image editor. |
| `QLabel.setScaledContents(True)` with the full 24 MP pixmap | Wrong: pushes a 24 MP resample into `paintEvent` (17 ms `fromImage` + smooth scaling on every repaint, and repaints happen on every occlusion/DPI change). |
| `QGraphicsView` + `QGraphicsPixmapItem` | Only if we want wheel-zoom / drag-pan over the stack. Cost is comparable (it draws the pixmap with a smooth transform), ~50 lines more code, and it brings the classic scrollbar/anchoring fiddling. Add later if asked. |
| QML `QQuickImageProvider` | Slicker animations, but a second language (QML/JS) in the repo, the QML runtime (~14 MB `qml/`, currently *not* collected because we do not use it), and the image still has to be produced by the same numpy→QImage code. Not worth it for 6 controls and a picture. |

### 5.6 Threading: the canonical pattern, and what actually breaks

**The rule (docs):** *"QObject and all of its subclasses are not thread-safe. This includes the entire
event delivery system… If you are calling a function on a QObject subclass that doesn't live in the
current thread and the object might receive events, you must protect all access… with a mutex;
otherwise, you may experience crashes or other undesired behavior."* And, crucially: *"you can safely
emit signals from your QThread::run() implementation, because signal emission is thread-safe."* [C]

**What that means in practice for us:** use plain `threading.Thread` for camera/solver/stacker
(brief 05 §6 already measured that `ctypes` releases the GIL, so a capture thread blocked in EDSDK
does not starve the stacker). Do **not** subclass `QThread` — it buys nothing here and its
thread-affinity semantics are the documented source of confusion.

The contract, in one paragraph: workers own numpy arrays; on a new result a worker resamples and wraps
the result in an owning `QImage`, then emits `Signal(object)` on a `QObject` created in the GUI thread.
`Qt.AutoConnection` sees the emitting thread ≠ receiver thread and delivers **queued**, i.e. the slot
runs in the GUI thread's event loop. Nothing else crosses the boundary. Widgets are touched only in
slots. There is no lock anywhere, because ownership moves with the message.

Measured [M]: 200 worker→GUI emits with a 1280×854 `QImage` payload, GUI thread pumping events:
**200/200 delivered, median latency 0.62 ms, p99 1.00 ms.** Emission itself is a Python object handoff
(no pixel copy — `QImage` is implicitly shared with refcounting).

**What breaks if you get it wrong — measured, and it is worse than a crash:**

| Violation | Observed |
|---|---|
| `QLabel.setText` / `setPixmap` / `repaint` from a `threading.Thread` | **300/300 calls, no exception, no warning, process alive** [M] |
| `QPixmap.fromImage` from a worker thread (the classic Qt4/Qt5 abort) | **no error** [M] |
| `QImage` view over a numpy buffer that is GC'd | **silently wrong pixels** (4293585642 in a 16-bit channel) [M] |

Qt 6's raster backend on Windows no longer enforces thread affinity for `QPixmap`, so the old
`QPixmap: It is not safe to use pixmaps outside the GUI thread` abort does **not** save you. The failure
mode is repaint tearing, stale back-buffer contents, and — eventually — heap corruption inside the
style/backing-store code. Design for the rule; the framework will not tell you when you break it.

What *does* bite in Python specifically:

- **Exceptions in slots are swallowed** (printed by `sys.excepthook` if installed, otherwise lost). If a
  renderer bug raises in `on_frame`, the preview just stops updating and nothing else happens. Install
  an `excepthook` that appends to the log pane.
- **Unbounded queued signals are a memory leak.** At 1 fps it cannot happen; if the solver ever emits
  faster than the GUI can paint, coalesce in the worker ("latest frame wins", keep one pending
  `QImage`). This is the one place a lock is legitimate.
- **`QTimer` is a GUI-thread object.** A 1 s "is the camera alive?" ticker must live on a GUI-thread
  `QObject` (or in a worker thread with its own event loop — unnecessary).

### 5.7 Packaging reality: does it survive PyInstaller/Nuitka under a vendored pbs tree?

**Yes — measured end to end on exactly the planned runtime.** python-build-standalone 3.13.15 +
`uv pip install --target`-style layout, `PyInstaller 6.22.3 --onedir --console`:

| Result | Value |
|---|---|
| build time | 65 s [M] |
| dist size | 131.8 MB [M] |
| platform plugins collected | `qwindows.dll`, `qdirect2d.dll`, `qminimal.dll`, `qoffscreen.dll` [V] |
| style plugin collected | `styles/qmodernwindowsstyle.dll` [V] |
| MSVC runtime | `MSVCP140*.dll`, `VCRUNTIME140*.dll` bundled into the dist [V] → **no VC++ redistributable prerequisite on the user's machine** |
| frozen cold start | 627 ms wall / 396 ms to first paint [M] |
| `--onefile` | 50.5 MB exe, **2442 ms** wall [M] — disqualified (also weakens LGPL replaceability, §5.8) |

Known pain, in the order it will bite:

1. **`platforms/qwindows.dll` missing → hard abort** at startup ("could not find or load the Qt
   platform plugin windows"). PyInstaller's bundled PySide6 hook gets this right for onedir with the
   current hooks-contrib; treat the dist tree as a *tested artifact* (launch it in CI), not as a
   build output you trust.
2. **`styles/qmodernwindowsstyle.dll` missing → silent Fusion fallback** (§5.2). No error, just a
   wrong-looking app. Only a screenshot/`objectName()` assertion catches this.
3. **Translations / QML / Addons bloat.** A Widgets app collects neither `qml/` nor Addons [V].
   `translations/` (7 MB) and `opengl32sw.dll` (20.6 MB) are dead weight for us, and the file-size
   table in §4 says exactly what to delete. (Avoid `--exclude-module` games until something breaks;
   use a spec file with explicit `datas`/`binaries` trimming and re-run the smoke test.)
4. **Nuitka**: has first-class Qt/PySide6 handling [UNVERIFIED for the exact `--enable-plugin=pyside6` flag name in 4.3.1 —
   the manual page fetched here documents `--mode=standalone|onefile` but does not name the Qt plugin
   in the snippet we read]. Nuitka is *not needed*: it buys startup time we already have (396 ms) at
   the cost of a C compiler in the build and an AGPL tool. Revisit only if cold start regresses.
5. **pbs + PySide6 is a supported combination**: abi3 wheel, no build-time linking against a specific
   `python3.dll`, and our run imports and paints from the copied tree. Brief 05's relocatability result
   carries over unchanged — PySide6 lives entirely in `site-packages`, the interpreter knows nothing
   about Qt.
6. **One real risk we cannot test away**: EDSDK.dll (brief 05 §7) — proprietary, non-redistributable.
   That is orthogonal to the GUI, but it decides whether the installer is "double-click" or "point us
   at your Canon install", and it is the only packaging problem here that could actually block shipping.

### 5.8 LGPL-3 obligations when bundling dynamically-linked PySide6 in a closed app

Qt's own list of what LGPL requires [C], plus what it means for us:

| Obligation (Qt's wording) | Concrete action for ministar |
|---|---|
| *"Complete corresponding source code of the library used with the application… including all modifications… should be delivered with the application (or alternatively provide a written offer with instructions on how to get the source code). It should be noted that the complete corresponding source code has to be delivered even if the library has not been modified at all."* | Ship/link the **unmodified Qt 6.12.0 sources** (or a written offer pointing at `download.qt.io`), in the installer's `licenses\qt\` folder or on the website. Our own Python source stays closed. |
| *"In case of dynamic linking, it is possible, but not mandatory, to keep application source code proprietary as long as it is 'work that uses the library' — typically achieved via dynamic linking."* | We already do this: `Qt6Core/Gui/Widgets.dll` are separate files in the onedir tree. **Do not** switch to a static Qt build, and do not Nuitka-freeze Qt into one binary. |
| Re-linking mechanism / "suitable shared library mechanism" (§4(d)(1)) | onedir keeps Qt as replaceable DLLs a user can swap → compliant. `--onefile` (extract to `%TEMP%`) makes replacement impractical → another reason onedir wins. |
| *"Provide a license copy & explicitly acknowledge Qt use"* | `THIRD-PARTY-NOTICES.txt` (Qt 6.12.0, LGPL-3.0, link to sources) + full LGPLv3 text + an About box line: "Uses Qt 6.12 (LGPL-3.0) and PySide6 6.12 (LGPL-3.0)". |
| Modifications to Qt must stay open; DRM/anti-tivoization terms accepted [C] | We modify nothing. If we ever patch Qt we must publish the patch. |
| PySide6's own bundled third-party licences | `doc.qt.io/qtforpython-6/licenses.html` lists them; copy that page into `licenses\` [C]. |

**Cost of compliance: an afternoon of packaging chores and one legal read.** The commercial licence
exists if that afternoon is more expensive than a seat licence [U on price].

### 5.9 Honest costs/risks of Qt

- **218.7 MB installed / 132 MB shipped** — the biggest single number in this document. It is the price
  of the only toolkit here that is simultaneously native-looking, 24 MP-capable, and LGPL-clean.
- **PySide6 API surface is huge** and the docs are C++-first; every Python question about Qt has a C++
  answer one translation away. (`QLabel`/`QSlider`/`QPlainTextEdit`/`QGroupBox` is ~1 % of it.)
- **Binding-upgrade friction is low but real**: PySide6 majors track Qt minors every ~6 months; pin
  `PySide6==6.12.*` and rely on the LTS branch (5 years of patches [C]).
- **Debugging cross-thread issues is unforgiving** precisely because it is silent (§5.6). Mitigation:
  one `Bridge(QObject)` with slots and a rule that no other module imports `QtWidgets` except the view
  layer. Enforce with a lint rule, not a code review.

---

## 6. Option 2 — Tkinter / ttk (runner-up)

### 6.1 Is it genuinely acceptable-looking on Windows in 2026?

**Yes, conditionally — and the condition is `sv-ttk`, not vanilla ttk.**

- Vanilla `ttk` on Windows uses the native `vista`/`xpnative` theme for standard controls, so a plain
  ttk app looks *native but 2010*: no rounded corners, no fluent spacing, no dark mode (the Tk window
  chrome, menus and any non-ttk widget stay light no matter what).
- **sv-ttk 2.6.1** (*"A gorgeous theme for Tkinter, based on Windows 11's UI"*, MIT, Tk 9 compatible
  [C]) is the closest thing to "it is a Windows 11 app": rounded entry boxes, dark/light, accent-ish
  buttons, correct scrollbars. It is a `.tcl` theme file, 0.1 MB, **+10 ms import** [M].
- **ttkbootstrap 2.2.4** gives 30 curated themes and semantic `bootstyle` keywords [C] at a **+274 ms
  import cost** [M]. For a 6-control app that is 0.27 s of every launch for cosmetics we would mostly
  override anyway. Verdict: use sv-ttk, not ttkbootstrap, if Tk is chosen.
- **customtkinter 6.0.0**: fully custom-drawn widgets (so it is not native, it is a *look*), 13.6k
  stars, but its PyPI metadata declares the licence as **CC0** while the repo says MIT [V] — sloppy
  metadata on a widget toolkit is a maintenance signal. Also it draws its own controls, so we inherit
  its bugs at 150 % DPI. Not recommended.

Remaining Tk look gaps, in order of visibility: **(a)** no dark title bar / menu bar without the
`DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)` ctypes hack [UNVERIFIED here — not tested]; **(b)** Tk's
text widget log is fine but every custom visual (the nudge indicator) must be drawn on a `Canvas` by
hand; **(c)** font rendering is GDI, so labels look slightly older than DirectWrite text in Qt at the
same size. Screenshot comparison for Tk was impossible in this pass (the Windows session was locked, so
`ImageGrab` captured the lock screen) — the Qt screenshot came from `QWidget.grab()`, which renders
without a visible desktop.

### 6.2 Image display for a 24 MP frame at 1 fps

- The only supported paths are `tkinter.PhotoImage` (Tk 8.6: **8-bit per channel, PPM/PGM/GIF/PNG
  only**) and `PIL.ImageTk.PhotoImage` (which converts a PIL image to a Tk photo image via
  `-data`/PPM bytes). There is **no 16-bit path** — a 16-bit stack must be tone-mapped to 8-bit in
  numpy first.
- Measured [M], 24 MP uint16 → 8-bit → 1280×854:
  `PIL.Image.fromarray((frame >> 8).astype(uint8)).resize(1280, LANCZOS)` + `ImageTk.PhotoImage`
  = **98 ms** (Qt+OpenCV equivalent: 6 ms + 0.65 ms). LANCZOS is the expensive part; `BILINEAR`
  or `Image.reduce(4)` is much cheaper [U for exact figures — not isolated in this pass].
- **Redraw cost at 1 fps is a non-issue.** Tk's canvas/label blit of a 1280×854 photo image measured
  as noise in the frame loop; the frame budget is dominated by the PIL resize, which belongs in the
  worker thread anyway (PIL releases the GIL during `resize`).
- **The real Tk limitation is on-demand scaling.** `PhotoImage.zoom()/subsample()` are integer-factor
  nearest-neighbour only, and Tk cannot smoothly rescale a photo image to an arbitrary widget size.
  Window resize therefore means "re-run PIL at the new size" (98 ms, visible hitch) or "accept
  letterboxing". Qt does it in `paintEvent` for free.
- **Must keep a reference** to every `PhotoImage` (`self._photo = photo`) or Tk garbage-collects the
  pixels and you get a blank label — the Tk analogue of the `QImage` lifetime bug, and just as silent.

### 6.3 Threading

Tk is **not thread-safe at all** and is stricter than Qt about it: calling Tk from a non-main thread
raises `RuntimeError: main thread is not in main loop` (when Tcl is built with threads, which pbs's
build is) or corrupts the interpreter. The canonical pattern is a `queue.Queue` written by workers and
drained by a `root.after(50, pump)` callback on the GUI thread — i.e. **the same architecture as Qt,
with a hand-rolled event queue instead of a queued signal**. It works fine; it is ~15 lines of
boilerplate that Qt gives you for free, with worse error reporting.

### 6.4 Packaging

Measured: **73.3 MB** frozen onedir, 20 s build, **450 ms wall / 304 ms to first paint** [M]. No
plugins, no style DLLs, no MSVC runtime question beyond what PyInstaller already copies. Tcl/Tk lives
inside the pbs tree already (brief 05 §1.3 lists `tcl/`, `tcl86t.dll`, `tk86t.dll` — **[V]**), so
"Tk is free in a vendored tree" is literally true. Zero licence friction (Tcl/Tk is BSD-style).

### 6.5 When I would choose Tk over Qt

If any two of these became true: the preview could be 8-bit-only and never resized smoothly; the UI
grew no custom-painted widget beyond a canvas arrow; we needed to shave 60 MB or 150 ms; or the
shipping contract forbade *any* LGPL text in the installer. None are true today.

---

## 7. Option 3 — wxPython

- **Native widgets, genuinely**: *"With wxPython software developers can create truly native user
  interfaces for their Python applications"* [C]. It wraps wxWidgets 3.3.4 [M: `wx.version()` →
  `4.3.2 msw (phoenix) wxWidgets 3.3.4`], so controls are Win32 controls; dark mode support in
  wxWidgets 3.3 is partial and lags what Qt 6.12 does with `windows11`.
- **Maintenance**: repo is alive (last commit 2026-10-20, `4.3.3` alpha snapshots nightly) but it is a
  small team, **602 open issues**, and the stable line moves about once a quarter [V]. It is not dying,
  it is *slow*.
- **Cost**: 66 MB Windows wheel, **60.9 MB installed** [V/M], import 112.7 ms [M], cold start 615 ms
  wall [M]. So it costs ~as much as Qt (61 vs 77 MB frozen contribution) while giving a *Windows 10*
  look, not a Windows 11 one.
- **Image path**: `wx.Bitmap.FromBuffer` needs an 8-bit contiguous buffer; scaling is
  `wx.Image.Scale(..., wx.IMAGE_QUALITY_HIGH)` (measured as part of the 226 ms bitmap build in §3 —
  slower than `cv2`, and again the answer is "resample in the worker with OpenCV"). `wx.StaticBitmap`
  blits cheaply. No 16-bit display path.
- **Verdict: no.** It is the answer for "I need real Win32 controls and I do not want LGPL", and
  neither clause is worth 60 MB of install for a worse-looking result.

---

## 8. Option 4 — Dear PyGui

- **Great at what it is for**: GPU-immediate-mode, and a live image is a texture upload. 15.7k stars,
  MIT, 2.3.1 (2026-05-02), active [V].
- **It does not look native, and it does not look "professional" either** — it looks like Dear ImGui,
  i.e. like a game's debug overlay. For a consumer astro tool the first impression is "beta tool from
  GitHub". No UIA/accessibility tree, custom font loading required to escape the default ImGui face.
- **The 24 MP problem is architectural, and worse than expected**: DPG 2.3.1 exposes only
  `mvFormat_Float_rgb` and `mvFormat_Float_rgba` [V: enumerated `dir(dearpygui)`]. A texture is
  **4 bytes/channel**: a full 24 MP RGBA frame would be **384 MB**, and even the display-size
  1500×1000 texture is 24 MB plus a float32 conversion of the frame in numpy [UNVERIFIED — conversion
  cost — not measured]. There is no 16-bit greyscale texture path, so the stacker's data has to be
  crushed to float RGBA every frame.
- **Maturity signal, measured**: `dpg.get_dearpygui_version()` **hard-crashed the interpreter**
  (`0xC0000005`, access violation) when called before `create_context()` [M]. Not fatal for the
  framework's reputation (it is a doc bug) but it is the kind of edge a shipping app cannot afford.
- **Text input/IME**: DPG draws its own inputs. IME support for Chinese/Japanese/Korean on Windows was
  added around v0.6.x (issues #447, #452, #689, all closed) [V via GitHub API], i.e. it exists; but
  text selection, clipboard, and caret behaviour in a custom-drawn input are historically the weakest
  part of any ImGui binding [UNVERIFIED for 2.3.1 specifically — not tested here].
- **Verdict: no** for the shipping UI. It is the right tool for a *diagnostic* window (star profile,
  histogram, live frame-rate graph) later, if ever.

---

## 9. Option 5 — the webview family (Flet, NiceGUI, pywebview, Electron-like)

The common shape: Python produces pixels → something serialises them → a browser engine renders them.
For a 24 MP image at 1 fps that means base64/PNG or a raw socket frame every second, plus a browser
engine in the installer.

| Option | Architecture (verified) | Bundle / cold start | Verdict |
|---|---|---|---|
| **Flet 1.0.5** | Python backend + **Flutter** client (`flet-desktop`), WebSocket between them [C: docs.flet.dev] | Flutter runtime is tens of MB inside `flet-desktop` (UNVERIFIED) [U]; import chain is heavy | No. Looks pretty, but we would be shipping a Flutter engine to draw one image, and every UI change crosses a socket. |
| **NiceGUI 3.18.0** | FastAPI + uvicorn **local HTTP server**, UI in a browser or wrapped webview [C] | 21.2 MB wheel + FastAPI/uvicorn; opens a listening port | No. A local HTTP server in a desktop app means firewall prompts, port collisions, and a security surface (see the classic "web UI on localhost" CVE family) — for zero benefit offline. |
| **pywebview 6.2.1** | WebView2 (Edge) window + JS bridge, UI in HTML/CSS/JS | ~0.5 MB wheel + depends on the WebView2 runtime (preinstalled on Win11, installable on Win10) (UNVERIFIED) [U] | No. Smallest of the three, but the UI is JS: we would write the nudge panel and preview in HTML/Canvas and pass frames over a string bridge. |
| **Electron-like (pywebview + Node, Tauri-likes)** | | 100–200 MB [U] | No. |

**The disqualifier is the same in all four cases and it is not size:** the image has to leave the
process that computed it and cross a text/binary bridge at 1 fps, then be re-decoded by a browser; and
the "offline" requirement means we must own the whole runtime anyway, so we get the *worst* of both
worlds — a bundled browser *and* a Python runtime. For a tool whose entire value is a live image plus a
few numbers, the native toolkit is strictly less machinery. **Nobody in this domain ships one (§12).**

---

## 10. Option 6 — Kivy (verdict only)

Kivy is a custom OpenGL UI toolkit designed for touch/mobile; on the desktop it looks like a phone app
and it brings SDL2 + its own text rendering + its own DPI quirks. Latest **PyPI release 2.3.1,
2024-12-26** [V] — ~22 months without a release while the repo stays active (commits 2026-10-21) [V].
For a Windows-only desktop tool it offers nothing Qt does not do better, and it puts a stalled release
cadence in the critical path. **No.**

---

## 11. Also-ran: GTK4 / PyGObject — viable on Windows? No.

- GTK itself builds on Windows; the **official** install path is MSYS2: *"This method is based on the
  packages provided by MSYS2… `pacman -S mingw-w64-ucrt-x86_64-gtk4`"* [C].
- There are **no Windows wheels for PyGObject on PyPI** — 3.58.1 publishes a single `sdist` [V]. So a
  vendored pbs tree cannot `pip install` GTK; you would have to hand-assemble GTK4 + GLib + Pango +
  GdkPixbuf + their DLLs and the GIRepository/typelib layout into the bundle, then keep it working
  across GTK minors. That is a full-time packaging project.
- This is exactly why **Siril ships as a compiled C app against MSYS2's UCRT64 GTK4** (brief §12) and
  not as a Python app — they *are* the GTK-on-Windows integration.
- **Verdict: no.** Excellent toolkit, wrong packaging story for a vendored Python tree.

---

## 12. What the tools our users already know actually do

| Tool | Stack | Evidence | What it tells us |
|---|---|---|---|
| **N.I.N.A.** | C# / .NET / **WPF** on Windows | GitHub topics include `wpf`, `dotnet`, `csharp`; repo pushed 2026-10-24 [V] | A rich Windows-only astro app, native toolkit, custom dark theme. WPF is the .NET analogue of Qt Widgets: retained-mode, chromeless dark styling, `Image` element for the preview. There is no Python equivalent of WPF; Qt Widgets is the closest thing available to us. |
| **SharpCap** | .NET / WinForms, closed source | [C] vendor site; **UNVERIFIED** for the exact UI framework version in the current release | Even the most "utilitarian" tool in the hobby is a native desktop toolkit, and it shows: dense, fast, no browser. |
| **Siril** | C + **GTK4** (`meson.build`: `gui_subdir = 'gui-gtk4'`, `USE_GTK4`) [V], MSYS2 UCRT64 Windows builds [C] | | Two of the three reference apps use GTK (Siril) or WPF (NINA) — both *native, retained-mode, styled* toolkits, both with custom dark themes drawn by the app. **Nobody uses Electron/webview for the live image.** The pattern in this niche is: native toolkit + custom dark skin + the image blitted straight into a widget. That is precisely the PySide6 recipe in §5. |
| Qt-based astro (KStars/Ekos, PHD2's GUI) | Qt (KStars: Qt/KDE) | UNVERIFIED for current versions | Qt is the only toolkit in the list that is *already* the de-facto choice for cross-platform astro software. |

**Relevance to our 24 MP path:** Siril and NINA both debayer/stack at full precision on CPU and then
hand a *display-ready* 8/16-bit buffer to the widget (Siril keeps a float32 working image and computes
a display LUT/extent per frame; NINA's WPF `Image` gets a `BitmapSource`). Neither hands the GUI thread
a 24 MP resample. Our §5.4 pipeline is the same idea with Qt types.

---

## 13. Decision matrix

| Criterion (weight) | PySide6 6.12 | Tk + sv-ttk | wxPython | Dear PyGui | Flet/NiceGUI/pywebview | Kivy |
|---|---|---|---|---|---|---|
| Looks native on Win11 (high) | **native `windows11` style, dark + accent [M]** | themed; light chrome remains | native but Win10-era | no (ImGui) | no (own engine) | no |
| 24 MP @ 1 fps path (high) | **6 ms resample + 0.65 ms upload [M]** | 98 ms PIL (worker) + 8-bit only [M] | ~worker resample + 8-bit | float32 RGBA only → 384 MB worst case [V] | bridge + browser decode | GL texture, ok-ish |
| Threading contract (high) | **queued signals, 0.62 ms, 200/200 [M]** | manual `after()` pump | `wx.CallAfter` | manual | socket events | Clock scheduling |
| Cold start, frozen (med) | 627 ms / 396 ms paint [M] | **450 / 304 ms [M]** | 615 / 502 ms [M] | n/a (crashed) | ≥1 s (UNVERIFIED) | ≥1 s (UNVERIFIED) |
| Bundle, frozen onedir (med) | 131.8 MB [M] | **73.3 MB [M]** | ~105 MB (UNVERIFIED) | ~80 MB (UNVERIFIED) | 150 MB+ (UNVERIFIED) | ~130 MB (UNVERIFIED) |
| Licence for closed app | LGPL-3, 4 chores [C] | **BSD-style, none** | wxWindows (LGPL+exception) | MIT | MIT/Apache | MIT |
| Text input / IME / a11y (med) | **full native** | native-ish | native | weak [U] | browser | weak |
| Maintenance risk (med) | **LTS, 5 yr [C]** | Tcl/Tk stable, sv-ttk 1 maintainer | 1 small team, 602 issues | active, young | very active | **no release in 22 mo [V]** |
| Dev ergonomics for *this* UI (low) | good (huge API, C++ docs) | very simple | medium | medium | JS/HTML split | poor on desktop |

---

## 14. If we go PySide6: the concrete plan

**Module layout** (enforces §5.6 by construction):

```
ministar/
  ui/            # the ONLY package allowed to import PySide6.QtWidgets
    app.py         # QApplication, main window, Bridge(QObject) with @Slot methods
    preview.py     # QLabel + setPixmap; owns dpr-aware target size
    panels.py      # solve status, nudge indicator (custom paintEvent), controls, log
  core/
    camera.py      # threading.Thread; ctypes/EDSDK (brief 02/08)
    solve.py       # ASTAP subprocess loop (brief 01)
    stack.py       # numpy registration + incremental stack (briefs 03/06)
  bus.py           # Bridge signals: frame_ready(object), solve_done(dict), log(str), nudge(dict)
```

- `bus.py` holds the `QObject` emitted from workers; `ui/app.py` connects it to slots. Workers import
  `bus`, never `ui`.
- Worker → GUI payload is always **already display-sized** (§5.4) and **already owning** its memory
  (`.copy()`), so no lifetime coupling exists between the stacker's buffers and Qt.
- Nudge indicator: a `QWidget` subclass painting two arcs/arrow glyphs in `paintEvent` + two large
  numeric labels — 40 lines, no toolkit fight, and it is the one place where "custom look" is wanted.
- Log: `QPlainTextEdit` with `setMaximumBlockCount(5000)` (bounded memory) and append-only slots.
- Theme: nothing. Inherit `windows11` + system scheme; add a small QSS block only for the
  monospace log font and the accent colour used by the nudge arrows.

**Per-frame budget (measured inputs):** camera/USB 0.4–22 ms (brief 05), stacking numpy, then
**14 ms worker + 1.4 ms GUI**. At 1 fps the headroom is 98 %. The tool is not GUI-bound, which is the
point of picking a toolkit whose numbers we already know.

**Packaging checklist:** pbs 3.13 + `PySide6-Essentials==6.12.*` (never the full `PySide6` metapackage,
which pulls 40 MB of Addons) + numpy + opencv-headless → PyInstaller `--onedir --windowed` → smoke test
the *frozen* tree for: `platforms/qwindows.dll`, `styles/qmodernwindowsstyle.dll`,
`QApplication.style().objectName() == "windows11"`, first paint < 1 s, 24 MP render, exit code 0.
`licenses\` folder per §5.8. No `--onefile`.

---

## 15. Open questions (things this pass did **not** settle)

1. **Dark title bar**: whether Qt 6.12 paints the caption bar dark automatically (or needs
   `DWMWA_USE_IMMERSIVE_DARK_MODE` via ctypes). Our screenshot came from `QWidget.grab()`, which
   excludes the window frame. **UNVERIFIED.** Same question, worse answer for Tk (**UNVERIFIED**, known
   to need the DWM attribute hack).
2. **On-screen present cost** (DWM composition, `swapBuffers`) — the session was locked; only raster
   costs are trustworthy. Irrelevant at 1 fps, but do not quote our `setPixmap+repaint` numbers as
   end-to-end frame latency.
3. **Nuitka + pbs + PySide6**: not built here. If cold start ever matters more than 396 ms, benchmark
   `--mode=standalone` before believing it helps (Qt DLL load dominates, and Nuitka does not change it).
4. **EDSDK redistribution** (brief 05 §7) remains the only true blocker, unrelated to the GUI.
5. **wxPython dark mode** under wxWidgets 3.3: not evaluated; moot given the verdict.
6. **IME/accessibility** for DPG 2.3.1 and Flet: not tested (we have no IME here); verdicts rest on
   architecture and issue history.
7. **PySide6 6.12 + Addons-free `--windowed`** build: only the `--console` build was timed. `--windowed`
   changes the entry point slightly (no console attach) and typically saves a few ms [U].

---

## 16. Sources

Qt / PySide6
- Qt 6.12 LTS release, 2026-09-30 — https://www.qt.io/blog/qt-6.12-released ("6.12 is a Long-Term Support (Qt LTS) release, meaning it gets maintenance over a 5-year period")
- Qt for Python 6.12 release, 2026-10-09 — https://www.qt.io/blog/qt-for-python-release-6.12-is-here
- Qt licensing (6.12) — https://doc.qt.io/qt-6/licensing.html
- LGPL/GPL obligations — https://www.qt.io/development/open-source-lgpl-obligations
- Qt for Python: licences in use — https://doc.qt.io/qtforpython-6/licenses.html
- Qt for Python: commercial use — https://doc.qt.io/qtforpython-6/commercial/index.html
- Styles and style-aware widgets (`windows11` style plugin) — https://doc.qt.io/qt-6/style-reference.html
- Dark mode on Windows 11 with Qt 6.5 (and why the native style cannot be dark) — https://www.qt.io/blog/dark-mode-on-windows-11-with-qt-6.5
- Qt mailing list, "Qt 6.5 NOT supporting Dark mode on Windows" — https://lists.qt-project.org/pipermail/interest/2023-June/039127.html
- `QImage` (buffer lifetime, 32-bit alignment, thread-safe painting) — https://doc.qt.io/qt-6/qimage.html · Python API: https://doc.qt.io/qtforpython-6/PySide6/QtGui/QImage.html
- `QPixmap` — https://doc.qt.io/qt-6/qpixmap.html
- Threads and QObjects (thread affinity, thread-safe emission) — https://doc.qt.io/qt-6/threads-qobject.html
- High DPI (Per-Monitor DPI Aware V2, scaling env vars) — https://doc.qt.io/qt-6/highdpi.html
- PySide6 wheel metadata (versions, licences, abi3) — https://pypi.org/pypi/PySide6/6.12.0/json , https://pypi.org/pypi/PySide6-Essentials/6.12.0/json
- PyQt6 (GPLv3/commercial) — https://pypi.org/pypi/PyQt6/json , https://riverbankcomputing.com/software/pyqt/

Tk / wx / DPG / webview / Kivy / GTK
- sv-ttk — https://pypi.org/project/sv-ttk/2.6.1/ , https://github.com/rdbende/Sun-Valley-ttk-theme/releases
- ttkbootstrap (2.x, 30 themes, bootstyle) — https://pypi.org/project/ttkbootstrap/ , https://github.com/israel-dryer/ttkbootstrap
- customtkinter 6.0.0 — https://pypi.org/pypi/customtkinter/json
- wxPython ("truly native user interfaces"), 4.3.2, wxWidgets 3.3.4 — https://www.wxpython.org/ , https://github.com/wxWidgets/Phoenix
- wxWindows Library Licence — https://wxwidgets.org/about/licence/
- Dear PyGui 2.3.1 — https://github.com/hoffstadt/DearPyGui/releases ; IME issues #447/#452/#689
- Flet 1.0.5 / docs — https://docs.flet.dev/ , https://github.com/flet-dev/flet
- NiceGUI 3.18.0 — https://pypi.org/project/nicegui/ , https://nicegui.io
- pywebview 6.2.1 — https://github.com/r0x0r/pywebview
- Kivy (last release 2024-12-26) — https://pypi.org/pypi/kivy/json , https://github.com/kivy/kivy
- PyGObject on PyPI (sdist only) — https://pypi.org/pypi/pygobject/json
- GTK on Windows = MSYS2 — https://www.gtk.org/docs/installations/windows/

Packaging
- PyInstaller 6.22.3 usage (`--onedir` vs `--onefile`, contents directory) — https://pyinstaller.org/en/stable/usage.html
- Nuitka 4.3.1 user manual — https://nuitka.net/user-documentation/user-manual.html
- python-build-standalone — https://github.com/astral-sh/python-build-standalone

Domain
- N.I.N.A. (C#/.NET/WPF) — https://github.com/isbeorn/nina
- Siril (C + GTK4) — https://gitlab.com/free-astro/siril/-/raw/master/meson.build , https://siril.org/download/
- SharpCap — https://www.sharpcap.co.uk/
