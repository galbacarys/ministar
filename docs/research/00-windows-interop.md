# 00 — Windows interop: the build/run pattern

**Status:** verified on this machine (WSL2, Ubuntu-24.04, Windows 10.0.26200.9457).
Every claim marked ✅ below was reproduced locally, not taken from documentation.

---

## 1. The problem this pattern solves

Development lives in WSL2 (real git, fast filesystem, decent shell). The product must be
a native Windows application, because the camera SDK is a Windows DLL and the user is
sitting in front of Windows. So there is a boundary, and the boundary needs a contract.

The obvious approach — call Windows binaries directly on files in the WSL filesystem —
is the one that does not work:

```
$ cd /home/user/src/ministar
$ cmd.exe /c ver
'\\wsl.localhost\Ubuntu-24.04\home\user\src\ministar'
CMD.EXE was started with the above path as the current directory.
UNC paths are not supported.  Defaulting to Windows directory.
Microsoft Windows [Version 10.0.26200.9457]
```

✅ Verified. Two separate failures compound here:

1. **Win32 programs cannot `chdir` into a WSL path.** WSL exposes it to Windows as a UNC
   path (`\\wsl.localhost\...`), and the classic Win32 API has no concept of a UNC current
   directory. `cmd.exe` announces this and silently relocates to `C:\Windows`.
2. **Windows processes cannot open ext4 paths at all.** Handing `astap.exe` a path like
   `/home/user/frames/x.fits` is not a quoting problem to be fixed — the file genuinely
   does not exist in the Windows namespace.

Consequence: *any* file a Windows program must read or write has to live on a real
Windows volume. That single fact dictates the whole layout.

> The failure mode is nasty because it is silent. `cmd.exe` warns and continues with a
> different working directory, so a `.bat` that relies on relative paths runs, does
> something wrong, and exits 0.

---

## 2. The pattern

Three zones, one direction of flow for source, and a hard rule about where data lives.

```
┌─ WSL ext4 ─────────────────┐        ┌─ /mnt/c (NTFS) ──────────────┐
│                            │        │                              │
│  ~/src/ministar            │ rsync  │  C:\ministar\build           │
│    src/          ──────────┼───────>│    *.exe  *.bat  *.dll       │
│    docs/                   │        │                              │
│    (git, editing, tests)   │        │  C:\ministar\data            │
│                            │        │    frames\ scratch\ output\  │
└────────────────────────────┘        └──────────────────────────────┘
                                                 │
                                                 │ cmd.exe /c  (invoked from bash)
                                                 ▼
                                      runs natively, writes results
                                                 │
                                      ◄──────────┘  artifacts read back
```

**Rules**

1. **Source of truth is ext4.** Git repos, editors, and tooling live in `~/src`. Never
   develop in `/mnt/c` — it is roughly an order of magnitude slower for metadata-heavy
   work (git status, node_modules, compiling).
2. **Copy before running.** The build directory on the Windows side is a *deployment
   artifact*, not a working copy. Treat it as disposable: wipe and re-sync freely.
3. **All runtime I/O happens on NTFS.** Frames in, results out, logs, caches, plate-solve
   scratch files. Windows programs cannot see ext4, so this is not a preference.
4. **The `.bat` is the only entry point.** bash never invokes an `.exe` directly except
   for ad-hoc probing. Args, environment, and exit codes flow through the `.bat`.
5. **Never `cd` into a WSL path and then call `cmd.exe`.** `cd /mnt/c/...` first.

---

## 3. The sync step

`rsync` is present ✅ and is the right tool. Cache and VCS noise must be excluded, and
the mirror should be deletable at any time.

```
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude 'bin' --exclude 'obj' \
  --exclude '__pycache__' --exclude '*.pyc' \
  ~/src/ministar/ /mnt/c/ministar/build/
```

Throughput measured ✅ at **~120 MB/s** writing to `/mnt/c` (40 MB in 0.33 s), so sync
cost is negligible for code. It is *not* negligible for hundreds of 36 MB FITS frames —
those belong on the Windows side from the moment the camera writes them, and should never
be copied across the boundary. The camera writes to `C:\`, the tool reads from `C:\`, and
WSL only ever sees them if we are debugging.

**Windows-side layout** (deliberately *not* `Program Files`):

```
C:\ministar\
  build\        ← rsync target, disposable
  data\
    frames\     ← camera writes here
    scratch\    ← plate-solve scratch, one subdir per solve
    output\     ← stacked results, logs
  tools\        ← third-party: astap, EDSDK DLLs
