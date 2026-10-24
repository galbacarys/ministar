# 05 — Python on Windows: interpreter, packaging, and the `pyedsdk` decision

**Scope.** How to get a trustworthy Python on this Windows machine, how to ship it, and whether
`pyedsdk` can carry the capture path. Everything called *verified* below was reproduced on this
machine on 2026-10-24 from the WSL2 side, driving real Windows binaries.

**Labelling convention** (consistent with briefs 00–04)

| Tag | Meaning |
|---|---|
| **[M]** | **Measured** on this machine in this pass. Timings are medians of 3–20 runs on the native Windows side, parent process = Windows Python (i.e. no WSL launch overhead in the numbers unless noted). |
| **[V]** | **Verified** fact — file inspected, command output, or source read. Not a benchmark. |
| **[C]** | **Cited** from upstream docs/source, URL given in §8. |
| **[U]** | **UNVERIFIED** — plausible, untested here. |

Machine: Windows 10.0.26200.9457, AMD Ryzen AI 7 350 (16 threads), 15 GB RAM.

---

## 0. Executive summary

1. **The premise "no working Python on Windows" is out of date — and wrong in an important way.** `uv`
   is already installed (`uv 0.12.6`, `C:\Users\username\AppData\Local\hermes\bin\uv.exe`) and it has
   **already provisioned a working 64-bit CPython 3.11.16** at
   `C:\Users\username\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none`. **[V]** What is
   missing is only *PATH wiring*: `where python` resolves to the 2-byte Microsoft Store reparse stub
   in `%LOCALAPPDATA%\Microsoft\WindowsApps`, and `py`/`dotnet` do not exist. Brief 00 §9 needs a
   correction: the interpreter is present, the alias is a decoy.
2. **The 43 ms spawn budget is already blown before any of our code runs.** A bare
   `python -c pass` costs **59–62 ms [M]**; `python -c "import numpy"` costs **231–243 ms [M]**. There
   is no packaging trick that puts a Python process spawn under 43 ms. **A resident daemon is not an
   optimisation, it is the only design that fits** — confirming brief 00 §8 from the other direction.
3. **Ship a vendored python-build-standalone tree, not PyInstaller.** A copied CPython tree is
   **provably relocatable**: moved to a fresh directory, launched from `C:\Windows` with *no* venv, no
   `PYTHONPATH`, no registry, no env vars, it resolved `sys.prefix` correctly and imported
   `numpy`+`cv2` (**234 MB [M]**). Launch cost **50–60 ms bare / 230–240 ms with numpy+cv2 [M]**.
   PyInstaller `--onefile` costs **2364 ms [M]** and is disqualified outright.
4. **`pyedsdk` cannot do bulb exposures and cannot do exposures over 15 s.** Verified by reading
   v0.2.16's source: `_CameraCommand` binds **2 of 15** commands (`_TakePicture`,
   `_PressShutterButton`) — there is **no `BulbStart`/`BulbEnd`** anywhere in the package, and
   `Camera.shot()` blocks on `_waitForEvent(event, timeout=15)` which raises `TimeoutError` after
   15 s. For an astrophotography tool this is not a rough edge, it is disqualifying. **Write our own
   `ctypes` binding** (§4.6).
5. **The feared numpy↔EDSDK copy overhead does not exist.** `ctypes.string_at` of a 2 MiB live-view
   buffer costs **0.42 ms [M]**; `np.frombuffer` costs **0.46 ms [M]**. Buffer copying is noise. The
   real per-frame costs are **JPEG decode: 6.3 ms @ 1024×680, 22.3 ms @ 1920×1280 [M]** and the
   camera's own cadence.
6. **The GIL is a much smaller problem than expected — verified.** `ctypes.windll` (which is what
   `pyedsdk` uses, and what we would use) **releases the GIL for the duration of the foreign call**:
   a Python counter in another thread kept running at **0.94×** its idle rate while a 200 ms
   `kernel32.Sleep` was in flight via `windll` [M]. So a capture thread blocked in EDSDK does *not*
   starve a stacking thread. The GIL does bite pure-Python per-frame bookkeeping (**1.84×**
   serialisation measured [M]), which is avoidable by keeping per-frame work in numpy/cv2.
7. **`EDSDK.dll` is the hardest packaging constraint, not Python.** It is proprietary Canon and is
   **not redistributable** without Canon's written permission (this is exactly why `pyedsdk` ships
   without it) [C]. CPython (PSF-2.0) and python-build-standalone (MPL-2.0 tooling) are both fine to
   bundle. So the installer either requires the user to supply EDSDK, or we obtain a redistribution
   grant. **Resolve this before choosing an installer technology.**

---

## 1. Getting a reliable Python on Windows

### 1.0 What is actually on this machine [V]

```
$ cmd /c "where python"
C:\Users\username\AppData\Local\Microsoft\WindowsApps\python.exe
$ stat -c '%s bytes' …/WindowsApps/python.exe
2 bytes                       # first two bytes are "MZ"; it is a reparse-point stub, not an exe
$ cmd /c "python --version"
Python was not found; run without arguments to install from the Microsoft Store, or disable
this shortcut from Settings > Apps > Advanced app settings > App execution aliases.
$ cmd /c "where py"      -> INFO: Could not find files for the given pattern(s).
$ cmd /c "where dotnet"  -> INFO: Could not find files for the given pattern(s).
$ cmd /c "where uv"
C:\Users\username\AppData\Local\hermes\bin\uv.exe
$ uv --version
uv 0.12.6 (7938ca5d5 2026-08-25 x86_64-pc-windows-msvc)
$ uv python list --only-installed
cpython-3.11.16-windows-x86_64-none  C:\Users\username\AppData\Roaming\uv\python\cpython-3.11.16-…\python.exe
$ <that python> -c "import sys,platform; print(sys.version); print(platform.architecture())"
3.11.16 (main, Aug 14 2026, 15:34:35) [MSC v.1944 64 bit (AMD64)]
('64bit', 'WindowsPE')
```

**Reading.** The Store placeholder is a **2-byte file** that shadows everything on `PATH`. That is
the whole of the "no Python" problem. Disabling it is a two-click operation
(*Settings → Apps → Advanced app settings → App execution aliases*, untick `python.exe` and
`python3.exe`) or `Remove-AppxPackage` on the *App Installer* stub owner [C]. **Nothing needs to be
installed for a Python to exist** — `uv` already owns a 64-bit interpreter outside `PATH`.

Consequence for the dev loop: invoke it by absolute path, or run `uv run`/`uv venv` from WSL2 via
`cmd.exe`, and the alias never enters the picture. Do **not** rely on `python` resolving.

### 1.1 python.org: the *Python install manager* has replaced the classic installer [C]

This is a 2025/2026 change that most documentation online has not caught up with. The CPython team's
recommended Windows path is now the **Python install manager** (`py` / `pymanager`, shipped as an
**MSIX**, installable from the Store or `python.org/downloads`):

- `python`, `py`, `pymanager` become globally available; `py list` shows installed runtimes. [C]
- **It cannot install per-machine**: *"The Python install manager does not support installing runtimes
  per-machine. To emulate a per-machine install, you can use `py install --target=<shared location>`
  as administrator…"* and *"Runtimes installed by the MSI are shared with those installed by the MSIX,
  and are all per-user only."* [C]
- Runtimes land per-user; aliases such as `python3.15.exe` are created as *global aliases*, and
  `py install --refresh` recreates them (`--refresh` also rebuilds Start-menu shortcuts and registry
  keys). [C]
- Free-threaded builds are installable with a `t` suffix: `py install 3.15t`. [C]
- The legacy full installer's per-user default was `%LocalAppData%\Programs\Python\PythonXY`; that
  installer is now documented under a deprecated section. [C]

**Alias conflict, precisely.** The doc's troubleshooting advice is to *"open 'Manage app execution
aliases', and check that the aliases for 'Python (default)' are enabled. If they already are, try
disabling and re-enabling to refresh the command"*, and to ensure `PATH` contains
`%UserProfile%\AppData\Local\Microsoft\WindowsApps` [C]. Note the subtlety: with the install manager,
`WindowsApps` is *supposed* to be on `PATH` — the alias is a legitimate redirect to a real runtime.
On **this** machine it is a redirect to nothing, which is why the error text appears.

