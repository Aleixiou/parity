# BUILD_SPEC.md — ordered build plan for `parity`

> Read `CLAUDE.md` first. It holds the verified cross-engine SQL you must use
> and the scope rules you must not break.

## Starting point

**Milestones 0–2 already have a working reference implementation** in
`src/parity/`, verified end-to-end against real DuckDB and PostgreSQL
instances (see CLAUDE.md §4.7 for exactly what was proven). `demo/proof.py`
reproduces the proof.

What exists: `types.py`, the `Dialect` base with shared query builders, the
PostgreSQL and DuckDB dialects, and the bisection engine.

What does **not** exist yet: any packaging, any CLI, any test suite, `git init`.
The reference code is proven correct on the happy path — it is not yet
hardened, and it has never been run by anyone but its author.

So Milestones 0–2 below are now **verify-and-harden**, not build-from-scratch.
Read the existing code first. If you think a design decision in it is wrong,
say so before changing it — several of them encode findings that were expensive
to discover and are documented in CLAUDE.md §4.

## How to run this

Do **one milestone at a time**, in order. After each, stop and show the test
output before starting the next. In Claude Code:

```
Read CLAUDE.md and BUILD_SPEC.md, then read the existing code in src/parity/.
Do Milestone 0 and Milestone 1, run the tests, and show me the output.
Do not start Milestone 2.
```

Every milestone has acceptance criteria. A milestone is not done until its
criteria pass — not until the code looks finished.

## Known gaps in the reference implementation

An honest list. Fix these as part of Milestones 0–2 — do not assume the
existing code is finished because it passes its proof script.

**Correctness holes (fix first):**

1. **Duplicate keys are silently wrong.** `fetch_range()` returns
   `{key: values}`, so if the key column is not unique, rows collapse and
   differences vanish. Detect non-unique keys and fail loudly with a clear
   message. This is the single most dangerous gap in the code.
2. **Non-integer keys are not validated.** The bisection arithmetic assumes an
   integer key. A `varchar` or `uuid` key will produce a confusing SQL error
   rather than "this key type isn't supported yet".
3. **DuckDB opens read-write.** `duckdb.connect(..., read_only=False)`
   contradicts CLAUDE.md §6's read-only guarantee. Set `read_only=True`, and
   handle the case where the file doesn't exist.
4. **`float_scale` is a per-dialect class attribute.** If the two sides ever
   disagree, *every* float row reports as different. Set it once, on both
   dialects, from a single source, and assert they match before diffing.
5. **`max_diffs` truncates mid-walk**, so the result is a partial answer that
   currently looks like a complete one. Mark the result as truncated.

**Missing infrastructure:** no CLI, no test suite, no packaging, no git repo,
no CI, no README.

**Rough edges:** the "table not found" error says `side A/B` because a dialect
doesn't know which side it is — pass the side in. Identifier case sensitivity
is untested. There is no query timeout or cancellation. Behaviour when the two
tables share no columns is undefined.

---

## Milestone 0 — Repo skeleton (verify and harden)

**Tasks**

1. `src/` layout package `parity`, `pyproject.toml` with setuptools backend,
   console script `parity = "parity.cli:main"`.
2. Optional extras: `duckdb`, `postgres`, `all` (see CLAUDE.md §6).
3. `.gitignore` (venv, `__pycache__`, `*.duckdb`, `demo/data/`), MIT `LICENSE`,
   placeholder `README.md`.
4. `git init`, first commit.
5. Bring up PostgreSQL locally (CLAUDE.md §7) and confirm both engines are
   reachable from Python.

**Acceptance:** `pip install -e ".[all]"` succeeds; `parity --help` runs;
a Python snippet connects to both DuckDB and PostgreSQL and prints their
versions.

---

## Milestone 1 — Dialect layer and canonical encoding

The correctness foundation. Get this wrong and every later result is a lie.

**Tasks**

1. `types.py` — `LogicalType` enum, `Column`, `TableRef`, `Segment`, `RowDiff`,
   `DiffStats`, `DiffResult` dataclasses.
