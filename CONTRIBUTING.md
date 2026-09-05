# Contributing

The most valuable contribution is **a new dialect**. The architecture exists to
make that a single file of roughly eighty lines that touches nothing else.

## Adding a dialect

The bisection engine knows nothing about SQL. It talks to two `Dialect` objects
through a contract, so adding Snowflake or BigQuery means writing one new file
in `src/parity/dialects/` and registering it in `get_dialect()`. If you find
yourself editing `engine.py`, the abstraction is wrong — say so in an issue
rather than working around it.

### What you implement

```python
class Dialect(ABC):
    name: str
    def connect(self, connection_string: str) -> None: ...
    def close(self) -> None: ...
    def query(self, sql: str) -> list[tuple]: ...
    def columns(self, table: str) -> list[Column]: ...
    def quote(self, identifier: str) -> str: ...
    def normalize(self, column: Column) -> str:   # canonical text, null-safe
    def hash_expr(self, text_expr: str) -> str:   # -> 60-bit integer
    def int_div(self, num: str, den: str) -> str: # truncating division
    def sum_wide(self, expr: str) -> str:         # overflow-safe sum
```

The base class already builds the row text, the per-segment checksum query, and
the small-range fetch on top of those. Read
`src/parity/dialects/duckdb_dialect.py` first — it is the shortest complete
example.

### The five things that will bite you

Each of these cost real time to discover. They are documented at length in
`CLAUDE.md` §4; the short version:

1. **The hash must be exactly 60 bits.** 15 hex characters of an MD5 digest.
   That is the widest prefix both PostgreSQL and DuckDB render as the same
   *positive* signed 64-bit integer. At 64 bits PostgreSQL wraps negative and
   the engines disagree. Your dialect must produce `648541476951500027` for
   input `'abc'` — there is a test that checks exactly this.

2. **The sum must be widened.** Row hashes reach 2^60, so summing a few million
   overflows a 64-bit accumulator. Aggregate in `numeric`, `decimal(38,0)`, or
   whatever your engine's arbitrary-precision type is. And wrap it in
   `coalesce(..., 0)`: an empty segment returns SQL `NULL` from `sum()`, and an
   empty bucket on one side must compare equal to an empty bucket on the other
   or the walker recurses into nothing.

3. **`NULL` must render as the literal `\N`, never SQL NULL.** An un-coalesced
   NULL poisons the whole concatenation and silently masks differences. Watch
   `CASE` expressions especially: `case when c then 'true' else 'false' end`
   sends NULL down the `else` branch, so a NULL boolean renders `'false'` and
   compares equal to a real FALSE. Both engines agreed on that wrong answer for
   a while. Use `case when c then 'true' when not c then 'false' end`.

4. **`/` is not portable.** PostgreSQL truncates on integer operands, DuckDB
   promotes to double. That is what `int_div` is for. Do not reach for
   `floor(a/b)` — double precision silently breaks on large key ranges.

5. **Prefer summing to XOR.** `bit_xor` exists in most engines and silently
   cancels duplicate rows, which is precisely the difference you need to see.

### Proving it works

A dialect is not done until `tests/test_encoding.py` passes against it. That
file is the correctness contract: it inserts the same literal into your engine
and into a reference engine and asserts the canonical text is byte-identical.

Add your engine to the fixtures there and run:

```bash
pytest tests/test_encoding.py -v
```

**Every test must plant a difference.** A test that only asserts identical
tables match passes trivially for a completely broken tool — this is the single
rule the project cares most about. For each positive assertion ("these agree"),
add the negative control ("and the harness notices when they genuinely don't").
That discipline is what caught the boolean NULL bug in point 3 above.

## Running the checks

```bash
pip install -e ".[all]" pytest mypy ruff
pytest
ruff check src tests demo
mypy src/parity --strict --ignore-missing-imports
```

CI runs the static checks first, because they take seconds where the test
matrix takes minutes. `mypy --strict` is what turns "type hints everywhere"
from an aspiration into a fact, and ruff's `ISC`, `BLE` and `S` rules are on
deliberately: implicit string concatenation inside a collection is the
missing-comma bug class, a blind `except` has to be justified where it sits,
and this tool builds SQL by hand so injection rules earn their place. Where a
rule is knowingly not applicable, the ignore lives in `pyproject.toml` with the
reason next to it rather than being switched off globally.

PostgreSQL-backed tests read `PARITY_TEST_PG` and skip cleanly when nothing is
listening, so the suite is useful with only DuckDB installed.

## Scope

Before proposing a feature, check it against the question the tool exists to
answer: *"can I safely switch off the old system?"* Data quality rules,
freshness checks, lineage, cataloguing, orchestration, a web UI, and schema
migration are all deliberately out of scope. The open-source predecessor in
this category was abandoned because maintaining it grew expensive; a narrow
scope is the only defence.

Non-integer and composite keys, sampling mode, and a dbt integration are
planned and welcome.

## Style

- Python 3.10+, `from __future__ import annotations` at the top of every module.
- Type hints everywhere. Dataclasses for value types. No ORM — emitting dialect
  SQL deliberately is the product.
- No network calls, no telemetry. This tool points at production warehouses;
  trust is the whole distribution strategy.
- Read-only by construction. The tool issues `SELECT` only and never generates
  DDL or DML against a user's database.
- Comments explain *why*, especially for cross-engine workarounds — each one is
  a landmine for the next person.
- Errors must name the side and the table. `"table not found"` is useless when
  two databases are in play.
