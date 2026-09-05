# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
with the caveat that 0.x means the CLI surface may still move.

## 0.1.0 — 2026-09-05

First release. Compares a table in PostgreSQL against a table in DuckDB and
reports exactly which rows and columns differ, without moving the data out of
either engine.

### What it does

- **`parity diff`** with human and JSON output, and CI exit codes: `0`
  identical, `1` differences found, `2` error. An error is never `1`, so a
  pipeline can tell a real disagreement from a bad connection string.
- Pushes hash aggregation into both engines and binary-searches the key space.
  On identical tables it issues **4 queries and downloads zero rows**, whether
  the table holds ten thousand rows or ten million.
- Reports each difference as `only_in_a`, `only_in_b`, or `different` with the
  columns that moved and both values.
- `--columns`, `--exclude`, `--bisection-factor`, `--threshold`,
  `--float-scale`, `--max-diffs`, `--json`, `--quiet`.

### Measured

10,000,000 rows per side, PostgreSQL 18.4 against DuckDB 1.5.5, six runs:

| Scenario | Queries | Rows downloaded | Wall time |
|---|---|---|---|
| identical tables | 4 | 0 (0.0000%) | 25-31s |
| 5 planted differences | 28 | 7,628 (0.0381%) | 48-55s |

Query and row counts are exact and hardware-independent; the times are one
laptop.

### Correctness

Every test plants a difference and asserts the tool finds exactly it. A suite
that only compares matching tables passes trivially for a completely broken
tool, and that discipline caught each of these before release:

- a `NULL` boolean compared **equal** to `FALSE`, because `CASE ... ELSE` swallowed
  the NULL. Both engines produced the same wrong answer, so cross-engine
  agreement could not detect it
- `timestamptz` columns reported **every row as different** when the two servers
  sat in different timezones. Sessions are now pinned to UTC
- the walk saw the table change underneath it, because PostgreSQL's default
  READ COMMITTED gives every statement a fresh snapshot. Now REPEATABLE READ
- absolute DuckDB paths broke on every Linux and macOS machine
- tables wider than 99 columns failed outright on PostgreSQL's 100-argument
  function limit
- two unrelated tables exhausted memory rather than answering
- `Infinity` and `NaN` aborted the run on DuckDB
- a `NULL` key was misreported as a duplicate key, and a missing `GRANT` as a
  missing table
- the bucket arithmetic overflowed int64 on wide key ranges

### Known limitations

Stated plainly, because a parity tool that reports a false match is worse than
useless.

- **Integer keys only.** Other key types are rejected, not guessed at.
- **Floats and decimals compare at 6 decimal places** by default. Two values
  differing only in the 7th place are reported as equal. `--float-scale`
  changes it, and the scale in force is printed on every run.
- **Keys must be unique**, and must not be NULL. Both are detected and refused.
- **At most 10,000 differences are reported** by default; past that the run is
  flagged `truncated` and `identical` is never true. `--max-diffs 0` lifts it.
- Supported types: integer, decimal, float, boolean, string, date, timestamp.
  Anything else is compared as raw text and warned about.
- A double whose magnitude reaches 1e32 is not supported and fails loudly.

### Engines

PostgreSQL (tested against 16 and 18) and DuckDB (tested against 1.5).
Snowflake and BigQuery are not implemented; see `CONTRIBUTING.md` — a dialect
is roughly 80 lines.
