# 01 — ASTAP as a plate-solving engine

**Status:** partially verified against the binary installed on this machine
(`C:\Program Files\astap`, **astap_cli.exe CLI-2025.10.11, MPL 2.0**). Items marked
✅ were reproduced locally; items marked ⚠️ come from documentation or source reading and
have *not* been confirmed here.

---

## 1. Use `astap_cli.exe`, not `astap.exe`

Two binaries ship in the same directory:

- **`astap.exe`** — a Lazarus/LCL GUI application. Even in batch mode it creates a form,
  can raise a tray icon and a popup notifier during the search spiral, and gates all
  `writeln` output on `isConsole`, which is false for a GUI build. It is not scriptable.
- **`astap_cli.exe`** — a true console program (`TCustomApplication`). Writes to stdout,
  no windows, no tray. ✅ Confirmed: `-h` produced clean multi-line output with exit 0.

There is **no `-headless` flag** — ✅ confirmed absent from the real help output. The CLI
binary *is* the headless mode.

Other flags that were suspected but **do not exist**: `-batch`, `-quit`, `-exit`,
`-pltsolve`, `-extend`, `-noise`. ⚠️ Note that PlateSolve2-compatible command lines were
removed in a recent version, so older tutorials found online describe a protocol this
build no longer speaks.

---

## 2. Verified CLI surface

✅ Reproduced verbatim from `astap_cli.exe -h` (CLI-2025.10.11):

| Flag | Arg | Meaning |
|---|---|---|
| `-f` | filename | Input image. Required. |
| `-r` | degrees | Radius of the square search spiral around the start position. |
| `-fov` | degrees | **Field size (see §3 — this is HEIGHT, not diameter).** `0` = auto. |
| `-ra` | hours | Start RA. Overrides the header. |
| `-spd` | degrees | South-pole distance = `dec + 90`. Overrides the header. |
| `-s` | int | Max stars to use (default 500). **Not** search radius, despite the letter. |
| `-t` | float | Quad tolerance (default 0.007). |
| `-m` | arcsec | Minimum star size, filters hot pixels (default 1.5). |
| `-z` | int | Downsample factor. `0` = auto. |
| `-check` | — | Check-pattern (CFA) filter. For raw OSC at 1×1 binning only. |
| `-d` | path | Star database directory. **Lowercase `d`** = path. |
| `-D` | abbrev | Force a star database, e.g. `d80`, `d50`, `g05`. **Uppercase `D`** = which DB. |
| `-o` | path | Output base path/name for `.ini` and `.wcs`. |
| `-sip` | — | Add SIP distortion coefficients. |
| `-speed` | auto\|slow | `slow` forces more overlap while searching. |
| `-wcs` | — | Write `.wcs` in Astrometry.net-compatible style. |
| `-log` | — | Write `.log` text file. ✅ |
| `-update` | — | Write the solution into the input FITS/TIFF header. |
| `-progress` | — | Log every progress step to stdout. ✅ |

Analyse-only modes (exit code encodes results — see §5): `-analyse snr_min`,
`-extract snr_min`, `-extract2 snr_min`.

**Option parsing:** single dash, and the `-d`/`-D` pair implies case sensitivity. ✅
Presumed to be Free Pascal's `TCustomApplication`, where the *last* occurrence of a
duplicated option wins. Treat that as ⚠️ unverified.

---

## 3. `-fov` semantics: the documentation is wrong

The help text says:

```
-fov diameter_field[degrees] {enter zero for auto}
```

It is **not** the diameter. ✅ Empirical proof — passing `-fov 2.13` for a frame whose
height is 2.13° produced:

```
Search radius: 15 degrees,
Start position: 23: 57  50.8, +89d 51  20
Image height: 2.13 degrees
```

The solver echoes back "Image height", and it matched the number passed to `-fov`. So
`-fov` is the **image height in degrees**, consistent with the internal
`fov_org := apply_arctan(height * |cdelt2|)`.

Compute it as:

```
fov_height_deg = 2 * atan( sensor_height_mm / (2 * focal_length_mm) ) * 180/pi
```

Worked example from the real frame on this machine (Canon 60D, 4.3 µm pixels, 400 mm,
5202×3464):

```
pixel scale = 206.265 * 4.3 / 400      = 2.217 "/px
height      = 3464 * 2.217 / 3600      = 2.13°
width       = 5202 * 2.217 / 3600      = 3.20°
```

Pass `2.13`, not the 3.85° diagonal.

