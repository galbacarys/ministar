# 06 — Should the stacking math live in Siril's CLI (or ASTAP/DSS) instead of numpy?

**Scope.** The alignment + incremental-stack + display loop for hundreds of 2–15 s untracked
subs, rotation included. Question asked: is delegating the *math* to an external tool's
command line — Siril first — actually a good idea, or should we own the numerics?

**Labelling.** Inherited from 03 so the two briefs read together: **[M]** measured for a brief
on this laptop (AMD Ryzen AI 7 350, 16 threads, 15 GB RAM, Linux; numpy 2.5.4, OpenCV 5.0.0,
sep 1.4.1, skimage 0.26.0, astroalign 2.6.2). **[D]** derived/estimated here. **[C]** cited.
**[U]** UNVERIFIED — plausible, not confirmed in this pass. Everything marked [M] was run on
Linux, not the Windows target; expect ±20 % on a Windows laptop of similar class.

---

## 0. Verdict, up front

| Candidate | Rotation? | Live/incremental? | Headless scriptable? | Licence | Verdict |
|---|---|---|---|---|---|
| **Siril `siril-cli.exe`** (1.4.x) | **Yes** — global star alignment; `-transf=` accepts shift, similarity, affine or homography (default homography) | **Yes, but**: `start_ls`/`livestack` is a *running weighted mean of a 2-image internal sequence*, writes a full FITS per frame, no scriptable progress handshake in `-s` mode | Yes (`-s`, `-s -` stdin, `-p` named pipes with `ready`/`status:` protocol) | GPL-3.0-or-later | **Not the fast path. Excellent as the *final* restacker**, or as a fallback when our own registration fails. |
| **ASTAP `astap_cli.exe`** | n/a | **No** — CLI is solve-only; `-stack` is a *GUI* option that opens the live-stack tab | Solve only | MPL-2.0 | Keep for what we already use it for: plate solving + `-analyse` quality gate. No stacking. |
| **`ortegaalfredo/astap-command-line` fork** | [U] | Sigma-clip batch only | JSON command file | LGPL-2.1 claimed vs MPL-2.0 upstream | **Dead** (last commit 2021-10-01, 32-bit Windows binaries). Reject. |
| **DeepSkyStackerCL.exe** | Yes (star-pattern, offsets+rotation) | **No** — one batch run = one output TIFF, settings come from the GUI's stored settings | Yes, but batch-shaped | BSD-3-Clause | **Reject for the loop**; optionally an "export final stack to DSS" nicety. BSD is the licence upside. |
| **In-process (numpy/OpenCV/sep/astroalign)** | Yes (`estimateAffinePartial2D`, astroalign) | **Yes — O(1) per frame** | n/a | MIT/BSD/Apache/LGPL (sep) | **This is the fast path.** ~150–250 ms/frame at 24 MP is achievable [M/D], i.e. inside a 1–2 s cadence with room for capture and display. |

**Recommendation in one sentence:** own the fast path (register on a ≤2 MP downsample, warp +
accumulate at full res, Welford/weighted mean in float32), and use **Siril as an optional
"high-quality restack" pass at end-of-session** (or on demand), driven from a generated `.ssf`
script, with the user's own Siril install — because the thing Siril is *good* at (global
registration + winsorised σ-clip over the whole sequence, with per-frame quality filters) is
exactly the thing that costs O(N) work per call and therefore cannot be the per-frame loop.

Why not Siril for the loop, in one paragraph: the *only* headless incremental path Siril has
is `start_ls` + `livestack <file>`, and reading the source (§1.7) it is a **running weighted
mean** — `stack_mean_with_rejection` over a 2-image sequence, i.e. rejection over two samples
is a no-op, `NBSTACK_WEIGHT` weighting, plus a **full FITS write of the accumulating image on
every frame**. That is a strictly worse estimator than what we can write in 40 lines of numpy
(Welford mean + variance so ±kσ per-pixel rejection actually works from N=8 upward), and it
costs a 48 MB-per-frame disk round-trip we would otherwise not pay.

---

## 1. Siril command line

### 1.1 Binaries and flags — the flags you listed mostly do not exist