```

Program Files is read-only to a non-elevated user, and several of the tools we drive
write sidecar files *next to their input*. ASTAP is the concrete example: given a RAW
input it drops an intermediate `.fits` into the input directory. Putting data under
`C:\ministar\data` removes an entire class of permission bugs.

---

## 4. Line endings are load-bearing

**A `.bat` with LF-only line endings loses its exit code.** ✅ Verified:

| File written with | Result |
|---|---|
| `LF` only | echoed `hello-lf`, **exited 0** — the final `exit /b 7` was misparsed and the status was lost |
| `CRLF` | **exited 7** as intended |

This is the single most dangerous failure in the pattern, because the script *appears to
work*: output is produced, nothing errors, and only the status is wrong. Since the whole
design leans on exit codes to detect failure, an LF-checked-out `.bat` turns every error
into a silent success.

Mitigation, both belts:

- `.gitattributes` forces `*.bat` and `*.cmd` to `eol=crlf` on checkout.
- Anything that *generates* a `.bat` (a build step, a test harness, a heredoc) must write
  `\r\n` explicitly. Shell heredocs emit LF. Prefer a checked-in template over a
  generated file.

Only batch files care. C#, Python, C, and Rust sources are fine either way.

---

## 5. Calling convention from bash

**Working directory.** `cd` to a `/mnt/c` path before invoking. ✅ Verified:

```
$ cd /mnt/c/temp/probe && cmd.exe /c "cd"
C:\temp\probe
```

with no UNC warning. From anywhere in ext4, the same command prints the warning and
returns `C:\Windows`.

**Absolute Windows paths, always.** `wslpath -w /mnt/c/Users` → `C:\Users`. ✅ Use it to
translate at the boundary. Never rely on the child process's cwd to resolve a relative
path — it is another way to inherit the UNC problem silently.

**Spaces in paths.** `cmd.exe /c 'cd /d "C:\Program Files\astap" && astap_cli.exe -h'`
**fails** ✅ with `The filename, directory name, or volume label syntax is incorrect.` —
the nested quoting does not survive the WSL interop argv re-quoting. Two working options:

- Put the `cd /d` inside a `.bat` (the pattern we recommend), or
- Use the 8.3 short path.

This is a strong argument for the `.bat`-as-entry-point rule: it keeps quoting hell inside
the one language that understands it.

**Environment.** Use `WSLENV` to forward variables across the boundary (`VAR/w` translates
a path). Do not assume the Windows process shares the WSL environment — it does not.
`PATH`, `HOME`, and locale all differ.

---

## 6. Exit codes

Exit codes propagate correctly through WSL interop, ✅ verified with a deliberate `42` and
`7`. Two ways to lose them, both of which I hit while testing:

- **Piping.** `cmd.exe /c foo.bat | head` makes `$?` the status of `head`. Capture first:
  `cmd.exe /c ... > out.txt 2>&1; rc=$?`
- **Forgetting `exit /b`.** Without it, the `.bat` exits with the status of its last
  command, which is usually `echo` and therefore `0`. Always end with:

  ```bat
  @echo off
  setlocal
  ...work...
  exit /b %errorlevel%
  ```

Codes above 255 cannot survive a Unix `$?`. This matters for ASTAP, which abuses its exit
code to smuggle `HFD*1e6 + nstars` out of `-analyse` and `-focus`. Do not use those modes
across the boundary; parse the `.ini` or the CSV instead.

---

## 7. Stdio is not a data channel

✅ Verified: child stdout arrives with **CRLF**, no BOM, in the console code page. Three
consequences:

1. Strip `\r`, or every parsed token carries a trailing carriage return.
2. Do not assume UTF-8. The console code page is typically 437 or 850; non-ASCII output
   will mojibake. Force `chcp 65001` in the `.bat` if you genuinely need UTF-8 out.
3. **Prefer files over stdout for anything real.** Stdout is for humans and for
   diagnostics. The machine-readable contract should be a file on disk that both sides
   can read — this is exactly how ASTAP works (`.ini` sidecar), and it is the model to
   copy for our own components.

---

## 8. Process lifecycle

**Orphans.** ✅ A `kill -9` on the bash side does **not** leave an orphaned `cmd.exe`
(verified with a 5-second `ping` sleeper: child count returned to 0). WSL tears down the
process group. Good news for a supervisor that restarts crashed workers.

**Per-invocation cost.** ✅ **~43 ms** round trip for `cmd.exe /c echo`, including the
Windows process spawn. That is a floor on any design that shells out once per frame:

- 43 ms is fine against a 2-second exposure cadence (~2% overhead).
- It is *not* fine for a focus loop that wants 10–30 live-view frames per second. Anything
  that must run at frame rate has to be a **long-lived Windows process**, not a subprocess
  spawned per frame.

This is the main architectural pressure the interop pattern creates: the pattern is
excellent for *starting* work and *inspecting* results, and it is unsuitable for sustained
high-frequency I/O. The shape that resolves it:

```
bash (supervisor / dev driver)  --start/stop-->  Windows daemon (long-lived)
                                <--status-----   camera + astap + stacker
                                <--files------
