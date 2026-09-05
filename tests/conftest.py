"""Shared fixtures.

Every database-backed fixture skips - never fails - when its engine is not
reachable, so `pytest` is useful on a laptop with only DuckDB installed.
"""

from __future__ import annotations

import os

import pytest

from parity.dialects.base import get_dialect

#: CLAUDE.md section 7 documents a Docker container on port 55432. A native
#: install on 5432 is equally valid, so the endpoint is configurable.
PG_URL = os.environ.get(
    "PARITY_TEST_PG", "postgres://parity:parity@127.0.0.1:5432/parity"
)

#: Schema the tests own outright. Kept separate from `public` so a stray run
#: against a real database cannot collide with anything a user cares about.
PG_SCHEMA = "parity_test"


def _pg_available() -> tuple[bool, str]:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "psycopg is not installed"
    try:
        psycopg.connect(PG_URL, connect_timeout=5).close()
    except Exception as exc:  # pragma: no cover - depends on local services
        return False, f"no PostgreSQL at {PG_URL}: {type(exc).__name__}"
    return True, ""


def _duckdb_available() -> tuple[bool, str]:
    try:
        import duckdb  # noqa: F401  - importing it *is* the probe
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "duckdb is not installed"
    return True, ""


@pytest.fixture(scope="session")
def pg_url() -> str:
    ok, why = _pg_available()
    if not ok:
        pytest.skip(why)
    return PG_URL


@pytest.fixture(scope="session")
def duckdb_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    ok, why = _duckdb_available()
    if not ok:
        pytest.skip(why)
    return str(tmp_path_factory.mktemp("duckdb") / "parity_test.duckdb")


def duckdb_write(path: str):
    """Open a writable DuckDB connection for building fixture data.

    The dialect itself is read-only by construction (CLAUDE.md section 6), so
    fixtures must create their tables through the driver directly and close the
    handle before any dialect opens the file - DuckDB allows one writer.
    """
    import duckdb

    return duckdb.connect(path)


def open_duckdb(path: str, side: str = "B", float_scale: int = 6):
    return get_dialect(f"duckdb:///{path}", side=side, float_scale=float_scale)


def open_pg(url: str, side: str = "A", float_scale: int = 6):
    return get_dialect(url, side=side, float_scale=float_scale)