2. `dialects/base.py` — the `Dialect` ABC (contract in CLAUDE.md §5), plus
   shared non-abstract helpers:
   - `row_text(columns)` → `concat_ws(chr(31), <normalized>...)`
   - `row_hash(columns)` → `hash_expr(row_text(...))`
   - `key_bounds(table, key)` → `(min, max)`
   - `segment_checksums(...)` and `fetch_range(...)` (Milestone 2 uses these)
   - `map_type(raw) -> LogicalType`, `get_dialect(conn_str) -> Dialect`
3. `dialects/postgres_dialect.py` and `dialects/duckdb_dialect.py` —
   implement **exactly** the expressions in CLAUDE.md §4.2 and §4.4.
   Introspect columns from `information_schema.columns` (both engines have it).
4. Identifier quoting: double quotes, internal `"` doubled. Schema-qualified
   names quote each part separately. This is the injection boundary — table and
   column names arrive from the CLI.
5. `tests/test_encoding.py` — **the verification harness.** Parametrised over
   every case in CLAUDE.md §4.2: insert the same literal into a DuckDB table and
   a PostgreSQL table, select the normalized expression from each, assert the
   strings are byte-identical. Cover at minimum: positive/negative integers;
   `1.5`, `1.0`, `-0.125`, `123456789.987654`; floats `0.1` and `1/3`; both
   booleans; a leap-year date; a microsecond timestamp; a whole-second
   timestamp; a Unicode string; NULL in every type; and the row hash of a
   multi-column concatenation.