```

One spawn at startup; afterwards, communication is over files, a localhost socket, or a
watched directory. Our own code should therefore be a resident Windows process, with the
`.bat` used to launch it, query it, and shut it down.

**Watch out for:** Defender first-run scanning (hundreds of ms on a freshly written exe —
a real cost if the build dir is wiped every iteration) and Windows Firewall prompting on
first bind of a listening socket.

---

## 9. Toolchain audit (this machine, ✅ verified)

Choosing an implementation language is constrained by what actually exists here:

| Tool | Status |
|---|---|
| `cmd.exe` | ✅ `C:\Windows\system32\cmd.exe`, on PATH |
| PowerShell | ✅ 5.1.26100.9444 (Windows PowerShell, not Core) |
| Node / npm | ✅ `C:\Users\username\AppData\Local\hermes\node\` |
| git | ✅ `C:\Users\username\AppData\Local\hermes\git\` |
| **Python** | ⚠️ **Corrected.** `where python` resolves to a 2-byte Microsoft Store reparse stub in `%LOCALAPPDATA%\Microsoft\WindowsApps`, which prints "Python was not found". But **`uv` is installed on Windows** (`%LOCALAPPDATA%\hermes\bin\uv.exe`) and has **already provisioned a real CPython 3.11.16** at `%APPDATA%\uv\python\`. The interpreter exists; only PATH wiring is missing. See 05. |
| `uv` | ✅ installed (Windows `...\hermes\bin\uv.exe`, and WSL `~/.local/bin/uv`) |
| `py` launcher | ❌ not found |
| .NET SDK | ❌ no install directory, no `dotnet` |
| `msbuild` / `csc` | ❌ not on PATH |
| Go, Rust | ❌ not installed |
| ASTAP | ✅ `C:\Program Files\astap` — CLI-2025.10.11, D80 catalog (1476 tiles), 1.3 GB |
| N.I.N.A., PHD2 | ✅ installed (AppData), so prior art is inspectable locally |

Anything not marked ✅ has to be installed before it can be a candidate, and that install
is itself a decision to defend. Notably, the "obvious" choice for astro tooling — Python —
is the one thing not present.

---

## 10. Anti-patterns

- ❌ Editing files in `/mnt/c` and running them directly. Tempting, fast to iterate, and it
  breaks the moment the tree needs to be rebuilt or the artifact is stale. Also invites
  committing from the wrong place.
- ❌ Passing WSL paths to Windows binaries and "fixing" it with more quoting.
- ❌ Parsing stdout with a regex as the primary interface.
- ❌ Generating `.bat` files from heredocs (LF).
- ❌ Shelling out per frame for anything that needs to be fast.
- ❌ Building or testing against data on the wrong side of the boundary — a test that
  passes on ext4 paths proves nothing about the shipping configuration.

---

## 11. Open questions

- **`\\wsl.localhost` performance** for the cases where we *do* want Windows to read WSL
  files (e.g. a debugger attaching to sources). Unmeasured; expected to be poor for many
  small files.
- **Long paths >260 chars.** Windows support is opt-in via manifest/registry. Untested;
  assume broken and keep paths short.
- **Defender exclusion** for the build/data dirs — worth measuring on the real rig, since
  a wiped-and-resynced build dir triggers first-run scanning on every exe.
- **`usbipd-win`** viability if we ever want libgphoto2 on Windows (see 02). It is a
  development convenience, not something to ship to users.
- **Toolchain decision** (C# vs. Node vs. Python) is unresolved — see 02 for the EDSDK
  binding situation, which is the binding constraint.
