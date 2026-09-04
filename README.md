# parity

**Prove two tables in two different database engines hold the same data —
without moving the data out of either engine.**

```
parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

`parity` pushes hash aggregation down into both engines, compares a handful of
integers, binary-searches the key space to isolate where they diverge, and
downloads only the rows that actually differ. On identical tables it downloads
**zero rows**.

## Status

Early. This is a placeholder README — the real one lands in Milestone 5 with
benchmark numbers, a supported-engine matrix, and a full limitations section.

Working today: the dialect layer (PostgreSQL, DuckDB) and the bisection engine.
Not yet: the CLI (`parity diff` is stubbed until Milestone 3).

## Install

```bash
pip install -e ".[all]"      # both engines
pip install -e ".[duckdb]"   # DuckDB only, no Postgres driver
pip install -e ".[postgres]" # PostgreSQL only
```

## Limitations you must know before trusting a result

- **Integer keys only.** The bisection arithmetic assumes an integer key
  column. Other key types are rejected rather than guessed at.
- **Floats and decimals are compared at 6 decimal places.** Two values that
  differ only in the 7th decimal place are reported as *equal*. This is a
  deliberate cross-engine rounding contract, not an accident.
- **Keys must be unique.** A non-unique key column is detected and rejected —
  silently collapsing duplicate rows would hide real differences.
- Supported types: integer, decimal, float, boolean, string, date, timestamp.
  Anything else is compared as text and reported as a warning.

## Development

See `CLAUDE.md` for the verified cross-engine SQL facts and `BUILD_SPEC.md` for
the build plan.

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1      # Windows
pip install -e ".[all]" pytest
pytest
```

Tests that need PostgreSQL read `PARITY_TEST_PG` (default
`postgres://parity:parity@127.0.0.1:5432/parity`) and skip cleanly when no
server is reachable.

## License

MIT — see `LICENSE`.