Siril ships **two binaries from one source tree** [C: `src/main-cli.c`, source comment "the
application is divided into 2 binaries"]. `siril-cli.exe` is a real, distinct, headless build
(not a flag on the GUI). On Windows it lives next to the GUI: `C:\Program Files\Siril\bin\`
[C: user report running `start siril-cli.exe -d ... -s ...` from that directory].

Verified option table — this is the *entire* `main_option[]` array in `src/main-cli.c`
(master, i.e. 1.4/1.5 line) [C]:

```c
{ "directory", 'd', G_OPTION_ARG_FILENAME }   // change working directory
{ "script",    's', G_OPTION_ARG_FILENAME }   // run .ssf script; "-" = read script from stdin
{ "initfile",  'i', G_OPTION_ARG_FILENAME }   // alternate config file
{ "pipe",      'p', G_OPTION_ARG_NONE     }   // console mode over named pipes
{ "inpipe",    'r', G_OPTION_ARG_FILENAME  }  // read-pipe path (commands in)
{ "outpipe",   'w', G_OPTION_ARG_FILENAME  }  // write-pipe path (messages out)
{ "sync_spcc",  0,  G_OPTION_ARG_NONE     }   // refresh SPCC mirror list, then exit
{ "format",    'f', callback -> print formats and EXIT }
{ "offline",   'o', callback -> start offline }
{ "version",   'v', callback -> print version and EXIT }
{ "copyright", 'c', callback -> print copyright and EXIT }
```

So, against the brief's assumed list:

| Assumed | Reality |
|---|---|
| `-s` / `--script` | ✅ correct. `-s -` reads the script **from stdin** [C] |
| `-d` / `--directory` | ✅ correct |
| `-c` / `--command` | ❌ **does not exist** — `-c` is `--copyright` and *exits*. There is no one-shot "run this command" flag; use `-s -` or a temp `.ssf` |
| `-f` / `--fitseq` | ❌ **does not exist as a CLI flag** — `-f` is `--format` and *exits*. `-fitseq` *is* real, but as an argument to the **`convert` command**: `convert basename [-debayer] [-fitseq] [-ser] [-start=] [-out=]` [C]. Easy to conflate; do not pass it to the executable. |
| `-o` | ❌ not an output flag — `-o` is `--offline` (skip network fetches; use it) |
| `--version` | ✅ `-v/--version`, prints and exits |
| `-p` | ✅ named-pipe mode, but needs `-r`/`-w` paths |
| `-h/--help` | ✅ comes from GLib (`GApplication`), not from Siril's array |

Debian's man page synopsis matches: `siril-cli [-i conf_file] [-f] [-v] [-c] [-h] [-p]
[-d working_directory] [-s script_file] [-r input_pipe_path] [-w output_pipe_path]
[image_file_to_open]` [C].

**Display:** no display needed. `siril_app_activate()` in the CLI sets `com.script = TRUE;
com.headless = TRUE;` before doing anything [C], and the docs say the CLI "does not even
require having a display" [C]. It is still a GTK/GLib program: on Windows it needs the DLLs
shipped in `bin\` (use the installer or the portable zip's own tree, don't copy the exe alone).

**Config side effects:** it runs `checkinitfile()` at startup and *creates* a settings file if
absent, and (this build) kicks off `initialize_python_venv_in_thread()` [C]. For a reproducible
embedding, pass `-i <our own conf file>` and `-o`. Note `checkinitfile()` failing →
`exit(EXIT_FAILURE)` — a corrupted config makes the process die at startup, so always check
the exit code and the first stdout line ("Welcome to Siril X.Y.Z - CLI").

### 1.2 Three ways to drive it, and the one that is actually a protocol

1. **Script file** (`-s file.ssf`) — fire-and-forget batch. Process exits at EOF; no per-command
   acknowledgement. Good for the end-of-session restack.
2. **stdin** (`-s -`) — `siril_input_stream_from_stdin()` → `execute_script(stream)` [C]. You can
   keep one process alive and feed lines. **UNVERIFIED:** whether lines are executed as they
   arrive or after a buffer fill, and whether anything is printed on completion of each command
   in script mode (nothing in the docs promises a token). On Windows, `main-cli.c` calls
   `ReconnectIO(1)` in this path, so the console handles are reattached — good for reading
   stdout from a redirected child.
3. **Named pipes** (`-p -r \\.\pipe\... -w \\.\pipe\...`) — the only mode with a real handshake.
   `src/core/pipe.c` implements Win32 named pipes with `CreateFile`/`WriteFile` behind
   `#ifdef _WIN32` (POSIX `mkfifo` in the `#else`) [C], and emits framed messages:

   ```
   ready\n                      // pipe is up, accepts commands
   log: <text>\n                // log lines mirrored into the pipe
   status: starting <cmd>\n
   status: success <cmd>\n
   status: error <cmd>\n
   status: busy\n | status: exit\n
   <progress text>              // PIPE_PROGRESS passthrough
   ```
   plus two control verbs: `ping` (returns `status: success ping` or `status: busy`) and
   `cancel` [C]. **This is the only sync mechanism Siril gives you**, and it makes a persistent
   "Siril as a service" architecture technically possible on Windows.

   Caveat: `-p` is *also* the mode where Siril stays alive waiting for commands, so you need one
   process for the whole session; and the docs themselves warn that after `start_ls` **only**
   `livestack`, `stop_ls` and `exit` are accepted [C].

### 1.3 Scripting language (`.ssf`) and what is headless-legal

`.ssf` = plain text, one command per line, `#` comments, `requires <min> [obsolete]` version
guard, space-delimited args. Quoting rule, verbatim from the docs: you "need to quote the entire
argument not just the string. So for example you should use `command "-filename=My File.fits"`,
**not** `command -filename="My File.fits"`" [C]. Scripts abort on the first command that returns
an error [C].

Headless legality is per command: the docs mark each command *scriptable* (✅) or
non-scriptable (🚫). Checked in 1.4.4 docs [C]:

| Command | Scriptable | Notes |
|---|---|---|
| `requires`, `cd`, `convert`, `calibrate`, `seqsubsky`, `register`, `seqapplyreg`, `stack`, `stackall`, `load`, `save`/`savetif*`, `autostretch`, `asinh`, `histo`, `exit` | ✅ | all fine headless |
| `findstar`, `setfindstar`, `setcpu`, `setmem`, `set16bits`/`set32bits`, `setcompress`, `setext`, `link`, `select`/`unselect` | ✅ | our knobs: thread cap, bit depth, compression, sequence naming |
| `seqstack` | — | **not in current docs**; the sequence staker is `stack` (plus `stackall`). Do not write `seqstack` in a new script. |
| `load_seq`, `ddp`, `disto`, `reloadscripts`, `clear`, `clearstar`, `show`, `setmag`, `tilt`, `inspector`, `ls`, `addmax` | 🚫 | GUI-only (note `reloadscripts` explicitly "may not be used headless") |

Minimal end-of-session restack script that we can generate verbatim [C syntax]:

```
requires 1.4.0
cd C:/Users/x/session_2026-10-24/_stack
convert light -debayer -out=seq_          # or: link seq_   (frames already FITS)
setfindstar -relax -roundness=0.5         # trailed 2-15 s stars are not round
seqsubsky seq_ 1 -samples=20              # polynomial degree 1 -> writes bkg_ prefix
register bkg_seq_ -transf=similarity -minpairs=10 -maxstars=400 -interp=lanczos4
seqapplyreg bkg_seq_ -framing=cog -filter-fwhm=2.5k -filter-round=0.85 -filter-nbstars=30
stack r_bkg_seq_ rej w 3 3 -norm=addscale -weight=wfwhm -out=final -32b
exit
```
(Every flag above is from the documented syntax in 1.4/1.5; the *combination* and the prefix
chaining (`seq_` → `bkg_seq_` → `r_bkg_seq_`) are ours. Note `convert` without `-fitseq` produces
a multi-file FITS sequence; `-fitseq` produces one FITS cube.)

### 1.4 Registration: what `register` actually does

Documented syntax (1.4.4) [C]:

```
register sequencename [-2pass] [-selected] [-prefix=] [-scale=]
register sequencename ... [-layer=] [-transf=] [-minpairs=] [-maxstars=] [-nostarlist] [-disto=]
register sequencename ... [-interp=] [-noclamp]
register sequencename ... [-drizzle [-pixfrac=] [-kernel=] [-flat=]]
```

- **Algorithm:** "The global matching is based on triangle similarity method … [Valdes1995].
  Our implementation is based upon the program `match` … Then RANSAC is used on the star lists
  to further reject outliers and determine the projection matrix" [C]. Feature detection is
  Siril's own PSF fitter (Dynamic PSF), not SExtractor; from the algorithm table: *Global →
  Dynamic PSF → triangles matching + RANSAC → new sequence: yes* [C].
- **Rotation: yes, and it is the default**, because `-transf=` defaults to **homography**:
  "`-transf=` specifies the use of either `shift`, `similarity`, `affine` or `homography`
  (default)". For our geometry **`-transf=similarity` is the right choice** — homography with a
  few dozen stars over-fits (and field rotation on a fixed tripod is a rotation, not a
  perspective warp).
- **`-interp=`** (not `-interpolation=`): `{none|nearest|cubic|lanczos4|linear|area}`; if no
  `-interp=` is given "the transformation is forced to shift" — i.e. **if you ask for rotation
  you must also pass an interpolation method** [C, forum + docs].
- **`-minpairs=`** minimum star pairs with the reference frame, otherwise the frame is dropped
  and *excluded from the sequence* (silently-ish: it lands in the sequence as unselected) [C].
  This is a free quality gate.
- **`-maxstars=`** 100–2000, more = more accurate, slower [C]. `-nostarlist` stops the per-frame
  `.star` side files (worth passing: they are large and we don't consume them).
- **Whole sequence, reference frame, two-pass:** `register` always works on a sequence
  (`-2pass` = compute transforms only + pick the best reference by quality/framing; then
  `seqapplyreg` writes the images; `-selected` registers only selected frames). There is **no
  "register one frame against a given reference" command**. That is the structural blocker for
  an incremental loop: the unit of work is a sequence, and the reference is chosen by Siril.
  (`setref` exists but is a scriptable selector for the *sequence's* reference index, not a
  per-frame registration.)
- **Trailed stars are a real risk here.** `SETFINDSTAR` has `-roundness=` (min roundness, 0–0.95)
  and an upper bound `-maxR`, and "PSF is applied and Siril rejects all detected structures that
  don't fulfil a set of prescribed detection criteria" [C]. At 14–85 mm with 2–15 s subs, stars
  are *streaks* (doc 03 §1.3: ≤2 px of trail means 50 mm → 2–3 s, 135 mm → ~1 s). The escape
  hatch is documented and explicit: **`-relax` "relaxes the checks … to allow objects not shaped
  like stars to still be accepted"** [C]. Any Siril-based path for this project must set
  `setfindstar -relax` (and probably lower `-roundness`), or registration will quietly fail on
  exactly the frames we take. **[U]** how well triangle+RANSAC on elongated sources behaves at
  the field edges when rotation is up to ~0.25°/min.

### 1.5 Stacking: `stack` — and where the quality filters moved

Documented syntax (1.4.4) [C]:

```
stack seqfilename
stack seqfilename { sum | min | max } [-output_norm] [-out=filename] [-maximize] [-upscale] [-32b]
stack seqfilename { med | median } [-nonorm, -norm=] [-fastnorm] [-rgb_equal] [-output_norm] [-out=] [-32b]
stack seqfilename { rej | mean } [rejection type] [sigma_low sigma_high] [-rejmap[s]] [-nonorm, -norm=]
      [-fastnorm] [-overlap_norm] [-weight={noise|wfwhm|nbstars|nbstack}] [-feather=]
      [-rgb_equal] [-output_norm] [-out=filename] [-maximize] [-upscale] [-32b]
```

- `-rej=` does **not** exist as a `-flag`; rejection is **positional**: `stack seq rej w 3 3`
  where the type is one of `n[one] p[ercentile] s[igma] m[edian] w[insorized] l[inear]
  g[eneralized] [m]a[d]` and the two numbers are σ_low σ_high; default type is Winsorized [C].
- `-norm=` exists: `add`, `mul`, `addscale`, `mulscale`, `-nonorm`; default = additive-with-scale
  [C]. Not `additive_scaling` — that is the internal enum name.
- `-weight=` exists: `noise | nbstack | nbstars | wfwhm` [C].
- **Quality gate / "not best-N %":** the `-filter-*` family exists but in 1.4 it is documented
  on **`seqapplyreg`**, not on `stack`:
  `[-filter-fwhm=value[%|k]] [-filter-wfwhm=...] [-filter-round=...] [-filter-bkg=...]
   [-filter-nbstars=...] [-filter-quality=...] [-filter-incl[uded]]`, where the value is
  *absolute* (`0.9`), *percent of best* (`80%`) or *k·σ* (`2.5k`) [C]. In **1.2** the `stack`
  syntax did carry a `[filtering]` placeholder ("The output images can be rescaled … filtering
  being some of these …") [C]. **UNVERIFIED:** whether 1.4's `stack` still parses `-filter-*`
  for backwards compatibility (the strings `filter-fwhm` do not appear in `src/stacking/stacking.c`,
  which suggests the filters are applied via the generic sequence-args filtering hook instead).
  Either way: **the documented, future-proof way to filter is `seqapplyreg -filter-*`** (or
  `select`/`unselect` on a criterion we compute ourselves).
- Also note: `-maximize` (bounding box across the whole sequence) and `-upscale`; `-out=` writes
  `.fit`; `-32b` for float output; `-feather=` to fade rotated-in borders — which is *exactly*
  the coverage-map problem doc 03 §6(e) flags.

### 1.6 Live stacking: exists, headless-capable, and thinner than it looks

Documented surface [C]: `start_ls [-dark=] [-flat=] [-rotate] [-32bits]` then
`livestack <filename>` per frame, then `stop_ls`. "Default processing will use shift-only
registration and 16-bit processing because it's faster" — **rotation is opt-in (`-rotate`)**,
which is backwards for us. After `start_ls`, only `livestack`/`stop_ls`/`exit` are accepted.

Source-level truth (`src/livestacking/livestacking.c`, master) [C]:

- The **unit of registration is a 2-image sequence**: the accumulating result
  (`live_stack_00001.fit`) as reference + the new frame. Per frame it builds a `sequence` with
  `seq.number = 2`, then `start_global_registration()` → `register_star_alignment` with
  `reg_type = shift_only ? SHIFT_TRANSFORMATION : SIMILARITY_TRANSFORMATION`. So: **similarity
  (translation + rotation) when `-rotate`, shift-only otherwise — and never anything smarter**
  (no re-anchoring of the reference, no re-computation of the reference quality).
- The stack call is `main_stack()` with
  `method = stack_mean_with_rejection`, `nb_images_to_stack = 2`,
  `weighting_type = NBSTACK_WEIGHT`, `normalize = ADDITIVE_SCALING`, `lite_norm = TRUE`.
  **Rejection over 2 samples is a no-op** — there is no outlier removal in headless live
  stacking. (The `-weight=nbstack` option is documented as "useful for live stacking" [C]: the
  old stack gets weight N, the new frame weight 1 — a running weighted mean, i.e.
  `acc = (N·acc + x)/(N+1)`.)
- **Disk cost per frame is structural:** every frame either is symlinked (FITS input) or is
  **written out as a new FITS** (`live_stack_%05d.ext`) after calibration/debayer, and the
  result is written to `live_stack_00001.ext` each iteration (overwrite). At 6000×4000×16 bit
  that is ~48 MB write + ~48 MB read per frame, before Siril's own read of the input.
- Per-frame timing *is* logged (`"Time to process the last image for live stacking: %s"` and
  debug-level `show_time_msg(..., "registration"/"stacking")`) [C], but `siril_log_info` /
  `siril_log_debug` visibility at default verbosity is **[U]**; if we ever drive this, sync on
  `-p` `status: success livestack` rather than on log lines.

**Incremental by re-running `stack` on a growing sequence**: yes, and it is the honest way to
describe the cost. `register` on N frames re-does star detection on all N unless you keep the
`r_`-prefixed registered sequence and only register the new frame (which Siril cannot do — the
sequence reference would change, so the old transforms are in the old reference frame). The
naive loop is therefore **O(N²)**: 200 frames × 200-frame re-registration. See §6 for the
numbers. This is the single strongest technical argument against "just call Siril every frame".

### 1.7 Performance

| Item | Number | Source |
|---|---|---|
| Full pipeline (convert/calibrate → register → stack), 24 MP lights | **~50 frames/minute ≈ 1.2 s/frame** on a 24-core i9 laptop with NVMe | [C] discuss.pixls.us/t/46135 |
| Siril 1.2.0 live stacking, ASI2600MC FITS (~26 MP) | **≈25 s per image** — vs DSS Live ≈15 s, SharpCap ≈4 s, same data | [C] theskysearchers.com/viewtopic.php?t=31542 |
| Process startup | UNVERIFIED; the CLI does locale/config/converters/colour-profile init and starts a Python-venv thread before running the script → assume **0.5–3 s**, so never spawn per frame | [U] from `main-cli.c` |
| Threading | OpenMP (`omp_set_max_active_levels(2)`, FFTW threads); parallel *images* bounded by a memory estimate, plus per-image OpenMP threads. `setcpu <n>` caps it | [C] `main-cli.c`, `src/core/processing.c`, `setcpu` |
| GPU | **None.** CPU-only | [C] |
| Memory | Siril computes required RAM per sequence and reduces parallelism to fit; overshoot ⇒ crash/OOM (their bug tracker has such reports) | [C] siril.org release notes 1.0.0 |

The 1.2 s/frame and the 25 s/frame figures are two orders of magnitude apart because they
measure different things (batch script on fast iron vs live stacking on unspecified hardware,
probably with rotation, 32-bit and a slow disk). Both are consistent with the source reading:
the *stack* step is cheap (2 images), the *FITS round-trips* and the *rotation resampling* are
not. Treat "Siril can do live stacking at a few seconds per frame on a good laptop" as the
realistic planning number, and note it is **2–10× too slow for a 1–2 s cadence** and costlier in
disk churn than the whole rest of our pipeline.

### 1.8 Licence: GPL-3.0-or-later, and what it means for us

Siril source headers say "GNU General Public License … either version 3 … or (at your option)
any later version"; the repo and Wikipedia summarise it as GPL-3.0 [C]. Consequences:

- **Calling `siril-cli.exe` as a subprocess is not, by itself, a licensing problem.** The FSF's
  own test is the *mechanism and semantics of communication*: "By contrast, pipes, sockets and
  command-line arguments are communication mechanisms normally used between two separate
  programs. So when they are used for communication, the modules normally are separate
  programs. But if the semantics of the communication are intimate enough, exchanging complex
  internal data structures, that too could be a basis to consider the two parts as combined
  into a larger program." (GPL FAQ, *What is the difference between an "aggregate" and other
  kinds of "modified versions"*, #MereAggregation) [C]. And: "A main program that uses simple
  fork and exec to invoke plug-ins and does not establish intimate communication between them
  results in the plug-ins being a separate program." (#GPLPlugins) [C].
  → **Our Python app may stay proprietary** if it talks to Siril only via argv, files and the
  pipe protocol (§1.2). Two things would break that: (a) loading Siril's Python module
  (`sirilpy`, which uses a socket + **shared memory**, and the same FAQ says "Using shared
  memory to communicate with complex data structures is pretty much equivalent to dynamic
  linking" [C]); (b) linking `libsiril`/its FITS or stacking objects into a Python extension.
  **Don't do either.**
- **Bundling the binary is allowed but is a *distribution* obligation, not a licence infection.**
  "The GPL permits you to create and distribute an aggregate, even when the licenses of the
  other software are nonfree or GPL-incompatible. The only condition is that you cannot release
  the aggregate under a license that prohibits users from exercising rights that each program's
  individual license would grant them." (#MereAggregation) [C]. In practice, shipping Siril in
  our installer means: ship/offer the **complete corresponding source** of that exact Siril
  build (or a written offer valid 3 years), ship the GPL-3 text and Siril's notices/copyright,
  do not add restrictions (no EULA clause forbidding reverse engineering of the bundled parts),
  and do not use the Siril marks to imply endorsement. GPLv3 §6's "Installation Information"
  duty applies to *User Products* (consumer devices), not to a Windows app, but **[U]** — get a
  lawyer's eye before shipping any bundle in a paid product.
- **Cheapest compliant path: don't bundle.** Detect a user-installed Siril (`HKLM\...\Siril`,
  `C:\Program Files\Siril\bin\siril-cli.exe`, `%LOCALAPPDATA%\Programs\Siril`, PATH), else show
  a "install Siril 1.4.x from siril.org to enable high-quality restacking" prompt with a deep
  link. This also shifts version-support risk onto a documented "requires ≥1.4, tested on 1.4.4"
  check, which we do with `requires` in the script and `-v` at detection.
- The **portable UCRT64 zip** on siril.org [C] is the friendliest middle ground: the user
  unzips, we point at `siril-cli.exe`, nothing is redistributed by us.

### 1.9 Windows availability

Official Windows 1.4.4 (2026-06-17) ships as **"64-bit UCRT64" installer and "Portable UCRT64"**
[C: siril.org/download]. `siril-cli.exe` is part of that `bin\` tree [C: user report + two-binary
design]. **[U]**: that the portable zip contains the identical `bin\` contents as the installer
(likely; verify by unzipping once on the target box).

### 1.10 Failure modes / integration hazards (all observed in docs or source)

1. **Command surface drifts between minor versions.** `stack`'s `[filtering]` slot existed in
   1.2 and is gone from the 1.4 syntax; `-weight_from_wfwhm` became `-weight=wfwhm`; the master
   branch already adds `register_mpp`/`stack_mpp` and `-disto=`. Pin the version, `requires` it
   in every script, and keep the Siril code path isolated behind one interface.
2. **Sequence naming is load-bearing.** `register`/`seqapplyreg` write `r_`-prefixed sequences and
   `.seq` files into the CWD; `live_stack_*` needs 5-digit fixed indexing; `.seq`, `.star`,
   `_stacked.fit` litter the directory. Give Siril a dedicated working directory per session.
3. **Dimension homogeneity is enforced** — mixed sizes abort with "Images must have same
   dimensions." (livestack) / "input images have different precision" (stack). Frames from a
   DSLR at fixed resolution are fine; live-view JPEG previews are not — never mix them into a
   Siril sequence.
4. **No progress API in script mode.** Only `-p` gives `status:`/`progress` framing (§1.2).
5. **Error signalling is "the script stops"** — exit code is the only reliable per-run signal,
   and in livestack mode a failure `break`s the loop and *leaves the process alive* (source
   `break` out of `live_stacker`), so watch stdout/pipe, not the process state.

---

## 2. ASTAP's own stacking

**Confirmed: `astap_cli.exe` cannot stack.** The official CLI table ends at solve/analyse/photometry
options; the stacking entry is `-stack <path>` whose own description is *"Start ASTAP with live
stack tab visible and path selected"* — a GUI action [C: hnsky.org/astap.htm command-line
section]. And the ASTAP-fork README states it plainly: "ASTAP already provides a command-line
version but only capable of solving." [C]

What ASTAP does have, in the GUI: average and sigma-clip stacking, master darks/flats/
flat-darks, a **Live stacking tab** that watches a directory, and — relevant to us — **no
rejection at all in live mode**: "Note there is no rejection of poor images. All images are
added with equal influence: result2:=(result1+B)/2, result3:=(result2*2+C)/3 …" [C]. It renames
processed files to `*.fts` and starts a new stack if a file is 0.2° away. So even as a
human-driven fallback it cannot be commanded.

**`ortegaalfredo/astap-command-line` fork** [C]: adds `-stack <json>`, JSON commands
`CreateMasterDark` / `CreateMasterFlat` / `Stack`, `Align: "stars"|"astrometry"`, master
dark/flat plus a light list, sigma-clipping only, "only the H18 star database is supported" for
astrometry. Repo facts: **last commit 2021-10-01**, self-described "Sync with CLI-0.9.581",
ships **win32** binaries, GitHub reports licence **LGPL-2.1** while upstream ASTAP is **MPL-2.0**
— an unresolved inconsistency in an already-abandoned tree. Rotation support is undocumented
(likely inherits ASTAP's star alignment, which does handle rotation, but **[U]**).
**Verdict: reject.** A 2021 fork of a Pascal codebase, 32-bit (so a 24 MP × N frame stack will
push the 2 GB address-space wall), zero maintenance, no per-frame API, no progress reporting.

Where ASTAP *does* help: we already solve and already have the `-analyse <snr>` quality gate
("[M] 288 ms wall for a failing solve including spawn"; "`-analyse 20` returns `HFD_MEDIAN=` and
`STARS=`") [C: 01]. That plus WCS-per-frame is an *alternative registration source* for our own
stacker (§5).

---

## 3. DeepSkyStacker CLI

Real, documented, and still in the codebase (`DeepSkyStackerCL/`), BSD-3-Clause since the 2018
open-sourcing; actively maintained (v6.1.x line, repo pushed 2026) [C].

Documented interface (official command-line manual) [C]:

```
DeepSkyStackerCL.exe [/r|R] [/S] [/SC] [/SR] [/O:<OutputFileName>] [/OFxxx] [/OCx] [/FITS] <ListFileName>
  /r  register only not-already-registered light frames   /R force re-register
  /S  stack   /SC stack + write calibrated frames   /SR stack + write registered+calibrated frames
  /O:name output file (always TIFF unless /FITS)    /OF16|16i|32|32i|32r   /OC0|1|2
```

Input is a **file list** (`.txt`), not a directory: first line `DSS file list`, second line
`CHECKED<TAB>TYPE<TAB>FILE`, then `1<TAB>light<TAB>C:\...`. And the killer line: *"At this time,
all the settings are shared between the stand-alone DeepSkyStacker and the command-line
DeepSkyStacker including RAW settings, registering and stacking settings, temporary file folder…
It means that before using the command-line version you must use the stand-alone version to set
all the values accordingly to your settings."* [C] — i.e. **the CLI reads its parameters out of
the GUI's stored settings**, so an unattended install is not reproducible without pre-seeding
that state. There is no per-frame mode, no progress channel, and the output is a single
`Autosave.tif`/`.fits` per run.

Live behaviour exists but is a **separate GUI program** (`DeepSkyStackerLive`, folder monitor,
`foldermonitor.cpp`, BSD) [C] — not scriptable.

Its registration is genuinely good and rotation-aware: "star detection (round, ≤50 px, Gaussian
centroid) + largest-triangle pattern matching, min 8 stars common to all frames, offsets+rotation
vs. best-scoring reference; sub-pixel" [C: via doc 03 §5]. Estimators include kappa-sigma and
auto-adaptive weighted (Stetson) [C].

**Verdict:** not for the loop (batch-shaped, GUI-coupled settings, TIFF output, no progress).
Interesting only as (a) a second opinion for a final stack, (b) the best permissively-licensed
reference *code* for star detection + triangle matching + kappa-sigma, if we want to steal ideas.
If the product is ever commercialised, DSS is the one external option with **no copyleft
obligation at all** — that is its only strategic advantage over Siril.

---

## 4. In-process Python (the "own it" option) — new measurements

Doc 03 already does this thoroughly; this section adds only what is *new or different* here, plus
the numbers needed for the latency budget in §6.

| Operation | 24 MP (6000×4000, uint16 48 MB) | Note |
|---|---|---|
| `sep.Background` | **350 ms** [M] | the expensive half — do it once per reference / on a 2× binned image, or reuse the previous frame's mesh |
| `sep.extract(sub, 5·σ, err=rms)` | **89 ms** [M] | 600 synthetic stars recovered exactly; without `err=` or on noisy frames it raises *"internal pixel buffer full"* unless `sep.set_extract_pixstack()` is raised [M] — the same failure doc 03 saw at 24 MP |
| `cv2.warpAffine` (rotation, bilinear) | **14 ms** u16 / **17 ms** f32 [M] | the cheapest step in the pipeline |
| `cv2.phaseCorrelate` | **10 ms @1 MP → ~246 ms @24 MP** [M ext.] | translation only *and* the FFT dominates at full res: run it on a downsample |
| `cv2.estimateAffinePartial2D` (RANSAC, 600 pts) | **0.05 ms** [M] | 4-DOF similarity; `estimateAffine2D` = 6-DOF affine; `findTransformECC` = refinement |
| `np.add` accumulator in place (f32 or u32) | **7.6 / 7.9 ms** [M] | u32 must be used if bit-exact sums are wanted |
| `np.multiply` (in-place), `img.mean()` | 3.1 ms / 7.1 ms [M] | |
| `u16→f32`, `u16→u8`, `resize 24MP→1.5MP` | 7.9 / 6.4 / **1.9 ms** [M] | preview path is essentially free |
| astroalign `find_transform` | **1019–1292 ms @24 MP; 536 ms @6 MP; 294 ms @2× bin; 114 ms @4× bin** [M] | rotation (0.400°) recovered exactly at every binning; 8× binning fails ("Reference stars … less than the minimum value (3)") [M] |
| astroalign `apply_transform` | **2162 ms @24 MP** [M] | skimage's warp — do **not** use it at full res; take `T.params` → `cv2.warpAffine` (14 ms) |

Consequences for the design:
- **Registration belongs on a ≤2 MP downsample** (doc 03 §4, reconfirmed: 294 ms at 2× bin vs
  ~1.1 s at full res, with the *same* transform recovered).
- **Accumulation is not the cost centre.** 7–10 ms per frame for a float32 add; even a
  Welford + ring-buffer σ-clip (doc 03: 91 ms/frame at 24 MP) fits in a 1–2 s budget with two
  orders of magnitude to spare.
- **The only genuinely expensive primitive is star detection on full-res frames.** Everything
  else is < 20 ms.

Licences [C]: astroalign **MIT**, sep **LGPL-3.0+** (that is a *library* linkage question — SEP is
LGPL, so importing it is fine under GPL-style terms as long as we keep it replaceable; if we ever
need a fully permissive stack, `photutils.DAOStarFinder` (BSD-3) is the drop-in but 3–10× slower),
OpenCV **Apache-2.0**, skimage **BSD-3**, numpy **BSD**.

Existing permissive live stackers worth reading (not re-deriving): **ALS** (`deufrai/als`, GPL-3,
literally astroalign + running mean — best architecture reference, copyleft code so read-don't-copy);
**AstraLite** (`art-den/astra_lite`, **MIT**, Rust + GTK, live stacking with satellite/metor trail
removal, Windows via `x86_64-pc-windows-gnu`, states 24 MP live stacking needs ≥4 cores / 2–4 GB
— an independent existence proof for the CPU budget); **PSS** (GPL-3, Python/OpenCV, CB
alignment-point stacking, CLI mode since 0.8 — the reference for *local* stacking, and for how to
structure a CLI batch); **DSS** (BSD-3, best permissive source); Siril's own source (GPL-3, best
documented estimator set).

---

## 5. Recommendation: hybrid, with Siril as the closer, and one idea that beats both

**Architecture**

```
capture → frame on disk (RAW/FITS) ─┬─► in-process loop (must be < ~300 ms/frame)
                                    │     · downscale to ≤2 MP, sep/SEP-or-OpenCV detect
                                    │     · similarity transform (predicted-rotation prior,
                                    │       phaseCorrelate residual, astroalign fallback)
                                    │     · cv2.warpAffine at full res into accumulators
                                    │     · float32 weighted mean + M2 (Welford) + coverage map
                                    │     · MTF/asinh stretch of a ≤1.5 MP preview → UI
                                    └─► Siril restack at end-of-session / on demand
                                          · generated .ssf, user-installed siril-cli.exe
                                          · register -transf=similarity, seqapplyreg -framing=cog
                                            + -filter-* quality gate, stack rej w 3 3 -weight=wfwhm
                                          · 30 s–3 min of unattended CPU, once
```

**Why not all-in-one Siril.** (1) There is no "register this frame against my reference" command;
the unit is a sequence and Siril picks the reference, so incremental means either the 2-image
`livestack` path (running mean, no rejection, full FITS round-trip per frame, §1.6) or O(N²)
re-registration. (2) The estimator we need — per-pixel ±kσ rejection that starts being meaningful
at N≈8–10 and a coverage map for rotated corners — is *not* what headless livestack does, and
there is no way to ask for it. (3) Every frame pays a ~48 MB write + read that our process does not
need to pay, because we hold the accumulator in RAM. (4) We already measured all the primitives;
the marginal engineering cost of owning them is small relative to the integration cost of a
GPL binary's versioning, config isolation, sequence naming and pipe protocol. (5) A 1–2 s UI cadence
with per-frame feedback (frame accepted/rejected, why, coverage, running SNR) is a *product*
feature; Siril's live path gives us no such signal.

**Why Siril is still worth shipping.** (a) The frames are on disk anyway — the objection "Siril
needs files" costs us nothing, we are not streaming. (b) Its global registration (DPSS + triangle
matching + RANSAC) is battle-tested where ours will fail first: sparse fields, heavy trailing,
cloud gaps, a hand-nudged tripod mid-session. (c) Winsorised σ-clip over the full sequence at the
end is *better* than our incremental estimator and costs nothing at that point. (d) It is a
credible "use the good tool" answer for the user who wants the best final image, and it costs us
one generated text file. (e) If our registration ever regresses, we have a second opinion on the
same frames for debugging.

**The idea that beats both, for our specific geometry.** On a fixed tripod the transform is a
*rotation by Ω·Δt about the projected pole* (doc 03 §1.2–1.4): 3 parameters total, of which 2 (the
pole's image position + rate) are **constants of the session** and can be calibrated from the
first few frames or even from two ASTAP solves. Then per frame you only estimate the *translation
residual* (10 ms phase correlation on a 1 MP tile) and *apply* the predicted rotation — no star
detection per frame at all, no triangle matching, immune to trailed stars, and the rejected-frame
decisions become "did the residual disagree with the prediction", which is a far sharper quality
gate than star count. astroalign/Siril then only run on (i) session start, (ii) after a nudge,
(iii) as the final restack. This is the cheapest path to a 1 s cadence that we have found, and
it is only available to us because we already plate-solve.

**Decision matrix**

| Situation | Use |
|---|---|
| Per-frame loop, 1–2 s cadence | in-process (own it) |
| Frame quality gate | ASTAP `-analyse` (already paid for) + our own roundness/FWHM from the detection we already run |
| Siril installed & user wants best final | generated `.ssf`, user's own `siril-cli.exe`, `-p`/`--offline`, `requires 1.4.0` |
| Siril absent | hide the feature with an install hint; never bundle by default |
| Commercial licensing sensitivity | DSS (BSD-3) as the restack backend instead of Siril — worse UX, no copyleft |

---

## 6. Latency budget

All numbers at 24 MP (6000×4000). "[M]" measured here; "[D]" derived by adding the measured
primitives plus estimated I/O; "[C]" cited.

| Path | Time to first image | Steady state per frame | Full re-stack of N=200 |
|---|---|---|---|
| **In-process, ≤2 MP registration** | 1st frame: one detect+stretch ≈ 0.3–0.5 s (or instant, if we simply display frame 1 stretched) | **~150–250 ms** [D from M: 114 detect@4×bin + 14 warp + 8 add + 2 preview] | **~20–60 s** [D: 200×150 ms re-register + 200×~20 ms accumulate with rejection ≈ 35 s, plus FITS reads ~10 s] |
| **In-process, predicted-rotation (fastest)** | same | **~30–50 ms** [D: 10 ms phase corr @1 MP + 14 warp + 8 add + preview] → disk-bound, not CPU-bound | same as above |
| **Siril `livestack` (persistent process, `-rotate`)** | process start 0.5–3 s [U] + first frame ≈ register+stack+writes ≈ **1–4 s** [D] | **~1.2–3 s** [C 1.2 s/frame batch figure; C 25 s/frame live figure is the pessimistic outlier] + 48 MB FITS write/read per frame [D] | n/a (it *is* incremental) |
| **Siril restack-on-growth (naive re-register)** | ~N× per-frame registration before anything appears | **O(N)** per frame → 200 frames ≈ 100–200 s [D] | same O(N²): **~2–5 min** [D] |
| **Siril one-shot script (final)** | n/a | n/a | **~30 s–3 min** [C/D: 200 frames at the cited ~1.2 s/frame incl. convert+register+stack ≈ 4 min; with `-2pass`+cached `r_` sequence and only `stack` ≈ 30–60 s] |
| **DSS CLI** | n/a (batch) | n/a | minutes; plus unavoidable 32-bit TIFF/`Autosave.tif` plumbing and GUI-seeded settings [C] |
| **ASTAP CLI** | 0.15–0.4 s hinted solve [C: 01] | n/a — no stacking | n/a |

---

## 7. Open questions / to verify on the Windows target

1. **[U]** `siril-cli.exe -s -` line-buffering: does it execute per line, and does anything mark
   command completion? (Decides whether stdin is usable or we must use `-p` named pipes.)
2. **[U]** Real startup time of `siril-cli.exe` on a Windows laptop, cold and warm, with `-i` and
   `-o`; and whether the Python-venv init blocks startup.
3. **[U]** Whether 1.4's `stack` still honours `-filter-*` (docs moved them to `seqapplyreg`).
4. **[U]** Quality of Siril registration on *trailed* 2–15 s frames at 14–85 mm with
   `setfindstar -relax -roundness=...`, measured against our own similarity fit on the same frames.
5. **[U]** Exact contents of the "Portable UCRT64" zip (does it include `bin\siril-cli.exe` and the
   full DLL set? footprint?).
6. **[U]** Legal review before bundling Siril in a paid installer (source-offer mechanics, notices,
   trademark).

---

## 8. References

Command line / scripting
1. Siril CLI man page — <https://siril.org/docs/man/>, <https://manpages.debian.org/unstable/siril/siril-cli.1>
2. `src/main-cli.c` (option array, `com.headless`, `-s -` stdin path, `ReconnectIO` on Win32) — <https://github.com/lock042/siril/blob/master/src/main-cli.c>
3. `src/core/pipe.c` (Win32 named pipes, `ready`/`status:`/`progress`, `ping`/`cancel`) — <https://github.com/lock042/siril/blob/master/src/core/pipe.c>
4. Headless mode — <https://siril.readthedocs.io/en/stable/Headless.html>
5. Command reference (1.4.4) — <https://siril.readthedocs.io/en/stable/Commands.html>; 1.2 for the older `stack [filtering]` syntax — <https://siril.readthedocs.io/en/1.2/Commands.html>
6. Script files (`requires`, argument quoting, per-command scriptability) — <https://siril.readthedocs.io/en/stable/scripts/Script-files.html>
7. Registration (global: triangle matching + RANSAC; algorithm table) — <https://siril.readthedocs.io/en/stable/preprocessing/registration.html>
8. Livestacking (GUI + headless, only FITS/RAW, shift-only default) — <https://siril.readthedocs.io/en/stable/Livestack.html>
9. `src/livestacking/livestacking.c` (2-image sequence, `stack_mean_with_rejection`, `nb_images_to_stack = 2`, `NBSTACK_WEIGHT`, similarity vs shift) — <https://github.com/lock042/siril/blob/master/src/livestacking/livestacking.c>
10. `src/core/command_def.h` (help strings for `stack`, `register`, `setfindstar` incl. `-relax`, `-roundness`) — <https://github.com/lock042/siril/blob/master/src/core/command_def.h>
11. Downloads (1.4.4, 2026-06-17; installer + Portable UCRT64) — <https://siril.org/download/>
12. Threading / memory model — `src/core/processing.{c,h}`, <https://discuss.pixls.us/t/dedicated-input-thread-and-buffering/30430>

Performance
13. "SIRIL stacking ~50 24-megapixel lights a minute" (24-core i9 + NVMe) — <https://discuss.pixls.us/t/speeding-up-siril-even-more-does-more-than-16g-of-ram-help/46135>
14. Live stacking shoot-out (Siril 1.2 ≈25 s/img, DSS Live ≈15 s, SharpCap ≈4 s; ASI2600MC) — <https://theskysearchers.com/viewtopic.php?t=31542>
15. Siril 1.0.0 release notes (memory-aware parallelism) — <https://siril.org/download/2022-03-09-siril-1.0.0/>

Licensing
16. GPL FAQ, *What is the difference between an "aggregate" and other kinds of "modified versions"* (#MereAggregation) and *#GPLPlugins* (fork/exec vs shared memory) — <https://www.gnu.org/licenses/gpl-faq.html>
17. Siril licence (GPL-3.0, headers say "v3 or later") — <https://github.com/lock042/siril> , <https://gitlab.com/free-astro/siril>

Other tools
18. ASTAP site, command-line section (`-stack` = "Start ASTAP with live stack tab visible"; live stacking = equal-weight running mean, no rejection) — <https://www.hnsky.org/astap.htm>
19. `ortegaalfredo/astap-command-line` (JSON commands, sigma-clip only, last commit 2021-10-01, win32) — <https://github.com/ortegaalfredo/astap-command-line>
20. ASTAP licence MPL-2.0 — <https://sourceforge.net/projects/astap-program/>, <https://github.com/han-k59/astap>
21. DeepSkyStacker command-line manual (switches, file-list format, "settings are shared with the standalone version") — <http://deepskystacker.free.fr/english/commandline.htm>
22. DeepSkyStackerLive (folder-monitoring GUI) — <http://deepskystacker.free.fr/english/live.htm>
23. DeepSkyStacker repo + BSD-3 licence + maintenance status — <https://github.com/deepskystacker/DSS>
24. astroalign API (`find_transform`, `PIXEL_TOL=2`, MIT) — <https://astroalign.quatrope.org/en/latest/api.html>, <https://github.com/quatrope/astroalign>
25. astroalign large-array failure report — <https://github.com/quatrope/astroalign/issues/77>
26. AstraLite (MIT; 24 MP live stacking footprint; Windows gnu toolchain) — <https://github.com/art-den/astra_lite>
27. Astro Live Stacker (GPL-3) — <https://github.com/deufrai/als>; PlanetarySystemStacker (GPL-3, CLI since 0.8) — <https://github.com/Rolf-Hempel/PlanetarySystemStacker>
28. Companion briefs: 01 (ASTAP CLI timings/`-analyse`), 03 (registration theory, estimators, in-process benchmarks).
