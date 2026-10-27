# OpenNGC (vendored, unmodified)

Upstream: <https://github.com/mattiaverga/OpenNGC> (directory `database_files/`)

| file | size | rows | fetched |
|---|---|---|---|
| `NGC.csv` | 3 942 946 B | 13 970 data rows | 2026-10-27 |
| `addendum.csv` | 17 496 B | 64 data rows | 2026-10-27 |

Semicolon-delimited, UTF-8. The header is repeated in both files.

**Do not edit these files.** They are copied verbatim on purpose.

## Licence

**CC-BY-SA-4.0.** Full text in `CC-BY-SA-4.0.txt`; attribution to Mattia Verga and
the OpenNGC contributors. Share-alike applies to **the data file**, not to ministar's
source, which is MIT.

Shipping the files *unmodified* is deliberate and load-bearing: a modified or
reduced copy would be a derivative requiring a "changes were made" notice, and a
converted copy (e.g. JSON) would become an artifact we maintain. Parsing the CSV at
load time costs ~59 ms for 14 034 rows, so there is nothing to gain by transforming
it and correctness to lose.

## Things worth knowing about the data

* `Type` includes **non-objects**: `Dup` (652 duplicate rows), `NonEx` (10), `Other`
  (419), `**` (244 double stars). These must be excluded before display or the same
  object appears several times.
* **`V-Mag` is only present for 30%** of rows, while `B-Mag` is present for 81%.
  Filters must fall back to B when V is absent, and say which one was used.
* `SurfBr` is present for 73%, `MajAx` for 86%. Absence is common and legitimate for
  compact objects, so a missing value must not be read as zero.
* `M` is zero-padded (`M045`, not `M45`), and M40/M45 are present, so the Messier
  list needs no extra file.
* Sizes (`MajAx`/`MinAx`) are in arcminutes.
* A handful of rows have unparseable or empty `RA`/`Dec`; the parser must skip them
  rather than crash.