**Verdict for dev:** acceptable but redundant — it is a second interpreter manager competing with the
`uv` that is already installed, it requires interaction with the Store/MSIX stack, and it gives us
nothing `uv` does not. **Verdict for shipping: irrelevant** — we vendor an interpreter instead (§1.3).

### 1.2 `uv` — and yes, `uv python install` genuinely works on Windows

- `uv` downloads **python-build-standalone** CPython: *"Python does not publish official distributable
  binaries. As such, uv uses distributions from the Astral python-build-standalone project."* [C]
- **Verified on this machine**: a `uv`-managed `cpython-3.11.16-windows-x86_64-none` exists, is 64-bit
  (`AMD64`), and runs. **[V]**
- `uv python install 3.12.3` pins an exact version; `uv python install 3.12` takes the latest patch.
  `uv python pin <ver>` writes `.python-version`; `uv python pin --global` writes it to the user config
  dir; uv searches upward for `.python-version`. [C]
- Managed installs live under `uv python dir` → `C:\Users\username\AppData\Roaming\uv\python` **[V]**.
  They are **distinct from** system Pythons; uv calls anything it did not install (including
  pyenv-managed ones) a *system* Python. [C]
- uv-managed trees are marked **externally managed**. Verified failure mode: pointing
  `uv pip install` at the managed interpreter directly gives *"The interpreter at … is externally
  managed … should not be modified"* **[V]** — you must go through a venv (or `--target`).
- **Relocatable?** The *managed tree* is (see §1.3, proven). A **venv is not**: its `pyvenv.cfg`
  contains an absolute `home = C:\Users\username\AppData\Roaming\uv\python\cpython-3.11-…` **[V]**.
  Never ship a venv; ship the base tree.
- `.venv` size here: **45 MB** for CPython 3.11.16 + numpy **[M]**.

**Verdict: yes, for the developer machine.** It is present, it works, it pins, it locks
(`uv.lock`), and it avoids the Store alias entirely. One caveat: **numpy 2.5.4 ships no `cp311`
wheel** [C/V from PyPI metadata] — 3.11 caps us at numpy 2.4.6 **[M]**. Pin the dev interpreter to
**3.13**, not 3.11, so we get current wheels and stay a couple of years off EOL.

### 1.3 python-build-standalone (astral-sh) — the right thing to vendor

Formerly `indygreg/python-build-standalone`; the `indygreg` repo now 404s and the project is
**`astral-sh/python-build-standalone`** (4520★, active, last push 2026-10-23) **[V]**. Release
`20261021` publishes 991 assets; Windows x86-64 `install_only` archives **[C]**:

| Archive | size |
|---|---|
| `cpython-3.13.17+20261021-x86_64-pc-windows-msvc-install_only.tar.gz` | **45 MB** |
| `…-install_only_stripped.tar.gz` | **20 MB** |
| `cpython-3.12.15…install_only.tar.gz` | 44 MB |
| `cpython-3.14.9…install_only.tar.gz` | 47 MB |
| `cpython-3.13.17…**freethreaded**-install_only.tar.gz` | 46 MB |

Design intent, quoted: *"self-contained, highly portable, high-performance Python distributions …
most extension modules from the Python standard library are present, and their library dependencies
are either distributed with the distribution or are statically linked"* — built to *"minimize
run-time dependencies"*, with PGO/LTO *"while preserving portability"* [C]. This is precisely the
"unzip and run" contract an installer needs.

**Relocatability proven [M].** Copied the uv-managed tree to `C:\Users\username\relo_test`, then:

```
cwd = C:\Windows, no venv, no PYTHONPATH, no env vars
> relo_test\python.exe -c "import sys,numpy,cv2; print(sys.prefix)"
OK numpy 2.4.6 cv2 5.0.0
prefix = C:\Users\username\relo_test          # <- resolved to the new location
> relo_test\python.exe -c "import sqlite3,ssl,json,decimal,ctypes"
stdlib modules OK; ssl: OpenSSL 3.5.7      # no missing-DLL breakage
```

Contents and on-disk cost of the base tree **[M]**:

| Component | Size |
|---|---|
| total tree | **78 MB** |
| `Lib/` | 45 MB |
| `DLLs/` | 17 MB |
| `tcl/` | 8.3 MB |
| `python311.dll` | 5.6 MB |
| `include/`, `libs/` (dev headers — deletable) | 1.9 MB |

Note `tcl/`, `_tkinter.pyd`, `tcl86t.dll`, `tk86t.dll` are **all present** **[V]** — pbs ships
Tcl/Tk, which the official embeddable zip does not (§1.4). That makes Tkinter a *free* GUI option in
a vendored tree.

**Recipe that works** (the shippable pattern, verified end-to-end): unzip pbs → `uv pip install
--target <tree>\Lib\site-packages …` → ship the directory → launch `<tree>\python.exe -m myapp`.
Result: **234 MB** for CPython + numpy + opencv-headless, zero external dependencies **[M]**.

**Verdict: yes — this is what we vendor.**

### 1.4 The official embeddable zip — not suitable for shipping an app

Stated limitations [C]:

- *"Tcl/Tk (including all dependents, such as IDLE), pip and the Python documentation are not
  included."*
- *"A default `._pth` file is included, which further restricts the default search paths… This file is
  intended for embedders to modify as necessary."* — `._pth` **replaces** normal `sys.path`
  computation; `site` is not imported unless the file says so, so **`.pth`-based and
  `site-packages` behaviour is off by default** and must be re-enabled by editing the file.
- *"Using pip to manage dependencies as for a regular Python installation is not supported with this
  distribution, though with some care it may be possible to include and use pip for automatic
  updates."* — **no venv either** (`No module named venv`).
- CPython maintainer (zooba) on adding Tk: *"This is deliberate, and certainly won't be added to the
  default… it would be wasteful bloat"* [C].

So: no pip, no venv, no Tcl/Tk, an opt-in `sys.path`. Every one of those is fixable by hand-copying
files from a full distribution — which is exactly the manual work that pbs has already done, better,
with a licence file. **Verdict: skip it.** Its only genuine advantage over pbs is that python.org
signs/publishes it as the canonical artifact; irrelevant when we are the ones shipping.

### 1.5 The rest, quickly

| Option | Verdict |
|---|---|
| **pyenv-win** (7414★, pushed 2026-10-23) **[V]** | Fine, healthy, and it does work — but it solves *"I need many interpreters"*, which is not our problem, and it adds shim indirection. `uv` supersedes it for our use (and is already installed). **Skip.** |
| **WinPython** (2290★, pushed 2026-10-11) **[V]** | A portable *batteries-included science stack* (numpy/scipy/spyder preinstalled), Windows-native and genuinely convenient. But it is a **dev/teaching distribution**: huge, opinionated, versioned as a bundle, no locking. **Skip for the product; acceptable as a zero-setup fallback** if a dev cannot get `uv` working. |
| **miniconda / Anaconda** | Adds a second package ecosystem (conda-forge) and a solver for problems `uv` already solves, plus **Anaconda's commercial licensing terms** on the default channel (paid for orgs >200 employees — **UNVERIFIED** for our situation). Its real edge is non-PyPI binary deps, which we do not have. **Skip.** |

### 1.6 Recommendation

**Developer machine:** keep the existing `uv`; add a **pinned 3.13** (`uv python install 3.13`,
`uv python pin 3.13`), work inside `uv`-managed `.venv`s, and never type bare `python`. Optionally
disable the Store alias so that a stray `python` fails loudly instead of silently launching nothing.
Do **not** install the python.org install manager as well — two interpreter managers on one machine
is how PATH bugs are born.

**What we ship:** a **vendored python-build-standalone `install_only` tree** (start from the
`stripped` 20 MB archive, add `Lib\site-packages`), launched by `ministar.exe`/`.bat`, with
`EDSDK.dll` resolved from an app-relative directory. **Not** PyInstaller `--onefile`, **not** the
embeddable zip, **not** a system-Python dependency.

---

## 2. Vendoring and legal

