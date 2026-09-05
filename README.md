# parity

[![tests](https://github.com/Aleixiou/parity/actions/workflows/tests.yml/badge.svg)](https://github.com/Aleixiou/parity/actions/workflows/tests.yml)

**Prove two tables in two different database engines hold the same data —
without moving the data out of either engine.**

Migrations don't fail on translation. They fail at cutover, because nobody can
prove the new pipeline produces the same data as the old one — so the legacy
system runs in parallel "just to be safe", forever, at double the cost.
`parity` is the proof.

```bash
parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

```
✗ 5 differences in 10,000,000 rows
  28 queries · 7,628 rows downloaded (0.04% of both tables) · 41.3s
  1 only in A · 1 only in B · 3 different

  only in A   key 999999999
  only in B   key 4000
  different   key 13             columns: note
      note   A ''   B NULL
  different   key 6010000        columns: amount
      amount   A 700.010000   B 700.000000
  different   key 8700000        columns: is_refunded
      is_refunded   A NULL   B false

  comparing floats and decimals at 6 decimal places
```

Exit code `1`. Put it in CI and the build fails until the data agrees.

## How it works

`parity` pushes hash aggregation **down into both engines**. It asks each side
for one checksum per bucket of the key range, compares a handful of integers,
recurses only into the buckets that disagree, and downloads rows only from
ranges already proven to differ.

On identical tables it downloads **zero rows** and issues **four queries**,
whether the table has ten thousand rows or ten million.

## Measured

10,000,000 rows per side, PostgreSQL 18.4 ↔ DuckDB 1.5.5, median of three runs
(`demo/benchmark.py`):

| Scenario | Queries | Rows downloaded | Wall time |
|---|---|---|---|
| identical tables | 4 | **0** (0.0000%) | 21.0s |
| 5 planted differences | 28 | 7,628 (0.0381%) | 41.3s |

The five planted differences — a changed decimal, a deleted row, an inserted
row at key 999,999,999, a `NULL` turned into `''`, and a `FALSE` turned into
`NULL` — are all found exactly, with no false positives.

Cost is roughly **one full hash pass per side**. It is CPU-bound on MD5, not
IO-bound on key lookup, which is why **an index on the key column makes no
measurable difference** (38.8s without, 37.3s with). Raising
`--bisection-factor` does not speed up the dominant first pass; it only reduces
round trips on later levels.

## Install

```bash
pip install "parity-diff[all]"        # both engines
pip install "parity-diff[duckdb]"     # DuckDB only — no PostgreSQL driver pulled in
pip install "parity-diff[postgres]"   # PostgreSQL only
```

> The PyPI distribution is `parity-diff` — plain `parity` is squatted by an
> empty project. The command you run and the module you import are both
> `parity`.

Python 3.10+. The core has no dependencies; drivers are optional extras and are
imported lazily, so a DuckDB-only user is never made to install `psycopg`.

## Usage

```
parity diff --a CONN --a-table TABLE --b CONN --b-table TABLE --key COL
            [--columns a,b,c] [--exclude x,y]
            [--bisection-factor 32] [--threshold 10000] [--float-scale 6]
            [--max-diffs 100] [--json] [--quiet]
```

**Exit codes** — this is what makes it a CI check:

| Code | Meaning |
|---|---|
| `0` | identical |
| `1` | differences found |
| `2` | error (bad connection string, missing table, unusable key) |

An error is never `1`. A CI job can always tell "the tables differ" from "the
tool could not run".

Connection strings:

```
postgres://user:password@host:port/database     (also postgresql://)
duckdb:///relative/path.duckdb
duckdb:///:memory:
```

Table names may be schema-qualified. Unqualified names default to `public` on
PostgreSQL and `main` on DuckDB.

`--json` emits the same content as a machine-readable object, including
`identical`, `truncated`, per-difference values, and the full stats block.

### In CI

```yaml
- name: prove the migration is complete
  run: |
    parity diff \
      --a "$LEGACY_URL"     --a-table public.orders \
      --b "$WAREHOUSE_URL"  --b-table analytics.orders \
      --key order_id --quiet
```

## Limitations — read these before trusting a result

A parity tool that reports a false match is worse than useless, so these are
stated plainly rather than buried.

- **Integer keys only.** The bisection arithmetic divides the key range. A
  `varchar` or `uuid` key is rejected with a clear message, not guessed at.
  Composite and hashed keys are a planned extension.
- **Floats and decimals are compared at 6 decimal places** by default. Two
  values differing only in the 7th place are reported as *equal*. This is a
  deliberate cross-engine rounding contract — the two engines do not otherwise
  agree on float text. Change it with `--float-scale`; it always applies to
  both sides, and the scale in force is printed on every run.
- **Keys must be unique.** A non-unique key is detected up front and rejected.
  Silently collapsing duplicate rows would hide real differences.
- **`Infinity` and `NaN`** in float columns are compared as those literal
  tokens on both sides. A double whose magnitude reaches 1e32 is not supported
  and fails loudly on DuckDB.
- **Supported types:** integer, decimal, float, boolean, string, date,
  timestamp. Anything else is compared as raw text and reported as a warning —
  two engines may render the same JSON or array differently for reasons that
  have nothing to do with the data.
- **`--max-diffs` makes the result partial**, and says so: the run is flagged
  `truncated`, and `identical` is never true. It does not mean the rest matched.
- **Timezone-aware timestamps are compared as instants, in UTC.** Both
  sessions are pinned to UTC on connect, so the same instant matches whatever
  the two servers' default timezones are. Without that pin, two sides in
  different zones render every `timestamptz` differently and the tool reports
  the entire table as changed.
- Columns present on only one side are skipped with a warning, not treated as
  differences.

## Supported engines

| Engine | Status |
|---|---|
| PostgreSQL | supported (tested against 16 and 18) |
| DuckDB | supported (tested against 1.5) |
| Snowflake, BigQuery | not yet — see `CONTRIBUTING.md`, a dialect is ~80 lines |

## Scope

In scope: proving two tables match, finding exactly which rows and columns
don't, doing it cheaply on large tables, running in CI.

Deliberately out of scope: data quality rules, freshness checks, anomaly
detection, lineage, cataloguing, orchestration, transformation, a web UI, a
server, schema migration. If a feature does not help someone answer *"can I
safely switch off the old system?"*, it does not belong here.

## Development

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[all]" pytest
pytest
```

Tests that need PostgreSQL read `PARITY_TEST_PG` (default
`postgres://parity:parity@127.0.0.1:5432/parity`) and **skip cleanly** when no
server is reachable — the DuckDB and pure-Python suites still run.

A disposable PostgreSQL:

```bash
docker run -d --name parity-pg -p 55432:5432 \
  -e POSTGRES_USER=parity -e POSTGRES_PASSWORD=parity -e POSTGRES_DB=parity \
  postgres:16-alpine
export PARITY_TEST_PG="postgres://parity:parity@127.0.0.1:55432/parity"
```

Reproduce the benchmark:

```bash
python demo/generate.py --rows 10000000
python demo/benchmark.py --expect-clean
python demo/generate.py --rows 10000000 --plant
python demo/benchmark.py --expect-planted
```

`CLAUDE.md` holds the verified cross-engine SQL and why each expression is the
way it is. `BUILD_SPEC.md` is the build plan.

## License

MIT — see `LICENSE`.