⚠️ Alternative and better: write `CDELT1`/`CDELT2` (or `XPIXSZ` + `FOCALLEN`) into the
FITS header and let ASTAP derive the field. That removes a whole class of unit bugs and
avoids the need to know the optics at solve time.

⚠️ Note also that the docs' "diameter" wording may be a fossil of an older behaviour.
Treat any online guide that tells you to pass the diagonal as wrong for this build.

---

## 4. The `.ini` result file is the real interface

`astap_cli.exe` always writes a sidecar `.ini` next to the input, or at the `-o` base.
✅ Verified on the failure path. Actual bytes of a real failed solve:

```
^M$
PLTSOLVD=F^M$
CMDLINE=astap_cli.exe  -f C:\...\test1.fits -fov 2.13 -r 15 -z 0 -s 300 -o ... -log -progress^M$
ERROR=Not enough stars.^M$
```

Four things to notice, all ✅ verified:

1. **A leading blank line precedes `PLTSOLVD`.** The file literally begins with a CRLF.
   A naive parser that assumes line 1 is the first key will break. This blank line is
   present in the CLI build's failure output for backward compatibility.
2. **CRLF line endings**, ASCII, no BOM.
3. `CMDLINE` is echoed verbatim — genuinely useful for reproducing a bad solve.
4. **`DIMENSIONS` is not always present.** It is absent here because the solver aborted
   before image analysis. Do not depend on any key other than `PLTSOLVD` existing.

### Success path — now ✅ verified on real frames

Two real frames solved successfully on this machine (2026-10-24). The success `.ini` contains
**exactly 13 keys, in this order** — and **no `DIMENSIONS`**, even on success:

```
PLTSOLVD=T
CRPIX1 / CRPIX2      reference pixel = image centre, 1-based FITS convention
CRVAL1 / CRVAL2      RA/Dec (J2000) of that pixel, degrees
CDELT1 / CDELT2      deg/px  (CDELT1 negative, CDELT2 positive on both frames)
CROTA1 / CROTA2      image twist per axis, degrees
CD1_1 / CD1_2 / CD2_1 / CD2_2   full CD matrix, deg/px
CMDLINE              verbatim command line
[WARNING=...]        only when non-empty  <-- CAN APPEAR ON SUCCESS
```

Three things this settles:

- **`DIMENSIONS` is never emitted**, on success or failure. Drop it from any design that
  expected it.
- **`WARNING` appears alongside `PLTSOLVD=T`.** Real example:
  `WARNING=Warning scale was inaccurate! Set FOV=8.62d, scale=9.0", FL=99mm`. So the parse
  rule is: `exit==0 and PLTSOLVD==T` ⇒ solved, **and separately** surface `WARNING` — it means
  the header's optics disagree with the solution (a genuinely useful diagnostic, and a hint that
  the frame was cropped).
- `CRPIX` was `(1469, 979.5)` and `(2601.5, 1732.5)`, i.e. exact frame centres.

The `.ini` does **not** contain `FOV`, `ROTATION`, `RADESYS`, or a parity flag. Derive:

```
pixel_scale    = sqrt(CD1_1^2 + CD2_1^2)       # deg/px
position_angle = atan2(CD1_2, CD2_2)           # PA of the image +y axis, East of North
parity         = sign(CD1_1*CD2_2 - CD1_2*CD2_1)
```

> ⚠️ **`position_angle` uses `CD1_2`, not `CD2_1`.** An earlier revision of this brief said
> `atan2(CD2_1, CD2_2)`. That is wrong in general; it happened to look right on the test frame
> only because `CD1_2 ≈ CD2_1` for a near-pure rotation. For a true rotation matrix the two
> forms are **negatives** of each other, so the error is a sign flip. Use `CD1_2`. Full
> derivation and a numeric cross-check in [07 §2.1](07-altaz-reframing-spec.md).

Cross-check that catches sign errors: `PA_y ≈ −CROTA2`. Measured: `PA_y = 178.05°` vs
`−CROTA2 = +178.036°`.

`parity = −1` (negative determinant) on both real frames — that is the **normal** FITS
convention (RA increases leftward). `parity = +1` means a mirrored solution, which flips every
left/right instruction in the reframing UI.

A `.wcs` file is written **only on success**, and only with `-wcs`. The `.log` requires `-log`.
✅ Confirmed on the failure path: `.ini` and `.log` present, no `.wcs`.