| Component | Licence | Redistributable in our installer? |
|---|---|---|
| CPython | **PSF-2.0** | **Yes** [C] |
| python-build-standalone *build tooling* | **MPL-2.0** | Yes — and we are not modifying it. MPL is *file-level* copyleft; obligations attach to modified MPL source files, not to a binary bundle we merely redistribute [C] |
| numpy 2.5.4 | BSD-3 + 0BSD + MIT + Zlib + CC0 [C] | Yes |
| scipy 1.18.1 | BSD-3 [C] | Yes |
| opencv-python-headless | Apache-2.0 (packaging MIT) [C] | Yes |
| opencv-python (full) | Apache-2.0 **but bundles LGPL Qt/GStreamer plugins** | Prefer `-headless` to dodge LGPL relink duties |
| scikit-image 0.26 | BSD-3 [C] | Yes |
| astropy 8.0.2 | BSD-3 [C] | Yes |
| PySide6 6.12 | **LGPL-3.0 / GPL-2 / GPL-3** [C] | Only as **dynamic** linking, ship Qt DLLs unmodified, provide relink route |
| PyInstaller 6.22.3 | GPLv2-or-later **with special exception** [C] | Yes, the exception permits proprietary bundles |
| Nuitka 4.3.1 | **AGPLv3** [C] | See §3.4 — needs a compliance opinion |
| **EDSDK.dll / EdsImage.dll** | **Proprietary Canon** | **NO — not without written permission** [C] |

**What we must include.** For CPython, the PSF licence **plus** the *"Licenses and Acknowledgements for
Incorporated Software"* section — OpenSSL, expat, zlib, libffi, libmpdec, and the rest are bundled
inside the interpreter and each needs its notice carried [C]. Practically: ship `LICENSE.txt` from
the pbs tree (**it is already in the tree root** **[V]**) plus a `THIRD-PARTY-NOTICES.txt` generated
from the wheel metadata (`uv pip list --format=json` → licences), plus PySide6's LGPL text and the
Qt relink notice if Qt ships.

**Sizes to plan the installer around [M]** unless marked otherwise:

| Artifact | Size |
|---|---|
| pbs `install_only_stripped` 3.13 archive (compressed) | 20 MB [C] |
| pbs `install_only` 3.13 (compressed) | 45 MB [C] |
| …unpacked base tree | **78 MB [M]** |
| + numpy + opencv-headless (`--target`) | **+156 MB → 234 MB [M]** |
| PyInstaller `--onedir` (numpy+cv2) | 163 MB **[M]** |
| PyInstaller `--onefile` single exe | 62 MB exe **[M]** |
| PySide6-Essentials wheel | 78.3 MB [C] |

A realistic full install (Python + numpy + cv2 + scikit-image + astropy + Qt) lands around
**400–500 MB unpacked**. Budget for a ~150 MB compressed installer. **[U]** on the exact number until
we assemble the real set.

**Gotchas bundling numpy/opencv wheels.**

- **`cp311` is a trap.** numpy 2.5.4 publishes `cp312`/`cp313`/`cp314`/`cp315` win_amd64 wheels and
  **no `cp311`** [C]; our existing uv interpreter is 3.11, which silently resolves to numpy 2.4.6
  **[M]**. Pin 3.13.
- **opencv-python vs opencv-python-headless.** Full `opencv-python` drags in GUI/Qt plugin DLLs we do
  not need for a headless capture pipeline and which carry LGPL obligations; `-headless`
  (**42 MB** wheel [C]) is the right dependency.
- **`--target` does not run post-install steps.** For wheels that is fine (numpy/cv2/skimage/astropy
  are all wheels). It is **not** fine for anything needing a build step or a script's
  `console_scripts` shim — those land in `--target`'s `bin/`/`Scripts` unpredictably. We do not need
  them.
- **`vcruntime140.dll`/`vcruntime140_1.dll` ship inside the pbs tree** **[V]** — do not assume the
  user has the MSVC redistributable; pbs already handles it.
