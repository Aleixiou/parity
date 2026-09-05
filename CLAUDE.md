# CLAUDE.md — project context for `parity`

> Read this file **and** `BUILD_SPEC.md` before writing any code.
> This file is durable context: what the project is, what is already proven,
> and the rules you must not break. `BUILD_SPEC.md` is the ordered build plan.

---

## 1. What this is

`parity` proves that **two tables living in two different database engines hold
the same data** — without moving the data out of either engine.

You point it at a table on each side. It pushes hash aggregation down into both
engines, compares a handful of integers, binary-searches the key space to
isolate where they diverge, and downloads only the rows that actually differ.

```
parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

## 2. Why it exists (do not lose sight of this)

Data migrations do not fail on translation — SQLGlot and LLMs largely solved
turning old SQL into new SQL. They fail at **cutover**, because nobody can prove
the new pipeline produces the same data as the old one. So the legacy system
runs in parallel "just to be safe", forever, at double the cost.

- 25% of data teams name legacy systems as their single biggest bottleneck
- ~74% of legacy modernisation projects fail
- Datafold **sunset its open-source `data-diff` in May 2024** to push users to a
  paid cloud product, vacating the open-source position in a category with
  already-proven demand
- What remains is an unmaintained community fork, SQLMesh's `table_diff` (which
  requires adopting the entire SQLMesh framework), and pandas-scale tools that
  die on real warehouse volumes

The opening is a **standalone, framework-free, warehouse-native parity checker
that an individual engineer can `pip install` without asking procurement.**

## 3. Scope discipline — this is the thing most likely to kill the project

Datafold abandoned its open-source tool because maintaining it was expensive.
The only defence is a brutally narrow scope.

**In scope:** proving two tables match; finding exactly which rows and columns
don't; doing it fast and cheaply on large tables; running in CI.

**Out of scope — refuse these, even when they seem like small additions:**

- data quality rules, freshness checks, anomaly detection, "observability"
- lineage, cataloguing, orchestration, transformation
- a web UI, a server, a database of past runs
- schema migration or DDL generation
- anything that requires adopting a framework to use

If a feature does not help someone answer *"can I safely switch off the old
system?"*, it does not belong here.

## 4. Verified cross-engine facts

**These were empirically verified against DuckDB 1.5.5 and PostgreSQL 16.13.
Do not change any expression below without re-running the verification harness
(`tests/test_encoding.py`, Milestone 1) and confirming every case still agrees.**

### 4.1 The row hash

The foundation of the whole tool: fold canonical row text into an integer that
**both engines compute identically**.

| Engine | Expression |
|---|---|
| PostgreSQL | `('x' \|\| substr(md5(<text>), 1, 15))::bit(60)::bigint` |
| DuckDB | `cast(('0x' \|\| substr(md5(<text>), 1, 15)) as bigint)` |

Both return `648541476951500027` for input `'abc'`.

**Why 15 hex characters (60 bits) and not 16.** 60 bits is the widest MD5 prefix
that both engines render as the same *positive* signed 64-bit integer. At 16
characters PostgreSQL's `bit(64)::bigint` wraps to negative and the two engines
disagree. Do not widen this.

**Why the sum must be widened.** Row hashes reach 2^60. Summing millions of them
overflows a 64-bit accumulator, so each side must aggregate in a wider type:
PostgreSQL `sum(h::numeric)`, DuckDB `sum(cast(h as decimal(38,0)))`.

### 4.2 Canonical text encoding

Every column is rendered to text before hashing. Two rows are equal **iff** their
canonical text is byte-identical, so these expressions are the correctness
contract. All 17 cases below were verified to agree exactly.

| Logical type | DuckDB | PostgreSQL |
|---|---|---|
| INTEGER | `cast(c as varchar)` | `(c)::text` |
| DECIMAL | `cast(cast(c as decimal(38,6)) as varchar)` | `cast(round((c)::numeric, 6) as text)` |
| FLOAT | `cast(cast(c as decimal(38,6)) as varchar)` | `cast(round((c)::numeric, 6) as text)` |
| BOOLEAN | `case when c then 'true' when not c then 'false' end` | `case when c then 'true' when not c then 'false' end` |
| STRING | `cast(c as varchar)` | `(c)::text` |
| DATE | `strftime(c, '%Y-%m-%d')` | `to_char(c, 'YYYY-MM-DD')` |
| TIMESTAMP | `strftime(c, '%Y-%m-%d %H:%M:%S.%f')` | `to_char(c, 'YYYY-MM-DD HH24:MI:SS.US')` |

Verified agreeing values include: `1.5 → "1.500000"`, `-0.125 → "-0.125000"`,
`1/3 → "0.333333"`, `2024-02-29 13:04:05.123456` round-trips exactly,
`2024-01-01 00:00:00 → "2024-01-01 00:00:00.000000"`, and full Unicode strings.

**Known, deliberate limitation:** floats and decimals are compared at 6 decimal
places. Document this loudly in the README — silent precision assumptions are
exactly how a parity tool loses trust. Make it configurable later
(`--float-scale`), never silently variable.

### 4.3 NULL handling and field separation

- NULL renders as the literal sentinel `\N`, never SQL NULL:
  `coalesce(<expr>, '\N')`. An un-coalesced NULL would poison the whole
  concatenation and silently mask differences.
- **The BOOLEAN expression must use `when not c`, never `else`.** With
  `case when c then 'true' else 'false' end` a NULL boolean falls into the
  `else` branch and renders `'false'`, so the `coalesce` never fires and a NULL
  on one side compares *equal* to a FALSE on the other. Both engines agreed on
  that wrong answer, so a cross-engine equality test could not see it - it was
  caught only by a test that planted a NULL-versus-FALSE difference and
  asserted the tool found it. This is the same bug class as NULL versus `''`.
- Fields join with `concat_ws(chr(31), ...)` — ASCII Unit Separator, which
  effectively never occurs in warehouse string data and is spelled identically
  in both engines.

### 4.4 Engine type names — they do not look alike

`information_schema.columns` exists in both engines but reports wildly
different type names for the same column. Verified output for one identical
table:

| Column DDL | DuckDB reports | PostgreSQL reports |
|---|---|---|
| `bigint` | `BIGINT` | `bigint` |
| `integer` | `INTEGER` | `integer` |
| `decimal(12,2)` | `DECIMAL(12,2)` | `numeric` |
| `double precision` | `DOUBLE` | `double precision` |
| `varchar(20)` | `VARCHAR` | `character varying` |
| `text` | `VARCHAR` | `text` |
| `boolean` | `BOOLEAN` | `boolean` |
| `date` | `DATE` | `date` |
| `timestamp` | `TIMESTAMP` | `timestamp without time zone` |

So `map_type()` must lowercase, strip everything from the first `(`, and match
against a generous alias set — and `timestamp` must match by *prefix*, because
PostgreSQL appends `without time zone`. Default schema differs too: `public`
on PostgreSQL, `main` on DuckDB.

### 4.5 Portability traps already discovered

- **`/` is not portable.** PostgreSQL truncates on integer operands; DuckDB
  promotes to double. Integer division must go through a dialect method
  (`int_div`): PostgreSQL `(a / b)`, DuckDB `(a // b)`. Do **not** work around
  this with `floor(a/b)` — double precision silently breaks on large key ranges.
- `md5()` returns the identical lowercase hex digest in both engines.
- `concat_ws`, `chr`, `coalesce`, `bit_xor` and ordered `string_agg` all exist
  in both, but prefer summing over XOR: **XOR silently cancels duplicate rows.**
- An empty segment returns SQL NULL from `sum()`, not 0. Both dialects must
  wrap the aggregate in `coalesce(..., 0)` or an empty bucket on one side
  compares unequal to an empty bucket on the other and the walker recurses
  into nothing forever.

### 4.6 Performance — measured, not assumed

Benchmarked at 2,000,000 rows per side, PostgreSQL 16 ↔ DuckDB, 2 vCPU:

```
identical tables      4 queries    0 rows downloaded            0.75s @ 200k
one changed row       8 queries    3,906 downloaded (0.195%)    6.5s  @ 2M
```

Re-measured at **10,000,000 rows per side**, PostgreSQL 18.4 (native, Windows)
↔ DuckDB 1.5.5, via `demo/benchmark.py`:

```
identical tables      4 queries        0 rows downloaded (0.0000%)   19.8s
5 planted diffs      28 queries    7,628 rows downloaded (0.0381%)   38.4s
```

The five planted differences are a changed decimal, a deleted row, an inserted
row at key 999,999,999, a NULL turned into `''`, and a FALSE turned into NULL.
All five are found exactly, with no false positives, while 0.0381% of the two
tables crosses the network. Times are the median of three runs.

**An index on the key column makes no difference** — measured 6.54s without
versus 7.39s with at 2M, and 38.8s without versus 37.3s with at 10M: noise in
both directions. This is counter-intuitive and worth
understanding before anyone "optimises" it: the top-level query must hash
*every row* to produce a full-table checksum, so the cost is CPU-bound on MD5,
not IO-bound on key lookup. The index only helps the final small-range fetches,
which are already trivial.

Consequences for anyone tuning this:

- Total cost ≈ **one full hash pass per side**, plus a negligible tail. Deeper
  bisection levels only scan the surviving segment.
- Raising `--bisection-factor` does *not* speed up the dominant first level. It
  reduces round trips on later levels only.
- The real optimisations later are sampling mode, and pushing the comparison
  into a `WHERE` clause on a watermark column — not indexes.
- Both sides' queries are issued **in parallel** (two threads). The comparison
  is otherwise entirely IO-wait on two independent engines.

### 4.7 Behaviours verified end-to-end

A working reference implementation (see §5) was run against real DuckDB and
PostgreSQL instances. These are confirmed, and the test suite must keep them
confirmed:

- identical 200k-row tables → 0 differences, **0 rows downloaded**, 4 queries
- a changed decimal → reported as `different` on exactly the `amount` column
- a row deleted from one side → `only_in_b`
- a row inserted on one side → `only_in_a`
- **NULL versus empty string → reported as `different`** (`'\N'` vs `''`).
  This is the trap naive implementations fail, and a real migration bug class
- an inserted row with key `999999999` widened the key range from 200k to 1e9,
  and the walk still completed in 18 queries — sparse key spaces cost round
  trips, not scans
- `bucket_bounds()` was verified against the SQL bucket expression for every
  key across randomised `(lo, hi, n)` combinations

## 5. Architecture

**A working reference implementation of Milestones 0–2 ships in this repo**
(`src/parity/`, proven by `demo/proof.py`). It is real, tested code, not
pseudocode — read it before rewriting anything. It has no CLI, no test suite,
and no packaging yet; that is what `BUILD_SPEC.md` picks up.

### Connection string grammar

```
postgres://user:password@host:port/database     (also postgresql://)
duckdb:///relative/path.duckdb
duckdb:///:memory:
```

The scheme before `://` selects the dialect in `get_dialect()`. Table names may
be schema-qualified (`public.orders`, `main.orders`); unqualified names default
to `public` on PostgreSQL and `main` on DuckDB.

```
src/parity/
  types.py                      # Column, TableRef, Segment, RowDiff, DiffResult
  dialects/
    base.py                     # Dialect ABC + get_dialect() + type mapping
    postgres_dialect.py
    duckdb_dialect.py
  engine.py                     # segmentation + recursive bisection (engine-agnostic)
  cli.py                        # argparse entry point, human + JSON output
tests/
  test_encoding.py              # cross-engine canonical encoding agreement
  test_engine.py                # bisection logic against fixtures
  test_integration.py           # real DuckDB <-> Postgres diffs
demo/
  generate.py                   # build N-row datasets with planted differences
  benchmark.py                  # timing + bytes-transferred proof
```

**The load-bearing separation:** dialects know *only* how to render and hash
values for one engine. The bisection algorithm knows *nothing* about any
engine. Adding Snowflake or BigQuery later must mean writing one new dialect
file and touching nothing else. If you find yourself adding an
engine-specific branch to `engine.py`, the abstraction is wrong — fix it there.

### The `Dialect` contract

```python
class Dialect(ABC):
    name: str
    def connect(self, connection_string: str) -> None: ...
    def close(self) -> None: ...
    def query(self, sql: str) -> list[tuple]: ...
    def columns(self, table: str) -> list[Column]: ...
    def quote(self, identifier: str) -> str: ...
    def normalize(self, column: Column) -> str: ...   # canonical text, null-safe
    def hash_expr(self, text_expr: str) -> str: ...   # -> 60-bit integer
    def int_div(self, num: str, den: str) -> str: ... # truncating division
    def sum_wide(self, expr: str) -> str: ...         # overflow-safe sum
```

Shared, non-abstract helpers on the base class build `concat_ws(...)` row text,
the per-segment checksum query, and the small-range row fetch — so a new
dialect is roughly 80 lines.

## 6. Engineering conventions

- **Python 3.10+**, standard library only for the core. `duckdb` and
  `psycopg[binary]` are **optional extras** (`pip install parity[postgres]`).
  A user who only has DuckDB must never be forced to install a Postgres driver.
- Type hints everywhere. `from __future__ import annotations` at the top of
  every module.
- Dataclasses for value types. No ORM, no SQLAlchemy — we emit dialect SQL
  deliberately, that is the product.
- No network calls, no telemetry, no phoning home. Ever. This tool points at
  production warehouses; trust is the entire distribution strategy.
- Read-only by construction: the tool issues `SELECT` only. Never generate
  DDL or DML against a user's database.
- Comments explain *why*, especially every cross-engine workaround — each one
  is a landmine for the next contributor.
- Errors must name the side and the table. `"table not found"` is useless when
  you are comparing two databases.

## 7. Local development environment (Windows)

DuckDB needs nothing. For PostgreSQL, either:

```powershell
# Option A - Docker Desktop (preferred, disposable)
docker run -d --name parity-pg -e POSTGRES_PASSWORD=parity -e POSTGRES_USER=parity `
  -e POSTGRES_DB=parity -p 55432:5432 postgres:16-alpine

# Option B - native installer from postgresql.org, then create the parity db
```

Connection strings used throughout the tests:

```
postgres://parity:parity@127.0.0.1:55432/parity
duckdb:///./demo/new.duckdb
```

Python setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[all]" pytest
```

## 8. How the project stays honest

Two rules that matter more than any feature:

1. **A parity tool that reports a false match is worse than useless.** Every
   test must include a *planted difference* and assert the tool finds exactly
   it — never only assert that identical tables match. It is trivially easy to
   write a diff tool that says "identical" for everything.
2. **Never claim a comparison is exact when it is approximate.** Float scale,
   unsupported types, and sampled modes must surface in the output, not just
   the docs.