⚠️ **The `.wcs` is not a minimal header — it is the input FITS header with WCS keys appended,
and it includes SIP coefficients by default** (`A_ORDER=3`, `A_*/B_*/AP_*/BP_*`) **even without
passing `-sip`.** An earlier revision claimed SIP was off by default. Observed otherwise. The
`.ini`, by contrast, carries no SIP terms at all — so if you need distortion terms you must read
`.wcs`, not `.ini`.

⚠️ A `.wcs` derived from a raw frame carries the *original* header with it (one real example had
`PROGRAM = 'Siril 1.4.3'`, `FILENAME = '...0063.cr2'`, `STACKCNT = 153`), so don't mistake those
inherited keys for ASTAP's own output. ASTAP adds `PLTSOLVD`, the WCS block, and a
`COMMENT ... Solved in 0.6 sec. Offset was 0.7". Mount offset RA=1.4", DEC=0.8"` line.

---

## 5. Exit codes

✅ Observed: `0` for `-h`, `1` for "no solution" (the `Only 0 stars found ... No solution
found!` case).

| Code | Meaning |
|---|---|
| 0 | solved |
| 1 | no solution (`.ini` has `PLTSOLVD=F`) |
| 2 | not enough stars detected |
| 16 | error reading image file |
| 32 | no star database found |
| 33 | error reading star database |
| 34 | `-update` failed to write |

⚠️ Codes 2–34 are documented but not all reproduced. The robust check is
**`exit == 0` AND `PLTSOLVD=T`** — belt and braces, because they can disagree.

⚠️ Codes >255 (from `-analyse`/`-focus`, which pack `HFD*1e6 + nstars` into the status)
cannot survive a Unix `$?`. Do not use those modes across the WSL boundary.

---

## 6. Star databases

✅ This machine: **D80 only** — 1476 tiles named `d80_####.1476`, 1.3 GB, installed
directly alongside the executables. No `.290` or `.001` tiles, so G05/W08 are absent.

How ASTAP finds them: the directory holding the executable, overridable with `-d`.
✅ Consistent with the install layout here.

⚠️ Selected automatically when `-D` is not given: `w08` above 20° field, `g05` above 6°,
otherwise the D-series. Each tile class has a hard maximum field — **5.14° for `.1476`
tiles (D50/D80)**, 9.53° for `.290`, 180° for `w08`. Exceeding it does not fail; ASTAP
*crops to the centre* and emits a `WARNING=` line.

✅ **Verified the hard way on real data (2026-10-24).** A genuine 100 mm / APS-C light frame
(`light_00001.fit`, 5202×3464, 180 s) has an **8.53°** tall field. With only D80 installed,
hinted solving against it was **unreliable — 2 of 5 `-fov` values solved, 3 failed**, while the
same frame solved fine with auto-FOV in 3.98 s. ASTAP's own warning confirms the cause:

```
WARNING=Warning scale was inaccurate! Set FOV=8.62d, scale=9.0", FL=99mm
Image height: 6.33 degrees     <-- cropped from 8.62 to fit D80
```

And because it cropped, **`CRVAL` then describes the cropped sub-image, not the user's frame** —
which silently corrupts anything that consumes the solution (reframing arrows, WCS bookkeeping).
A ~2° field frame at 385 mm solved cleanly with no warning. So:

- the database ceiling is **not theoretical**, it silently degrades results at wide focal lengths;
- it must be detected (compare solved pixel scale × width against expected field) rather than
  assumed;
- it argues for **choosing the database from the computed field** before solving, not after.

A 5202×3464 frame at 100 mm is an ordinary wide-field DSLR shot, so this is the *common* case for
our user, not the edge case.

**This is a real constraint for our users.** With a 100–400 mm lens on APS-C the field
runs from 2.1° to 8.6° tall. At the long end D80 is correct; at 8.6° D80 will silently
crop. Shipping guidance should tell users to install **D80 + G05** (and W08 if we ever
support wide lenses below ~85 mm).

⚠️ Duplicate/legacy databases (H17/H18/V17/…) still load but emit `WARNING=Old database!`.

---

## 7. What makes a solve succeed — and our test frames failed

The two real N.I.N.A. FITS frames on this machine (Canon 60D, 400 mm, 2 s, DEC +89.85)
both failed:

```
0 stars found of the requested 300. Background value is 15304. ...
Only 0 stars found in image. Abort
```

✅ Verified. These are N.I.N.A.'s "Snapshot" frames from a `PlateSolver/Failed/` folder —
N.I.N.A. was failing them for the same reason. The frames are effectively starless
(2 s exposure, unfocused or obstructed).