**Acceptance:** every encoding case agrees across engines. The hash of a
multi-column row computed in DuckDB equals the same row's hash in PostgreSQL.
Skip (don't fail) cleanly when an engine isn't available.

---

## Milestone 2 — The bisection engine

Engine-agnostic. It must not import any dialect module.

**Algorithm**

```
diff(a, b, key, bisection_factor=32, threshold=10_000):
    cols = (columns_a ∩ columns_b) − {key}, sorted by name
    warn loudly about columns present on only one side, and about UNKNOWN types
    lo = min(min_key_a, min_key_b)
    hi = max(max_key_a, max_key_b) + 1        # half-open [lo, hi)
    queue = [(lo, hi)]
    while queue:
        (s_lo, s_hi) = queue.pop()
        if s_hi - s_lo <= 1 or estimated_rows <= threshold:
            download both sides for [s_lo, s_hi), compare in Python, emit diffs
            continue
        n = min(bisection_factor, s_hi - s_lo)
        cs_a = a.segment_checksums(..., s_lo, s_hi, n)     # ONE query
        cs_b = b.segment_checksums(..., s_lo, s_hi, n)     # ONE query
        for i in range(n):
            if cs_a.get(i, EMPTY) != cs_b.get(i, EMPTY):
                queue.append(bounds_of_bucket(i, s_lo, s_hi, n))
```

**The subtle part — bucket boundaries must invert the SQL exactly.**
SQL assigns `bucket = (key − lo) * n / (hi − lo)` using *truncating integer
division*. Python must compute the inverse with ceiling division:

```python
def bounds_of_bucket(i, lo, hi, n):
    span = hi - lo
    b_lo = lo + -(-(i * span) // n)          # ceil(i*span/n)
    b_hi = lo + -(-((i + 1) * span) // n)    # ceil((i+1)*span/n)
    return (b_lo, b_hi)
```

Get this wrong and the tool skips rows while reporting a clean match — the worst
possible failure mode. Test it directly: for random `(lo, hi, n)` and random
keys, assert Python's bucket assignment matches what the SQL expression returns
for every key.

**Row-level comparison.** For a small range, both sides return
`{key: (normalized values...)}`. Keys only in A → `only_in_a`; only in B →
`only_in_b`; present in both with differing tuples → `different`, and report
*which columns* differ by comparing element-wise.

**Tasks**

1. `engine.py` with `diff(...) -> DiffResult`, counting queries, segments
   checked, and rows downloaded into `DiffStats`.
2. Empty-segment handling: a bucket absent from both sides matches; absent from
   one side does not.
3. `tests/test_engine.py` — a `FakeDialect` backed by in-memory dicts so the
   bisection logic is tested without any database. Assert: identical tables
   produce zero diffs and download zero rows; a single changed row in 1M is
   found; a missing row and an extra row are classified correctly; the number of
   queries grows logarithmically, not linearly.

**Acceptance:** planted differences are found exactly — no false positives, no
misses. A 1M-row identical comparison downloads **zero** rows.

---

## Milestone 3 — CLI

```
parity diff --a CONN --a-table TABLE --b CONN --b-table TABLE --key COL
            [--columns a,b,c] [--exclude x,y]
            [--bisection-factor 32] [--threshold 10000] [--float-scale 6]
            [--max-diffs 100] [--json] [--quiet]
```

**Exit codes** (this is what makes it a CI check, so get them right):
`0` identical · `1` differences found · `2` error.

**Human output** — lead with the verdict, then the evidence:

```
✗ 3 differences found in 12,481,003 rows
  41 queries · 18,204 rows downloaded (0.15%) · 6.2s

  only in A   1 row    key 8471002
  only in B   1 row    key 9930011
  different   1 row    key 1200455   columns: amount, updated_at
      amount       A 100.000000              B 100.500000
      updated_at   A 2024-03-01 09:00:00.0   B 2024-03-01 09:30:00.0

  comparing floats at 6 decimal places
```

`--json` emits the same content as a machine-readable object. Always print the
rows-downloaded percentage — it is the proof that the tool did the clever thing
rather than dragging both tables across the network.

**Acceptance:** exit codes correct in all three cases; `--json` output parses;
a genuinely helpful error when a table or key column doesn't exist, naming which
side failed.

---

## Milestone 4 — The 10M-row proof

This milestone exists to produce **the artifact you show people**. Treat the
benchmark output as a deliverable, not a test.

**Tasks**

1. `demo/generate.py` — build an N-row dataset (default 10M) in both DuckDB and
   PostgreSQL with a realistic mixed schema: `id bigint`, `customer_id int`,
   `amount decimal(12,2)`, `status varchar`, `is_refunded boolean`,
   `created_at timestamp`, `note varchar` (some NULL). Use `generate_series` on
   both sides so the data is identical by construction, then plant differences
   via a `--plant` flag: one changed value, one deleted row, one extra row, one
   NULL-vs-empty-string trap.
2. `demo/benchmark.py` — run the diff, report wall time, query count, rows
   downloaded, and the percentage of the table transferred.
3. Verify: the tool finds **exactly** the planted differences, and the
   NULL-vs-`''` case is reported as a difference (this is where naive
   implementations silently pass — a real migration bug class).

**Acceptance:** on 10M rows the tool finds exactly the planted differences,
downloads well under 1% of the table, and completes in a time worth putting in a
README. Record the numbers.

---

## Milestone 5 — Ship it

1. `README.md`: the problem in two sentences, a 30-second quickstart, the
   benchmark numbers from Milestone 4, an honest limitations section (integer
   keys only for now, 6-decimal float comparison, supported types), and the
   supported-engine matrix.
2. GitHub Actions: run tests against DuckDB always, and PostgreSQL via a service
   container. Badge in the README.
3. `CONTRIBUTING.md` with a short "adding a dialect" guide — the whole
   architecture is designed to make that an 80-line file; say so, because
   dialect contributions are how this project grows without you.
4. Tag `v0.1.0`, publish to PyPI.

**Acceptance:** a stranger can go from `pip install parity` to a working
cross-engine diff using only the README.

---

## Deliberately deferred

Do not build these during the spike, but design so they remain possible:

- non-integer and composite primary keys (hash the key to an integer, keep the
  same bisection)
- Snowflake and BigQuery dialects — **the first commercially serious step**,
  once the core is proven
- sampling mode for approximate fast checks
- a dbt integration (`--select` a model, diff prod vs PR)
- continuous parity monitoring during a migration — this is the commercial shape,
  not the open-source tool

## Definition of done for the two-week spike

Milestones 0–4 complete, benchmark numbers recorded, repo public with a README
that a data engineer mid-migration would immediately understand.