- **`site-packages` in a vendored tree is only on `sys.path` because it is the normal layout** **[M]**.
  Do not use a `._pth` (that is the embeddable zip's mechanism and it *disables* normal `sys.path`).

---

## 3. Packaging to an exe

### 3.1 The numbers that decide it [M]

All measured as subprocess launch → exit, 7 runs, same machine, app that does `import numpy, cv2`:

| Form | Cold start (median) | Size | Notes |
|---|---|---|---|
| `cmd.exe /c exit` (baseline spawn) | **30–40 ms** | — | matches brief 00's 43 ms |
| bare `python -c pass` | **59–62 ms** | — | already >43 ms |
| vendored tree, `-c pass` | **50–60 ms** | 78 MB | **fastest real option** |
| `python -c "import numpy"` | **231–243 ms** | — | 5.4× the budget |
| vendored tree, `import numpy, cv2` | **230–240 ms** | 234 MB | |
| **PyInstaller `--onedir`** | **184 ms** | 163 MB | worst case 1573 ms (Defender first-run scan) |
| **PyInstaller `--onefile`** | **2364 ms** | 62 MB | 13× worse; extracts to `%TEMP%\_MEIxxxx` every launch |
| `import numpy` alone, warm, in-process | **148 ms** | — | the irreducible marginal cost |

Three conclusions, none of them arguable:

1. **`--onefile` is disqualified.** 2.4 s to first frame is not a product. The whole saving is disk
   ergonomics and the cost is re-extraction on every launch [C].
2. **`--onedir` (184 ms) beats the vendored tree (230 ms) by ~45 ms** — plausibly because PyInstaller
   has already resolved imports into a flat layout. That is a real but small win *paid for* with
   freezing fragility and AV risk (§3.3, §3.4).
3. **Nothing here is under 43 ms.** So the architecture answer is identical for every option:
   **one long-lived Windows process**, spawned at session start, that owns the camera and the
   stacker; the `.bat`/CLI becomes a *control channel* to it, not a per-frame launcher (§3.5).

### 3.2 PyInstaller vs Nuitka vs vendored tree vs zip+bat

| | PyInstaller 6.22.3 (2026-09-12) | Nuitka 4.3.1 (2026-10-17) | **vendored tree + launcher** | zip + `.bat` |
|---|---|---|---|---|
| Mechanism | bootloader + frozen archive | **transpiles to C**, compiles with MSVC/MinGW | ship interpreter + packages as files | same, hand-rolled |
| Cold start | 184 ms onedir / 2364 ms onefile [M] | claimed faster than PyInstaller; **benchmarks are thin and mostly not-Windows** — treat as **[U]** | **230–240 ms** [M] | same |
| Binary size | 163 MB [M] | smaller in most reports [C] | 234 MB [M] | same |
| Build time | ~23 s for numpy+cv2 [M] | **minutes**, needs a working C toolchain | ~seconds (unzip + `uv pip install --target`) | seconds |
| Native DLL (`EDSDK.dll`) | needs explicit `binaries=`; arch must match | via `--include-data-files` | **drop it next to the exe**; trivially correct | same |
| Debuggability | opaque; tracebacks into frozen code | better (real C) | **best — it is just Python on disk** | best |
| AV false positives | **chronic** (§3.3) | lower but non-zero | **lowest** — no self-extracting bootloader | lowest |
| Toolchain | none beyond Python | MSVC/MinGW, long CI | none | none |
| Licence | GPLv2 + exception [C] | **AGPLv3** [C] | PSF/MPL — clean | clean |

**Verdict: vendored tree + a small launcher.** We lose ~45 ms versus PyInstaller onedir and gain:
exact arch control for `EDSDK.dll`, no bootloader for Defender to hate, a tree we can hot-patch
(swap a `.py` and restart the daemon instead of a full rebuild), and `sys.prefix` behaviour that has
been proven correct under relocation **[M]**. If a single-file installer is a UX requirement, wrap the
*vendored tree* in Inno Setup/MSIX — that is an installer, not a self-extractor, and it does not pay
2.4 s per launch.

### 3.3 Antivirus false positives

PyInstaller's bootloader has a long, unresolved history of Defender/AV detection —
[pyinstaller#5492](https://github.com/pyinstaller/pyinstaller/issues/5492) (Defender flags
`runw.exe`), [#6754](https://github.com/pyinstaller/pyinstaller/issues/6754) (`--onefile` flagged),
[#7019](https://github.com/pyinstaller/pyinstaller/issues/7019) (the *wheel itself* flagged) [C]. This
is structural: a stripped, UPX-ish, self-extracting PE that writes to `%TEMP%` and spawns a child is
the shape of a dropper. Nuitka is not immune either — recent examples of ML-heuristic detections on
compiled binaries abound ([microsoft/apm#487](https://github.com/microsoft/apm/issues/487) is a
`Trojan:Win32/Bearfoos.B!ml` hit) [C].

Observed here, incidentally, in the **onedir worst-case of 1573 ms [M]** — that spike is almost
certainly Defender's first-run scan of a freshly written 163 MB tree, exactly the effect brief 00 §8
warned about. It is a real cost on every rebuild, and a much bigger one on a user's first launch.

**Mitigations, in order of effect:** (1) do not use a self-extracting bootloader; (2) **code-sign
everything**; (3) submit to Microsoft's false-positive portal and keep a clean build hash; (4) avoid
UPX entirely (`--noupx`); (5) ship a stable, versioned publisher identity so reputation accrues.

### 3.4 Code signing

- **Azure Artifact Signing (formerly Trusted Signing)**: ~**$9.99/month** for up to 5,000 signatures
  (~$120/yr), fully managed cert lifecycle [C].
- **EV certificates no longer buy instant SmartScreen reputation.** As of 2026: *"EV code signing
  certificates no longer grant instant SmartScreen reputation. New releases of EV-signed apps must
  build reputation organically, just like OV-signed apps."* [C]
- Microsoft's own guidance: a valid OV/EV signature still shows *"⚠️ Warning — app flagged as
  unrecognized"* on first download until reputation accrues; only **Microsoft Store (MSIX)** re-signing
  avoids the warning entirely [C].

Implication: signing is mandatory (it stops the worst heuristics and is required for any
professional distribution) but it **will not** make first-run warnings disappear. A Store/MSIX
distribution path is the only thing that does. Plan for the warning, do not plan around it.

Nuitka's AGPLv3 licence is a separate compliance question: the commonly-held reading is that
Nuitka's output is not a derived work of Nuitka, but that is an opinion we would need on paper before
shipping a commercial product built with it. **[U]** — and another reason to prefer the vendored tree.

### 3.5 A resident daemon removes the startup cost entirely — and it is the only viable shape

The numbers in §3.1 make this non-optional. The shape:

```
ministar.exe (launcher, ~60 ms)  ──spawn once──►  daemon (long-lived)
   │                                                ├─ EDSDK message-pump thread  (owns the camera)
   │  ◄── named pipe / localhost HTTP ──────────────┤─ capture thread
   │                                                ├─ stacking thread (numpy/cv2)
   └─ exits or stays as the tray/UI                 └─ UI thread
```

Concretely, on this machine: the daemon pays **230–240 ms once** [M] and then **zero** per frame. A
per-frame subprocess design pays 230 ms against a 43 ms budget and 2 s exposures — i.e. it *cannot*
sustain even a 1 fps live-view loop, let alone the 10–30 fps brief 02 discusses. Since briefs 00 §8
and 02 §1.6 already converged on a long-lived Windows process for *interop* reasons, Python adds a
second, independent reason to make the same choice. That convergence is the strongest architectural
signal in this document.

---

## 4. `pyedsdk` deep dive

### 4.1 Project health [V] (all read from PyPI JSON + GitHub API + source, 2026-10-24)

| Fact | Value |
|---|---|
| PyPI name / latest | `pyedsdk` **0.2.16** |
| Last release | **2026-06-09** |
| First release | 2026-02-23 (then 0.2.2…0.2.11 all within 48 h — a burst) |
| Release cadence since | 0.2.12, 0.2.15 (2026-04-03), 0.2.16 (2026-06-09) — **~4.5 months idle** |
| Maintainer | **François Margall** (`fmargall`), sole PyPI owner, sole contributor |
| Repo | `github.com/fmargall/pyedsdk` — **5★, 1 fork** |
| Repo last push | **2026-06-09** |
| Open issues | **1** — "#2 docs: add 2 language of docs" (2026-08-20) |
| Classifier | **`Development Status :: 3 - Alpha`** |
| Licence | PyPI `license_expression: MIT`; **GitHub API reports `NOASSERTION`/"Other"** — inconsistent |
| Dependencies | `pywin32>=306`, Windows only |
| `requires_python` | **null** (no constraint) |

**Health verdict:** alpha, single-maintainer, 5 stars, one unaddressed issue, 4.5 months quiet.
The *absence* of bug reports on a 5-star repo is not health — it is absence of users. Do not read
"0 open bugs" as "works".

### 4.2 Does it bundle EDSDK? No — and how it finds the DLL

Explicitly, from the README: *"For intellectual property and licensing reasons, the EDSDK dynamic
libraries (`EDSDK.dll`) are not distributed with this Python package"*; Canon requires developers to
request the SDK directly, *"This may take a few days before you have access"* [C].

Resolution order, **from `pyedsdk/core/loader.py`** [V] — this is the whole algorithm:

1. `EDSDK_PATH` environment variable (a *file* path, not a directory);
2. **every directory on `PATH`**, looking for a literal `EDSDK.dll`;
3. `Path.cwd() / "EDSDK.dll"`.

then `ctypes.windll.LoadLibrary(...)`, else `RuntimeError("EDSDK.dll not found.")`. An explicit path
override exists: `loadSDKLib(r"C:\Canon\EDSDK\Dll\EDSDK.dll")`.

**Two packaging consequences.** (a) There is **no package-relative / app-relative search** — a
frozen app must either set `EDSDK_PATH` or `os.chdir()` before the first call, or call
`loadSDKLib()` with an explicit path. (b) There is **no architecture check**: `loader.py` imports
`platform` but only tests `platform.system() != "Windows"`. Point a 64-bit interpreter at the
`EDSDK/` (32-bit) tree instead of `EDSDK_64/` and you get an opaque loader failure rather than a
clear "wrong architecture" message. Note brief 02 §1.3: the 64-bit DLLs live in a **sibling tree**
`EDSDK_64/Dll/`, not a subfolder — an easy mistake.

### 4.3 API coverage — the decisive section

**30 of 56 EDSDK functions are bound** — the source literally annotates counts (`# Number of
functions binded: 30 / 56`) [V]. What is bound:

`EdsInitializeSDK`, `EdsTerminateSDK`, `EdsRelease`, `EdsGetChildCount`, `EdsGetChildAtIndex`,
`EdsGetPropertyData`, `EdsSetPropertyData`, `EdsGetPropertyDesc`, `EdsGetCameraList`,
`EdsGetDeviceInfo`, `EdsOpenSession`, `EdsCloseSession`, `EdsSendCommand`,
`EdsSendStatusCommand`, `EdsSetCapacity`, `EdsGetVolumeInfo`, `EdsCreateFileStream`,
`EdsCreateMemoryStream`, `EdsCreateEvfImageRef`, `EdsDownload`, `EdsDownloadCancel`,
`EdsDownloadComplete`, `EdsDownloadEvfImage`, `EdsGetDirectoryItemInfo`, `EdsGetLength`,
`EdsGetPointer`, `EdsGetEvent`, `EdsSetObjectEventHandler`, `EdsSetPropertyEventHandler`,
`EdsCreateFlashSettingRef` [V].

**What we need and what is missing:**

| Capability | `pyedsdk` 0.2.16 | Evidence |
|---|---|---|
| Enumerate cameras | ✅ `_getCameraList()` + `_getChildCount`/`_getChildAtIndex`, `Camera(cameraIndex)` | [V] |
| Device info | ✅ `_getDeviceInfo` → `szPortName`, `szDeviceDescription`, `deviceSubType` | [V] |
| Open/close session | ✅ in `Camera.__init__` / `_close()`, `__enter__`/`__exit__` | [V] |
| **Set ISO / Tv / Av** | ✅ properties `isoSpeed`, `shutterSpeed` (seconds as float), `aperture`; `getAvailableApertures()`; `EdsSetPropertyData` bound | [V] |
| **Enumerate legal values** | ✅ `_getPropertyDesc` → `_PropertyDesc{form, access, numElements, propDesc[128]}` | [V] |
| `TakePicture` | ⚠️ `shot()` deliberately uses `PressShutterButton` (Completely→OFF) instead of `TakePicture` (*"when using the mirror lockup, _TakePicture command won't work"* — a defensible choice) | [V] |
| **Bulb** | ❌ **ABSENT.** `_CameraCommand` binds **2 of 15** commands: `_TakePicture = 0x0`, `_PressShutterButton = 0x4`. No `BulbStart`, no `BulbEnd`, no method, no enum member. `_ShutterSpeed._Bulb = 0x0C` exists only as a *Tv value*, not a command | [V] |
| **Bulb elapsed time** | ❌ **ABSENT**, and doubly blocked: `kEdsStateEvent_BulbExposureTime` (0x310, see brief 02 §1.9) arrives via the **camera-state handler**, which `pyedsdk` does not bind at all. So we could not even report how long the shutter had been open | [V] |
| **Long exposure** | ❌ **`shot()` has a hard 15 s ceiling.** `_waitForEvent(event, timeout=15)` loops `PeekMessageW` + `sleep(0.01)` and raises `TimeoutError("EDSDK event timeout after 15 seconds")` | [V] |
| **Live view frames** | ⚠️ Works, but `LiveViewStream.getFrame() -> bytes` returns **raw JPEG bytes** via `ctypes.string_at(ptr.value, size)`. **No numpy, no PIL, no Bayer decode** — we decode ourselves | [V] |
| LV start/stop | ✅ `_startLiveView()` sets `Evf_Mode=1` and ORs `EvfOutputDevice._PC` — correct idiom | [V] |
| LV frame retry | ✅ retries `ERR_OBJECT_NOTREADY` up to **750** times, then raises `ERR_WAIT_TIMEOUT_ERROR` | [V] |
| Download captured image | ⚠️ file-only, via the **object event handler**: on `DirItemRequestTransfer` it calls `_getDirectoryItemInfo`, `_createFileStream(filename, CreateAlways, Write)`, `_download(ref, itemSize, stream)`, `_downloadComplete(ref)` | [V] |
| **Property events** | ✅ handler registered (`_setPropertyEventHandler`), `_PropertyEventHandler = WINFUNCTYPE(c_uint32, c_uint32, c_uint32, c_uint32, c_void_p)` | [V] |
| **Object events** | ✅ handler registered for `DirItemRequestTransfer` only | [V] |
| **Camera-state events** | ❌ **ABSENT.** No `EdsSetCameraStateEventHandler` binding at all ⇒ **no `WillSoonShutDown`, no `ShutDownTimerUpdate`, no `JobStatusChanged`, no `BulbExposureTime`** — i.e. no way to call `ExtendShutDownTimer` in response to the body warning it is about to sleep. On a 30-minute integration this matters | [V] |
| Camera-added handler | ❌ absent (`EdsSetCameraAddedHandler` unbound) | [V] |
| Progress callback | ❌ absent (`EdsSetProgressCallback` unbound) | [V] |
| `SaveTo=Host` | ✅ set in `Camera.__init__` (`_SaveTo._Host`) | [V] |
| `EdsSetCapacity` | ✅ bound, but `pyedsdk` binds only `_CreateFlashSettingRef`-adjacent helpers — capacity reporting is not wired into `shot()` | [V] |

**Are callbacks usable from Python without blocking?** Partly. They are `WINFUNCTYPE` function
pointers, and EDSDK on Windows delivers events **only while a message pump runs** — `pyedsdk`
implements that as `PeekMessageW/TranslateMessage/DispatchMessageW` inside `_waitForEvent`, which is
**only called from `shot()`** [V]. Consequences:

- There is **no standing pump**. Between shots, `EdsGetEvent()`/the pump is not being called, so
  property and state notifications are not delivered. (Brief 02 §1.5 notes `EdsGetEvent()` must be
  called regularly on Windows.)
- `shot()` **blocks the calling thread** for the whole exposure+download and therefore must be called
  off the UI thread — but then it is also the only thing pumping messages, which couples the pump to
  the capture. A UI thread that also wants messages will be fighting this.
- Live view in callback mode spins a **`threading.Thread(daemon=True)`** calling `getFrame()` +
  `callback(frame)` in a tight loop with **no sleep** — a busy-spin that will hold a core and, given
  the GIL result in §0.6, will contend for the GIL on every callback invocation.
- `__del__` calls `_close()`, which calls `_SDK._terminate()`. Finaliser-driven SDK teardown is
  fragile (interpreter shutdown, partially-constructed objects).

**Our own pump thread is therefore mandatory regardless of which binding we use** — that is a design
requirement, not a `pyedsdk` defect.

### 4.4 64-bit Python + EDSDK 13.x?

- EDSDK 13.x ships both `EDSDK/Dll/` (32-bit) and `EDSDK_64/Dll/` (64-bit) trees, and brief 02 §1.3
  records 13.12.31 as the version researched. **[C]** via brief 02.
- `pyedsdk` is pure `ctypes` (no compiled extension, `pywin32` only for Windows API access), so there
  is **no ABI to go stale** — a 64-bit interpreter loading a 64-bit `EDSDK.dll` should work in
  principle. **[U] whether anyone has run pyedsdk against 13.x on 64-bit** — there are no issues,
  no CI, no test matrix, and no test suite mention in the README. Treat as unproven.
- **Known-breakage surface [U]:** (1) passing the 32-bit DLL path yields a raw loader error, not a
  diagnostic (§4.2); (2) `_DeviceInfo`/`_VolumeInfo`/`_DirectoryItemInfo` use `c_char * 256` and
  `c_uint64` fields — layout must be `#pragma pack`-compatible, which pbs/CPython **does** default to
  correctly, but any `.h`-only field reordering in a future EDSDK would break silently; (3) EDSDK
  13.x added properties (`TempStatus`, `AutoPowerOffSetting`, `ScreenOffTime`) that `_PropertyID`
  need not include.
- One structural gap: `_BaseRef` etc. are all `ctypes.c_void_p`, not `c_void_p` under a typed handle
  class. That is **correct and safe** for opaque EDSDK refs, but it means the type system cannot catch
  passing a `StreamRef` where a `CameraRef` belongs. Expect `EDS_ERR_INVALID_HANDLE` to be the only
  feedback.

### 4.5 Alternatives

| Option | Assessment |
|---|---|
| **`Jiloc/edsdk-python`** | 64★, 32 forks, MIT, created 2022-08 — the most-used binding. But **last push 2023-11-13** (~3 years stale) [V], 8 open issues including **#9 "Live Stream" (2025-02-08, unanswered)** and **#7 "Registering CameraAddedHandler doesn't accept callables"** [V]. Requires **building a C++ extension** against EDSDK headers and *editing `EDSDKTypes.h`* per its README [C]. C++ extension ⇒ per-Python-version rebuilds, MSVC dependency, and a live-stream feature that is openly unresolved. **Reject.** |
| **`chibiegg/python-edsdk-ctypes`** | v0.0.1 (2026-05-02), MIT, pure ctypes, cross-platform, **0★** [V]. Interesting as a *reference implementation* to read, not as a dependency. |
| **`andrewrk/pyedsdk`** | 2010, *"install python 3.1 32bit (can't be 64bit)"* [C]. Dead. |
| **`pythonnet`** | Legitimate technology (embeds the CLR; 3.0.5 added 3.13 support; free-threaded supported from 3.14 per its threading docs) [C]. But it means shipping a **.NET runtime** — which this machine does not even have as an SDK — to call a C DLL through a managed wrapper. Strictly more moving parts than `ctypes`. **Reject for our case.** |
| **digiCamControl HTTP API** | 733★, actively maintained (pushed 2026-10-22), and it does expose a dev API / web-server surface [V/C]. But it is **a whole application to install, supervise and version**, it wraps EDSDK anyway (so it cannot dodge Canon's licence), and it adds latency we cannot budget. Interesting as a *fallback* if EDSDK access is denied. **Reject as primary.** |
| **`gphoto2`** | **No `win_amd64` wheels**; libgphoto2 does not support Windows ("*Support for operating systems from Microsoft is not currently available*") — brief 02 §3. **Reject.** |
| **Canon CCAPI** | Latest **1.4.0d (2025-11-17)** [C]. Real body coverage: EOS R1, R5 II, R6 III, R3, R5, R6 II, R7, R8, R10, R50, R50V, R100, M50 II, PowerShot V10 [C]. **But: "Currently, CCAPI supports Ethernet connectivity only the 1D X Mark III. All other CCAPI compatible cameras currently only support WiFi."** [C] ⇒ **not a wired-in alternative** on the bodies we care about. It is HTTP-over-Wi-Fi, so it is also a different reliability and latency regime (and a different failure mode: radio interference at a dark-sky site). **Reject as the wired path; keep as an optional transport.** |

### 4.6 Verdict: write our own `ctypes` binding

**`pyedsdk` is not production-viable for this product**, for reasons that are structural rather than
a matter of polish:

1. **No bulb.** For an astrophotography tool whose whole premise (per brief 03) is hundreds of 1–15 s
   subs, and which will want >30 s integrations, the absence of `BulbStart`/`BulbEnd` is fatal. Adding
   it means patching a package we do not control.
2. **A hard 15 s ceiling in `shot()`.** Even for non-bulb timed exposures, `_waitForEvent(timeout=15)`
   raises `TimeoutError` on any exposure ≥ ~15 s. This alone rules out 30 s and 60 s subs.
3. **No camera-state events**, so no power-off therapy (`WillSoonShutDown` → `ExtendShutDownTimer`)
   and no bulb elapsed-time signal — the bulb workflow brief 02 §1.9 specifies
   (`kEdsAEMode_Bulb` → `BulbStart` → `BulbExposureTime` → `BulbEnd`, with `BulbEnd` guaranteed in a
   `finally` path) is not expressible through this API. That is not a small gap: leaving a shutter
   open because the host process died makes the body unusable until it is power-cycled.
4. Alpha status, one maintainer, 5 stars, no tests, no CI, 4.5 months idle, licence metadata
   inconsistent between PyPI and GitHub.
5. Pump threaded through the capture call, busy-spin LV thread, `__del__`-driven SDK teardown — all
   things we would have to fight.

The counterargument — "43 ms and a 20-function surface means we can write it ourselves" — is right.
**The surface we actually need is ~20–24 functions:**

*Lifecycle (8):* `EdsInitializeSDK`, `EdsTerminateSDK`, `EdsGetCameraList`, `EdsGetChildCount`,
`EdsGetChildAtIndex`, `EdsGetDeviceInfo`, `EdsOpenSession`, `EdsCloseSession`.

*Properties (4):* `EdsGetPropertyData`, `EdsSetPropertyData`, `EdsGetPropertyDesc`,
`EdsGetPropertyDescEx` (13.19+, optional).

*Commands (4):* `EdsSendCommand` (covers `TakePicture`/`BulbStart`/`BulbEnd`/`PressShutterButton`/
`DoEvfAf`/`DriveLensEvf`), `EdsSendStatusCommand` (`UILock`/`UIUnLock`), `EdsSetCapacity`,
`EdsGetEvent`.

*Live view (3):* `EdsCreateMemoryStream`, `EdsCreateEvfImageRef`, `EdsDownloadEvfImage`
(+ `EdsGetPointer`, `EdsGetLength` to read the buffer).

*Download (5):* `EdsCreateFileStream` (or memory stream), `EdsDownload`, `EdsDownloadComplete`,
`EdsGetDirectoryItemInfo`, `EdsGetVolumeInfo`.

*Events (3):* `EdsSetObjectEventHandler`, `EdsSetPropertyEventHandler`,
**`EdsSetCameraStateEventHandler`** — the one `pyedsdk` omits and we need.

*Memory (1):* `EdsRelease`.

Total ≈ **22–26**, of which ~10 are trivial (no structs). That is a few hundred lines.

**`ctypes` pain points, ranked by how much they will actually hurt:**

| Hazard | What it costs us |
|---|---|
| **`EdsBaseRef` opaque handles** | Not a problem — `c_void_p` is the right representation. The cost is that every ref needs a matching `EdsRelease` on every path, or you leak. Enforce with `try/finally` and a small handle wrapper. |
| **Structs by value** (`EdsCapacity` in `EdsSetCapacity`, `EdsDeviceInfo`, `EdsPropertyDesc`, `EdsDirectoryItemInfo`, `EdsRational`) | Must match EDSDK's packing exactly. `c_char*N` + `c_uint64` ordering errors do not raise — they silently corrupt. **Mitigate by asserting `ctypes.sizeof()` against values derived from the header**, and by testing `sizeof` on both 32- and 64-bit. |
| **Callbacks + GIL + lifetime** | `WINFUNCTYPE` callables must be kept alive (a dangling callback is a crash, not an exception). Any Python exception inside a callback crosses a C frame — wrap every handler body in try/except and stashing the error. **Verified good news:** the *outbound* calls release the GIL (§0.6), so a callback on EDSDK's own thread can acquire it normally. |
| **Message pump** | EDSDK on Windows needs `PeekMessageW`/`DispatchMessageW`/`EdsGetEvent` pumped from a thread that is not blocked. **Dedicated pump thread, always running**, is the only clean design. |
| **`EdsUInt32`/`EdsInt32` typedefs** | `c_uint32`/`c_int32`; fine. The trap is `EdsPropertyID` being a `uint32` *used as* an enum — an unknown ID is a silent 0, not an error. Keep an explicit `IntEnum` and validate. |
| **`EdsRelease` ownership** | EDSDK refs are refcounted. Returning a ref from `EdsGetChildAtIndex` transfers ownership *to the caller*. Get this wrong and you leak across a multi-hour session. |
| **Error codes** | `EdsError` is `uint32`; `EDS_ERR_*` must be checked on **every** call. Note brief 02 §1.5: illegal property values return **`EDS_ERR_DEVICE_BUSY`**, which reads as "camera busy" but means "bad value" — the single most misleading behaviour in the whole API. |

Mitigation strategy: build the binding from the **`EDSDK.h` header programmatically** (the header is
doxygen-annotated and brief 02 §1.3 lists 60 exported functions), generate `argtypes`/`restype`
declarations mechanically, and add a `sizeof` self-test at import. That is far more robust than
hand-writing 25 prototypes, and it is what makes DIY the *safer* path rather than the braver one.

---

## 5. The rest of the stack on Windows

### 5.1 Wheels: availability and cost (win_amd64, CPython 3.13/abi3) [C]

| Package | Latest | Windows wheel | Size |
|---|---|---|---|
| numpy | 2.5.4 | `cp313-win_amd64` | **12.0 MB** |
| scipy | 1.18.1 | `cp313-win_amd64` | **34.9 MB** |
| opencv-python | 5.0.0.93 | `cp37-abi3-win_amd64` | **42.0 MB** |
| opencv-python-headless | 5.0.0.93 | abi3 | ~30 MB (no Qt) |
| scikit-image | 0.26.0 | `cp313-win_amd64` | **11.4 MB** |
| astropy | 8.0.2 | `cp311-abi3-win_amd64` | **6.2 MB** |
| Pillow | 13.0.0 | `cp313-win_amd64` | **6.9 MB** |
| pywin32 | 312 | `cp313-win_amd64` | **6.6 MB** |
| PySide6-Essentials | 6.12.0 | `cp310-abi3-win_amd64` | **78.3 MB** |
| PyQt6 | 6.11.0 | abi3 | 6.5 MB (+Qt libs separately) |

**All of them ship win_amd64 wheels; none needs a compiler.** That is the single biggest practical
reason Python is viable here at all. Everything we need is a binary install, which means the
`uv pip install --target` vendoring recipe in §1.3 works with no MSVC on the build machine.

### 5.2 GUI

| Option | Size | Licence | Verdict |
|---|---|---|---|
| **PySide6 (Qt)** | **78 MB** (Essentials) [C] | LGPL-3 [C] | The capable choice: a real widget set, `QThread`/signals that map cleanly onto a capture thread + UI thread, and a proper image-display path. Cost: +78 MB, LGPL compliance (dynamic link only, ship Qt unmodified), and Qt's own DLL-loading quirks inside a frozen app. |
| **Tkinter** | **0** (pbs ships `tcl/`, `_tkinter.pyd`, `tk86t.dll` — verified present [V]) | PSF | Free in every sense, in a vendored tree. **Good enough for a control panel** (sliders, a canvas for the live-stack preview, a log pane). Weak for modern visual polish and for high-rate image redraws (the `PhotoImage` path wants a `PPM`/`PNG` round-trip per frame — **UNVERIFIED** cost, but it is the known weak point). |
| **Local web UI (FastAPI + htmx)** | FastAPI 0.162 (MIT), uvicorn 0.54 (BSD) — tiny [C] | MIT/BSD | Attractive: the daemon already *is* a process with state; the UI becomes a browser tab; **no GUI toolkit on the IPC path at all**, which keeps the capture thread clean. Downsides: a localhost HTTP surface (brief 00 §8 already flags the **Windows Firewall prompt on first bind**), a browser dependency, and higher frame-to-UI latency. |

**Recommendation:** start with **Tkinter** (zero marginal size, already in the tree) for the control
surface, keep the *live-stack preview* as `numpy → cv2.imwrite/imencode → Qt-or-Tk image`, and treat
FastAPI+htmx as the "if we outgrow Tk" escape hatch which happens to share the daemon's IPC layer.
Defer PySide6 until something actually needs it — 78 MB and an LGPL obligation should be earned.

### 5.3 Resident daemon, UI thread, and not blocking on EDSDK

The GIL measurements in §0.6 are the key to this section, and they are favourable:

- **`ctypes.windll` releases the GIL for the duration of the call [M]** (counter thread ran at 0.94×
  idle rate during a 200 ms `windll` `Sleep`; `ctypes.CDLL` likewise 0.88×; numpy matmul 1.14×). So a
  capture thread that is blocked *inside* `EdsDownloadEvfImage` or `EdsSendCommand` is **not**
  holding the GIL. A stacking thread keeps running.
- **Pure-Python work does serialise**: two copy-heavy Python threads took **1.84×** the single-thread
  time [M]. So per-frame bookkeeping must be short, and the heavy lifting must stay in numpy/cv2
  (which release the GIL).
- `import ctypes` costs 10.6 ms, `import json` 6.4 ms, `import numpy` **147.9 ms** [M] — all one-time
  in a daemon.

Resulting thread plan:

| Thread | Work | GIL behaviour |
|---|---|---|
| **EDSDK pump** | `PeekMessageW`/`DispatchMessageW`, `EdsGetEvent()` on a ~10 ms tick, dispatch to a queue | brief holds, releases in calls |
| **Capture** | `EdsSendCommand(BulbStart)`, sleep, `BulbEnd`; or `EdsDownloadEvfImage` per frame | GIL released during each call [M] |
| **Stacking** | `cv2.imdecode` (**6.3 ms @1024×680, 22.3 ms @1920×1280 [M]**), `cv2.warpAffine` (**0.81 ms [M]**), `acc += frame` (**0.074 ms [M]**) | GIL released inside cv2/numpy [M] |
| **UI** | Tk/Qt event loop | brief holds |
| **IPC** | named pipe or localhost socket | brief holds |

Cross-thread anything with `queue.Queue` and never with shared mutable state. **The pump thread must
be separate from the capture thread** because `shot()`-style blocking calls must not stop the pump —
this is exactly the design that `pyedsdk` gets wrong (§4.3).

### 5.4 Is the GIL a problem for capture + stacking?

**No, not for this architecture** — and this is a measured answer, not an assumption. Because (a)
every EDSDK call is a `ctypes` foreign call that drops the GIL, and (b) all the pixel work is in
numpy/cv2 which drop the GIL, the two threads genuinely overlap. The GIL would only bite if the
stacking pipeline were hand-written Python loops — which per brief 03 it must not be anyway (§4:
"do all registration at ≤2 MP", cv2-based).

If we ever *do* hit a GIL wall, the escape route now exists: **free-threaded CPython is officially
supported as of 3.14 (PEP 779)** [C], pbs publishes `freethreaded-install_only` builds [C], and
numpy already ships `cp314-cp314t-win_amd64` wheels and scikit-image ships `cp313t` [C/V]. But: the
`t` builds are ABI-distinct, ecosystem coverage is partial, and **no one has ever run an EDSDK
`ctypes` binding on a free-threaded build** [U]. Treat free-threading as a future option, not a
design premise.

---

## 6. Python-specific risks for this project, honestly

| # | Risk | Severity | Evidence / mitigation |
|---|---|---|---|
| 1 | **Spawn cost vs 43 ms budget** | **High — structural** | 59–62 ms bare, 231–243 ms with numpy [M]. **Mitigation: resident daemon (§3.5). No other mitigation exists.** |
| 2 | **`pyedsdk` cannot do bulb, and caps `shot()` at 15 s** | **Fatal, if we use it** | §4.3/§4.6 [V]. **Mitigation: own the binding.** |
| 3 | **No EDSDK redistribution right** | **High — legal/product** | Canon proprietary; `pyedsdk` ships without it for exactly this reason [C]. **Mitigation: user-supplied DLL, or a written grant.** Decide before choosing an installer. |
| 4 | **Packaging fragility** | Medium | Import resolution, native DLL discovery (no app-relative search in any binding we have seen), `sys.path` surprises. PyInstaller hides these until they break at a user's site; a vendored tree makes them *visible* [M: relocation test passed]. **Mitigation: vendored tree + a startup self-test that prints `sys.prefix`, the EDSDK path, and EDSDK version.** |
| 5 | **Antivirus false positives** | Medium | Chronic for PyInstaller [C]; observed a 1573 ms Defender first-run spike in our own onedir build [M]. **Mitigation: no self-extracting bootloader, code-sign, submit FPs.** |
| 6 | **EDSDK callback reentrancy** | Medium | Handlers fire on EDSDK's thread while it pumps; exceptions cross a C frame; a dangling `WINFUNCTYPE` callback is a hard crash. `pyedsdk` releases refs *inside* its handler [V], which shows how easy it is to get ownership wrong. **Mitigation: handlers do nothing but enqueue; all logic on our own thread; wrap every handler in try/except; keep callback objects alive in a module-level list.** |
| 7 | **numpy ↔ EDSDK buffer copying** | **Low — measured, not a problem** | `string_at` 2 MiB = **0.42 ms**, `np.frombuffer` = **0.46 ms** [M]. Copy freely; optimise elsewhere. |
| 8 | **Real-time throughput vs a compiled language** | Low–Medium | The per-frame costs are dominated by *cv2/numpy*, not Python: JPEG decode 6.3/22.3 ms, warp 0.81 ms, accumulate 0.074 ms [M]. Python's own overhead is per-*call*, not per-pixel. Where it will hurt is **many small Python-level operations per frame** (1.84× GIL serialisation [M]) — a design smell to avoid. **Mitigation: keep the frame loop as few vectorised calls as possible.** |
| 9 | **GIL** | **Low — measured** | `windll`/numpy release it [M] (§0.6). Not the bottleneck anyone expected. |
| 10 | **Long-session resource leaks** | Medium | Every `EdsCreateXxx`/`EdsGetChildAtIndex` ref needs `EdsRelease`; a multi-hour session leaks until the body refuses to open. `__del__`-based teardown (as `pyedsdk` does [V]) does not survive `Ctrl-C`. **Mitigation: explicit `close()` on every path; log handle counts; restart the daemon between sessions.** |
| 11 | **`import` cost of the science stack** | Low | 148 ms for numpy [M] + cv2/skimage/astropy on top — one-time in a daemon, and a 400-500 MB tree is the price. **Mitigation: preload in the daemon before the UI says "ready".** |
| 12 | **Licence hygiene across a big tree** | Low but tedious | PSF + MPL + BSD + Apache + LGPL(Qt) + proprietary(EDSDK). **Mitigation: generate notices from wheel metadata in CI; ship `LICENSE.txt` from the pbs tree verbatim (already present [V]).** |

**The honest summary.** Python is a *worse* choice than C#/C++ on exactly two axes — cold-start cost
and distribution complexity — and the numbers show both are manageable if and only if we commit to a
resident daemon and a vendored interpreter tree. It is a *better* choice on the axes that matter for
this project: the whole astro imaging stack (numpy, cv2, scikit-image, astropy, SEP, astroalign)
already exists as tested Windows wheels, and brief 03's stacking pipeline is written against it.

The one place Python actively costs us is that **all four candidate EDSDK bindings are inadequate**
(§4), which means the capture layer must be hand-written `ctypes` no matter what — and that work is
identical in difficulty to writing it in C# `P/Invoke`. That is the real trade being made: we accept
a new risk (possession of a bespoke binding) in exchange for an existing ecosystem.

If EDSDK redistribution permission is refused **and** the user-supplied-DLL UX is unacceptable, revisit
**C# (.NET) + a thin localhost API driven from Python**: the camera layer is ~25 P/Invoke signatures
either way, C# has no cold-start problem, and the imaging stack can stay in a Python process. That
hybrid is the fallback, and it is a fallback worth pricing early because it changes the installer.

---

## 7. Open questions

- **Does `pyedsdk` (or any binding) actually work against EDSDK 13.x on 64-bit Windows?** Unproven
  anywhere; no CI, no issues, no tests. Needs an afternoon with a real body. **[U]**
- **Does Canon grant redistribution of `EDSDK.dll` to a third-party application vendor?** The whole
  installer design depends on the answer. Ask Canon; do not guess. **[U]**
- **Defender first-run cost on a fresh 234 MB vendored tree** — we measured 1573 ms for a 163 MB
  PyInstaller onedir; the vendored equivalent is untested. **[U]**
- **Tkinter per-frame image display cost** at 10–30 fps live view — unmeasured, and it is the one
  place Tk could disqualify itself. **[U]**
- **Free-threaded 3.14 + `ctypes` + EDSDK callbacks** — completely untested territory. **[U]**
- **CCAPI wired viability** — only the 1D X Mark III does Ethernet, so this is closed for R bodies,
  but the R1 supports Ethernet per Canon's matrix [C] and any future body might. Revisit only if EDSDK
  access fails.
- **Whether `uv`'s `--target` install of a full science stack is reproducible enough for release
  builds** — `uv lock` covers the venv path; the `--target` path used for vendoring needs its own
  lockfile discipline (`uv pip compile`), untested here at full scale. **[U]**

---

## 8. Sources

**Interpreter / packaging**
- `uv` — installing Python: <https://docs.astral.sh/uv/guides/install-python/>
- `uv` — Python versions (managed vs system, pinning, `.python-version`, externally-managed): <https://docs.astral.sh/uv/concepts/python-versions/>
- python-build-standalone (portability claims, PGO/LTO): <https://docs.astral.sh/python-build-standalone/>
- python-build-standalone releases (asset names/sizes, `install_only`, `stripped`, freethreaded): <https://github.com/astral-sh/python-build-standalone/releases>
- python-build-standalone repo/licence (MPL-2.0): <https://github.com/astral-sh/python-build-standalone>
- CPython "Using Python on Windows" — install manager, MSIX, per-user only, `--target`, `--refresh`, `py install 3.15t`, embeddable limits, `._pth`: <https://docs.python.org/3/using/windows.html>
- Python install manager source/releases: <https://github.com/python/pymanager>
- CPython licence (PSF-2.0 + incorporated software notices): <https://docs.python.org/3/license.html>
- Embeddable/venv gap discussion: <https://discuss.python.org/t/venv-or-virtualenv-with-embeddable-python/12030>
- "Tkinter deliberately excluded from embeddable" (zooba): <https://github.com/python/cpython/issues/99566>
- App Execution Aliases shadowing `python.exe`: <https://michielvoo.net/2023/02/12/windows-11-app-execution-aliases.html>, <https://stackoverflow.com/questions/58754860/cmd-opens-windows-store-when-i-type-python>, <https://superuser.com/questions/1919454/how-to-remove-python-exe-from-app-execution-aliases>
- pyenv-win: <https://github.com/pyenv-win/pyenv-win> · WinPython: <https://github.com/winpython/winpython>

**Packaging / signing**
- PyInstaller releases: <https://pypi.org/project/pyinstaller/> (6.22.3, 2026-09-12; GPLv2 + exception)
- Nuitka releases: <https://pypi.org/project/Nuitka/> (4.3.1, 2026-10-17; AGPLv3)
- PyInstaller AV false positives: <https://github.com/pyinstaller/pyinstaller/issues/5492>, <https://github.com/pyinstaller/pyinstaller/issues/6754>, <https://github.com/pyinstaller/pyinstaller/issues/7019>
- onefile vs onedir startup: <https://github.com/pyinstaller/pyinstaller/issues/4563>, <https://github.com/orgs/pyinstaller/discussions/8970>
- ML-heuristic AV detection on compiled binaries: <https://github.com/microsoft/apm/issues/487>
- Code signing options: <https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options>
- SmartScreen reputation: <https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation>
- Azure Artifact Signing (formerly Trusted Signing): <https://azure.microsoft.com/en-us/products/artifact-signing>
- "EV certs no longer grant instant reputation": <https://www.todesktop.com/blog/posts/windows-apps-psa-ev-certs-do-not-grant-immediate-reputation-anymore>

**EDSDK bindings**
- `pyedsdk` on PyPI (0.2.16, MIT, Alpha, `pywin32>=306`): <https://pypi.org/project/pyedsdk/>
- `pyedsdk` source — `loader.py`, `camera.py`, `live_view_stream.py`, `core/_functions.py`, `core/_types.py`, `core/_callbacks.py`, `core/_enums.py`, `core/_sdk.py`: <https://github.com/fmargall/pyedsdk>
- `Jiloc/edsdk-python` (64★, MIT, last push 2023-11-13; issues #7, #9): <https://github.com/Jiloc/edsdk-python>
- `chibiegg/python-edsdk-ctypes` (v0.0.1, pure ctypes): <https://github.com/chibiegg/python-edsdk-ctypes>
- `andrewrk/pyedsdk` (2010, 32-bit only): <https://github.com/andrewrk/pyedsdk>
- digiCamControl: <https://github.com/dukus/digiCamControl> · dev API: <http://www.digicamcontrol.com/doc/development/lib>
- pythonnet (embedding, threading, free-threaded from 3.14): <https://pythonnet.github.io/pythonnet/python.html>, <https://pythonnet.github.io/pythonnet/threading.html>

**Canon CCAPI**
- Latest CCAPI (1.4.0d, 2025-11-17): <https://developers.canon-europe.com/developers/s/article/Latest-CCAPI>
- CCAPI supported cameras + interfaces: <https://developercommunity.usa.canon.com/s/article/CCAPI-Supported-Cameras>
- "Ethernet only on 1D X Mark III": <https://developercommunity.usa.canon.com/s/article/Can-I-use-an-ethernet-connection-with-the-CCAPI>
- CAP (EDSDK & CCAPI) overview: <https://asia.canon/en/campaign/developerresources/camera/cap>

**Python runtime semantics**
- PEP 779 — free-threaded Python officially supported (3.14): <https://peps.python.org/pep-0779/>
- What's new in 3.14 (PEP 779 status, free-threading improvements): <https://docs.python.org/3/whatsnew/3.14.html>
- `ctypes` GIL-release semantics (`PyDLL` is the exception): <https://docs.python.org/3/library/ctypes.html>

**Wheel metadata** (version, licence, win_amd64 size — read from the PyPI JSON API on 2026-10-24):
`numpy`, `scipy`, `opencv-python`, `scikit-image`, `astropy`, `Pillow`, `PySide6-Essentials`, `PyQt6`,
`pywin32`, `fastapi`, `uvicorn` — `https://pypi.org/pypi/<package-name>/json`

**Internal cross-references**
- Brief 00 §8 (43 ms spawn floor; resident-process conclusion), §9 (toolchain audit — **needs the
  correction in §1.0 above**).
- Brief 02 §1.3 (EDSDK tree layout, 32/64 sibling dirs), §1.5 (verified property/command/event IDs,
  `DEVICE_BUSY`-means-bad-value), §1.6 (LV cadence, 2 MiB over-request), §3 (`gphoto2` has no
  Windows support).
- Brief 03 §4 (registration at ≤2 MP, cv2 warp costs) — the workload this brief's §5 latency numbers
  are measured against.

---

## 9. Reproduction notes

Everything tagged **[M]** or **[V]** was produced on this machine in a scratch directory on the
Windows side (`C:\Users\username\pyprobe`, `C:\Users\username\relo_test`) driving real Windows binaries
from WSL2. The measurement scripts are deliberately trivial so they can be re-run against the real
rig; the recipes, in order:

1. Inspect the existing interpreter: `uv --version`, `uv python list --only-installed`, `uv python dir`.
2. Venv + deps: `uv venv --python 3.11 .venv` then `uv pip install numpy opencv-python-headless pyinstaller`.
3. Spawn timing: `subprocess.run` around `python -c pass` / `python -c "import numpy"`, 7 runs, take the median.
4. Packaging: `python -m PyInstaller --noconfirm --clean --onedir|--onefile app.py`, then time the exe the same way.
5. Relocation test: `cp -r <uv python tree> <new dir>`, then run it **from a different cwd with no env vars** and check `sys.prefix`.
6. Vendoring: `uv pip install --python '<tree>\python.exe' --target '<tree>\Lib\site-packages' numpy opencv-python-headless`.
7. GIL test: start a pure-Python counter thread, block the main thread in `ctypes.windll.kernel32.Sleep(200)`, compare the counter's iteration count against an idle baseline of the same duration. A ratio near 1.0 means the GIL was released.

Caveat on the timings: the *parent* process is a Windows Python, but the shell driving it is WSL2, so
absolute numbers carry a few ms of boundary overhead. Relative comparisons (onedir vs onefile vs
vendored, bare vs numpy) are sound. Re-measure on the real rig before committing to a latency budget.