**The lesson generalises and is important:** *"0 stars found" and "wrong patch of sky"
are the same exit code, `1`.* A pipeline that treats exit 1 as "target not found" will
misfire constantly, because the actual cause is far more often a bad frame (cloud, dew,
focus, cap on, too-short exposure, wrong lens). **Frame quality gating — star count,
HFD/FWHM, background level — must sit in front of the solver**, not behind it.

Useful diagnostic: `-analyse 20` returns `HFD_MEDIAN=` and `STARS=` directly. ✅ On the
bad frames it reported `HFD_MEDIAN=99.0`, `STARS=0` — a perfectly good quality gate for
free, in one process spawn.

⚠️ **But `-analyse` is unreliable on raw, uncorrected light frames.** On a real 180 s light
(RGB, uncalibrated, heavy skyglow) it reported `HFD_MEDIAN=99.0, STARS=0` — the same output it
gives for a blank frame — yet the *solver* found 18 stars immediately and 4750 after adaptive
thresholding. On the Siril-processed stack of the same target it correctly reported
`HFD_MEDIAN=6.1, STARS=545`.

So: `-analyse` is a good gate for **already-calibrated/stacked** frames, and a misleading one
for raw subs. For the live loop, star count and focus ought to come from our own detector on
data we already have in memory, rather than a second process spawn whose thresholds we cannot
control.

⚠️ `-analyse` also packs its result into the exit code as `HFD*1e6 + nstars`, which cannot
survive a Unix `$?`. Measured: a run reporting `HFD=6.1, STARS=545` (true code ≈ 6 100 545)
surfaced as **33** — i.e. pure truncation modulo 256, indistinguishable from ASTAP's real
exit code 33 ("error reading star database"). Never read `-analyse` results from the exit code
across the WSL boundary; parse the stdout or the CSV.

---

## 8. Speed

✅ Measured here: a failing solve that aborted after star detection took **288 ms wall**,
including process spawn. The harness floor is **~43 ms** per `cmd.exe` round trip (see
00), so spawn cost is a meaningful share of any very fast solve.

### Measured here (2026-10-24, 60D frames, `astap_cli` CLI-2025.10.11)

| Case | Frame | Wall clock |
|---|---|---|
| Auto-FOV (`-fov 0`), `-r 60` | 2937×1958, 1.25° field | **4.0 s** |
| Auto-FOV (`-fov 0`), `-r 60` | 5202×3464, 8.5° field | **8.2 s** |
| **Hinted** (`-fov` + `-ra`/`-spd`, `-r 3`) | 2937×1958 | **0.74 s** (3 runs: 745/732/737 ms) |

So the hinted/full-blind ratio is roughly **10×**, and the 43 ms `cmd.exe` spawn overhead (see 00)
is a small share of even the fast path. A ~0.7 s cadence is comfortably inside a real-time loop;
`-fov 0` is not.

⚠️ Also observed: `-z 0` (auto) picked **1×1** binning on the 1958-px-tall frame and **2×2** on
the 3464-px-tall frame, consistent with the >2500 px threshold.

⚠️ From published benchmarks and source reading, not measured here:

| Scenario | Typical |
|---|---|
| Hinted solve (RA/Dec + accurate `-fov`, small `-r`) | **0.15–0.4 s** |
| `-z 0` vs `-z 1` | marginal difference |
| `-fov 0` (auto-detect) | several attempts, up to ~5× slower |
| Blind (`-r 180`) | tens of seconds to minutes |

The dominant cost is the **search radius**, not the solver. Cost scales with roughly
`(radius / fov)²` positions in the spiral. `-r 180` at a 2° field is thousands of
positions; `-r 3` is single digits.

**Design consequence:** a sub-second real-time loop is achievable *only* if we always feed
back the previous solution as a hint (`-ra`, `-spd`, tight `-r`) and pass an accurate
`-fov`. Blind solving is a recovery path, not the steady state. Our loop should be:

```
first frame   → blind-ish (-r 30), accept the latency once
subsequent    → hint from previous WCS, -r 2..5, sub-second
after a nudge → widen to -r 10..15 while the user is hand-aiming
```

✅ Also verified: `-z 0` (auto) chose **2×2 binning** on a 3464-px-tall frame. That is the
documented behaviour above ~2500 px, and it is what we want — solving does not need full
resolution, and it keeps the loop fast.

---

## 9. RAW input — a trap

