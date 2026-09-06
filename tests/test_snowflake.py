"""Snowflake against DuckDB, end to end - the verification the draft needs.

DRAFT dialect (see src/parity/dialects/snowflake_dialect.py): Snowflake earns
the word "supported" only once this file passes against a live account, byte
for byte with DuckDB. It is the Snowflake twin of test_mysql.py, and the only
Snowflake-specific content is the fixture DDL and the three decisions the draft
made: the row hash goes through FLOOR(MD5_NUMBER_UPPER64(x)/16), integer and
decimal are split by numeric_scale, and the NULL sentinel is CHR(92)||'N'. Each
is verified below by the values agreeing with DuckDB, not by reading the SQL.

Skips cleanly unless PARITY_TEST_SNOWFLAKE points at a reachable account, so it
never runs in CI and never spends warehouse credits on its own. Run it with:

    pip install -e ".[duckdb,snowflake]" pytest
    set PARITY_TEST_SNOWFLAKE=snowflake://user:pw@account/PARITY_TEST/ENC?warehouse=PARITY_WH&role=PARITY_RO
    pytest tests/test_snowflake.py -v
"""

from __future__ import annotations

import pytest
from conftest import duckdb_write, open_duckdb, open_snowflake

from parity.engine import diff

pytestmark = pytest.mark.snowflake

N = 5_000

#: Side B: the DuckDB reference. Types line up with the Snowflake table below -
#: a real boolean on both sides (Snowflake has one, unlike MySQL), a decimal,
#: a naive timestamp - so any difference the tool reports is a real one, not a
#: type mismatch.
DUCKDB_TABLE = f"""
create table orders as
select i::bigint                                             as id,
       (i % 97)::integer                                     as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)             as amount,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end       as status,
       (i % 11 = 0)                                          as is_refunded,
       (timestamp '2024-01-01 00:00:00'
            + (i % 86400) * interval '1 second')             as created_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar end                   as note
from generate_series(1, {N}) as s(i)
"""

#: Side A: the Snowflake table. NUMBER(38,0) reports as an integer only because
#: its scale is 0 - the draft's columns() reads numeric_scale to tell it from
#: the decimal `amount`, and a bug there would render the key as `1.000000`.
SNOWFLAKE_TABLE = """
create or replace table orders (
    id          number(38,0),
    customer_id number(38,0),
    amount      number(12,2),
    status      varchar,
    is_refunded boolean,
    created_at  timestamp_ntz,
    note        varchar
)
"""

#: GENERATOR makes N rows; row_number() turns them into a stable 1..N key. id
#: is materialised in the subquery so each derived column reads one fixed value
#: rather than calling the sequence generator again mid-row.
SNOWFLAKE_FILL = f"""
insert into orders
select id,
       mod(id, 97),
       (mod(id * 7, 100000) / 100.0),
       case when mod(id, 3) = 0 then 'paid'
            when mod(id, 3) = 1 then 'open' else 'void' end,
       (mod(id, 11) = 0),
       dateadd(second, mod(id, 86400), '2024-01-01 00:00:00'::timestamp_ntz),
       case when mod(id, 13) = 0 then null else 'note ' || id::varchar end
from (
    select row_number() over (order by seq4()) as id
    from table(generator(rowcount => {N}))
)
"""


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B: the DuckDB reference table, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("snowflake_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_TABLE)
    finally:
        con.close()
    return path


def _build_snowflake(snowflake_url: str, plant: str | None) -> None:
    """Create the Snowflake `orders` table, optionally with one planted defect.

    Uses its own connection and commits (autocommit is on), because the dialect
    opened later must see the data. This is the *only* place the tests write to
    Snowflake; the parity dialect itself issues SELECT only.
    """
    a = open_snowflake(snowflake_url, side="A")
    cur = a._conn.cursor()
    try:
        cur.execute(SNOWFLAKE_TABLE)
        cur.execute("truncate table orders")
        cur.execute(SNOWFLAKE_FILL)
        if plant == "changed":
            cur.execute("update orders set amount = amount + 0.01 where id = 1234")
        elif plant == "deleted":
            cur.execute("delete from orders where id = 777")
        elif plant == "null_trap":
            # id 13 is a multiple of 13, so `note` is NULL on the DuckDB side;
            # setting it to '' here plants the NULL-versus-empty-string trap.
            cur.execute("update orders set note = '' where id = 13")
    finally:
        cur.close()
        a.close()


def _diff(snowflake_url: str, duck_path: str, **kwargs):
    """Diff the Snowflake orders table against the DuckDB one."""
    a = open_snowflake(snowflake_url, side="A")
    b = open_duckdb(duck_path, side="B")
    try:
        # Side A names the table as Snowflake stored it (unquoted -> ORDERS);
        # the key is given lower-case on purpose, to exercise the engine's
        # case-insensitive key/column matching against Snowflake's ID/AMOUNT.
        return diff(a, b, "ORDERS", "main.orders", "id", **kwargs)
    finally:
        a.close()
        b.close()


def test_the_hash_constant_agrees_with_the_other_engines(snowflake_url):
    """The whole cross-engine contract in one number.

    Snowflake reaches it through FLOOR(MD5_NUMBER_UPPER64(x)/16), a fourth
    distinct path after PostgreSQL's bit-cast, DuckDB's hex-cast and MySQL's
    CONV. If this disagrees, nothing else can be trusted.
    """
    a = open_snowflake(snowflake_url, side="A")
    try:
        got = a.query(f"select {a.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
        assert int(got) == 648541476951500027
    finally:
        a.close()


def test_identical_tables_match_and_download_nothing(snowflake_url, duck_path):
    """The headline claim, cross-engine: agreement moves zero rows."""
    _build_snowflake(snowflake_url, plant=None)
    result = _diff(snowflake_url, duck_path)
    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0


def test_a_changed_decimal_is_found_on_exactly_that_row(snowflake_url, duck_path):
    """A one-cent change on one row is reported as that row, that column."""
    _build_snowflake(snowflake_url, plant="changed")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(1234, "different")]
    # Columns are reported as side A (Snowflake) stores them - upper-cased.
    assert result.diffs[0].columns == ["AMOUNT"]


def test_a_deleted_row_is_reported_only_in_b(snowflake_url, duck_path):
    """A row missing from Snowflake is only_in_b, not an error."""
    _build_snowflake(snowflake_url, plant="deleted")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(777, "only_in_b")]


def test_null_versus_empty_string_is_caught(snowflake_url, duck_path):
    """The trap naive tools miss: NULL on one side, '' on the other."""
    _build_snowflake(snowflake_url, plant="null_trap")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(13, "different")]
    assert result.diffs[0].columns == ["NOTE"]