⚠️ **`astap_cli.exe` does not read RAW files at all.** It reads FITS, TIFF, PNG, JPEG, and
PNM. The GUI `astap.exe` reads CR2/CR3/NEF/ARW via bundled `dcraw.exe` and
`unprocessed_raw.exe` — ✅ both are present in the install directory here — but the CLI
build has no such path.

Connected to this: when the GUI does convert a RAW, it writes the intermediate
`<name>.CR2.fits` **into the input directory**. So the input directory must be writable.
✅ Consistent with the read-only install directory observed here (`dr-xr-xr-x`), which is
another reason to keep data under `C:\ministar\data` and never under Program Files.

**Design consequence:** the camera-capture layer must hand the solver a **16-bit mono
FITS**, not a `.CR2`. That means we own raw→FITS conversion, or we capture to FITS
directly. Doing our own conversion is likely the better path anyway — we need the pixels
for live stacking regardless, so a single decode serves both consumers and the solver
never sees a RAW.

⚠️ Unverified: whether the solver prefers debayered or CFA-mosaic input. `-check` exists
for the 1×1 CFA case, implying mosaic input is supported. Worth measuring both on a real
frame; debayered stars are cleaner and we need debayered pixels for stacking anyway.

---

## 10. Windows/WSL mechanics that bit us

✅ All verified:

- **Quoting.** `cmd.exe /c 'cd /d "C:\Program Files\astap" && astap_cli.exe -h'` fails with
  `The filename, directory name, or volume label syntax is incorrect.` A `.bat` wrapper
  works fine. **Wrap, don't inline.**
- **Paths are not translated.** Interop passes argv verbatim; the binary cannot open
  `/mnt/c/...`. Use `wslpath -w`.
- **Relative paths in `-o`** resolve against the child's cwd, which may be `C:\Windows`
  (see 00). Always pass absolute Windows paths.
- **File locking.** ⚠️ Documents are written with a single write, so a poller should not
  see partial files — but the `.wcs` save is wrapped in a "sometimes error using APT,
  locked?" exception handler in the source, so a held handle can silently lose it.
  Give each solve its own scratch directory and delete the stale `.ini` before running.

The robust loop wrapper is therefore:

```
1. rm -f  C:\...\scratch\s_NNN.ini
2. astap.bat -f <abs win path to fits> -o C:\...\scratch\s_NNN  [hints]
3. rc = exit code
4. if rc == 0 and .ini exists and PLTSOLVD==T  → parse CD matrix
5. else → classify: 0 stars (bad frame) vs no solution (wrong sky)
```

---

## 11. Landscape

⚠️ All of this section is from research, not local verification.

- **No library interface.** ASTAP is a monolithic Free Pascal application; there is no
  DLL, no API, no embeddable form. It is driven as a subprocess or not at all.
- **The CLI+.ini contract is a de facto standard.** N.I.N.A., CCDciel, INDIGO, and Ekos
  all speak it, and N.I.N.A.'s reference implementation deliberately **ignores the exit
  code** and trusts only `PLTSOLVD` plus the CD matrix. Independent reimplementations
  (`arcsec` in Rust, `seiza` in Go) clone the same flag set and the same `.ini` keys
  specifically for drop-in compatibility.
- **That is good for us.** If we code to the ASTAP CLI contract rather than to ASTAP
  itself, we can swap the solver binary later — including onto one of the faster
  reimplementations — without touching our pipeline. Define a narrow solver interface and
  keep ASTAP behind it.
- Alternatives worth knowing: `astrometry.net` (robust, much slower, awkward on Windows),
  `tetra3` (pure-Python lost-in-space solver, 10–30° fields, ideal for our focal range but
  needs numpy and has no Windows story of its own), PlateSolve3 (free, closed, its own
  protocol).

---

## 12. Open questions

- ⚠️ **Confirm the success-path `.ini`** — every key in §4 — with a real, focused,
  star-bearing frame. *This is the highest-value next verification step.*
- ⚠️ Debayered vs CFA-mosaic input: speed and reliability difference.
- ⚠️ Real hinted-solve latency on laptop-class hardware, warm and cold, to size the loop.
- ⚠️ Whether exit codes 2/16/32/33/34 survive WSL interop intact.
- ⚠️ Non-ASCII and >260-char paths (French/Japanese folder names appear in the source as a
  known pain point).
- ⚠️ Behaviour when two solves run concurrently against the same star database — parallel
  solving may be worth it for a multi-frame loop.
