"""Cross-engine canonical encoding agreement - the correctness foundation.

Two rows are equal *iff* their canonical text is byte-identical, so if these
tests pass for the wrong reason every later result is a lie. Three rules shape
this file:

1. Every positive assertion ("these agree") is paired with a **negative
   control** ("and the harness notices when they genuinely differ"). A
   comparison that returns "same" unconditionally would pass the positive half
   of this file trivially - and that is exactly the bug class a parity tool
   cannot afford (CLAUDE.md section 8).
2. The values are the ones listed in CLAUDE.md section 4.2, because those are
   the ones that were empirically verified.
3. Engines that are not reachable cause skips, never failures.
"""

from __future__ import annotations

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb, open_pg

from parity.dialects.base import (
    DEFAULT_FLOAT_SCALE,
    Dialect,
    get_dialect,
    map_type,
    require_matching_scales,
)
from parity.types import Column, LogicalType

# --------------------------------------------------------------------------
# Fixture schema. Only the physical type names differ between engines; every
# value literal below is spelled identically on both sides, which is itself
# part of what makes the comparison meaningful.
# --------------------------------------------------------------------------

#: table -> (postgres column type, duckdb column type)
VALUE_TABLES: dict[str, tuple[str, str]] = {
    "enc_integer": ("bigint", "bigint"),
    "enc_decimal": ("decimal(20,6)", "decimal(20,6)"),
    "enc_float": ("double precision", "double"),
    "enc_boolean": ("boolean", "boolean"),
    "enc_string": ("text", "varchar"),
    "enc_date": ("date", "date"),
    "enc_timestamp": ("timestamp", "timestamp"),
    # Deeper scale than we compare at, so the 6-decimal-place limitation can be
    # asserted rather than assumed.
    "enc_scale": ("decimal(20,10)", "decimal(20,10)"),
}

#: table -> [(row id, label, SQL literal)]
VALUE_ROWS: dict[str, list[tuple[int, str, str]]] = {
    "enc_integer": [
        (1, "zero", "0"),
        (2, "positive", "42"),
        (3, "negative", "-42"),
        # Spelled as a cast string literal: `-9223372036854775808` is parsed as
        # negation of a value one past bigint max and overflows on PostgreSQL.
        (4, "bigint_max", "cast('9223372036854775807' as bigint)"),
        (5, "bigint_min", "cast('-9223372036854775808' as bigint)"),
        (6, "null", "null"),
    ],
    "enc_decimal": [
        (1, "one_and_a_half", "1.5"),
        (2, "whole", "1.0"),
        (3, "negative_eighth", "-0.125"),
        (4, "wide", "123456789.987654"),
        (5, "zero", "0"),
        (6, "null", "null"),
    ],
    "enc_float": [
        (1, "tenth", "0.1"),
        (2, "one_third", "0.3333333333333333"),
        (3, "negative", "-2.5"),
        (4, "null", "null"),
        # Non-finite doubles are ordinary - any division by zero makes one -
        # and DuckDB cannot cast them to DECIMAL at all, so before they were
        # handled explicitly the whole diff died on them.
        (5, "infinity", "cast('Infinity' as double precision)"),
        (6, "negative_infinity", "cast('-Infinity' as double precision)"),
        (7, "nan", "cast('NaN' as double precision)"),
    ],
    "enc_boolean": [
        (1, "true", "true"),
        (2, "false", "false"),
        (3, "null", "null"),
    ],
    "enc_string": [
        (1, "ascii", "'hello'"),
        (2, "empty", "''"),
        (3, "unicode", "'héllo wörld 日本語 \U0001f389'"),
        (4, "quote", "'O''Brien'"),
        (5, "null", "null"),
    ],
    "enc_date": [
        (1, "leap_day", "date '2024-02-29'"),
        (2, "epoch", "date '1970-01-01'"),
        (3, "null", "null"),
    ],
    "enc_timestamp": [
        (1, "microseconds", "timestamp '2024-02-29 13:04:05.123456'"),
        (2, "whole_second", "timestamp '2024-01-01 00:00:00'"),
        (3, "null", "null"),
    ],
    "enc_scale": [
        # Differ only past the 6th decimal place: the documented limitation
        # says these compare EQUAL.
        (1, "beyond_scale_a", "1.0000000100"),
        (2, "beyond_scale_b", "1.0000000200"),
        # Differ at the 6th decimal place: these must compare DIFFERENT.
        (3, "within_scale_a", "1.0000010000"),
        (4, "within_scale_b", "1.0000020000"),
        (5, "null", "null"),
    ],
}

#: A mixed-type table for the row-hash test. Row 1 and row 2 differ in exactly
#: one column; row 3 is all-NULL; row 4 repeats row 1 but with an empty string
#: where row 1 has NULL - the migration bug class naive tools miss.
ROW_TABLE_COLUMNS: list[tuple[str, str, str]] = [
    # (column, postgres type, duckdb type)
    ("id", "bigint", "bigint"),
    ("customer_id", "integer", "integer"),
    ("amount", "decimal(12,2)", "decimal(12,2)"),
    ("status", "varchar(20)", "varchar(20)"),
    ("is_refunded", "boolean", "boolean"),
    ("created_at", "timestamp", "timestamp"),
    ("note", "text", "varchar"),
]

ROW_TABLE_ROWS: list[str] = [
    "1, 977, 100.00, 'paid', false, timestamp '2024-03-01 09:00:00.123456', null",
    # differs from row 1 in `amount` only
    "2, 977, 100.50, 'paid', false, timestamp '2024-03-01 09:00:00.123456', null",
    "3, null, null, null, null, null, null",
    # differs from row 1 in `note` only: '' versus NULL
    "4, 977, 100.00, 'paid', false, timestamp '2024-03-01 09:00:00.123456', ''",
]

ROW_TABLE = "enc_row"


def _ddl(table: str, engine_index: int) -> str:
    if table == ROW_TABLE:
        cols = ", ".join(f"{n} {t[engine_index]}" for n, *t in ROW_TABLE_COLUMNS)
        return f"create table {{q}} ({cols})"
    pg_type, duck_type = VALUE_TABLES[table]
    col_type = (pg_type, duck_type)[engine_index]
    return f"create table {{q}} (id integer, v {col_type})"


def _inserts(table: str) -> list[str]:
    if table == ROW_TABLE:
        return [f"insert into {{q}} values ({r})" for r in ROW_TABLE_ROWS]
    return [
        f"insert into {{q}} (id, v) values ({rid}, {lit})"
        for rid, _label, lit in VALUE_ROWS[table]
    ]


ALL_TABLES = [*VALUE_TABLES, ROW_TABLE]


@pytest.fixture(scope="session")
def duck(duckdb_path: str) -> Dialect:
    """Fixture data in DuckDB, then a read-only dialect over it.

    The dialect cannot create these tables: it is read-only by construction
    (CLAUDE.md section 6). So the writer connection builds the file and closes
    it before the dialect opens it - DuckDB permits only one writer.
    """
    con = duckdb_write(duckdb_path)
    try:
        for table in ALL_TABLES:
            con.execute(f"drop table if exists {table}")
            con.execute(_ddl(table, 1).format(q=table))
            for stmt in _inserts(table):
                con.execute(stmt.format(q=table))
    finally:
        con.close()

    dialect = open_duckdb(duckdb_path, side="B")
    yield dialect
    dialect.close()


@pytest.fixture(scope="session")
def pg(pg_url: str) -> Dialect:
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {PG_SCHEMA} cascade")
        con.execute(f"create schema {PG_SCHEMA}")
        for table in ALL_TABLES:
            q = f"{PG_SCHEMA}.{table}"
            con.execute(_ddl(table, 0).format(q=q))
            for stmt in _inserts(table):
                con.execute(stmt.format(q=q))
    finally:
        con.close()

    dialect = open_pg(pg_url, side="A")
    yield dialect
    dialect.close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _value_column(dialect: Dialect, table: str) -> Column:
    for col in dialect.columns(_table_name(dialect, table)):
        if col.name == "v":
            return col
    raise AssertionError(f"no column 'v' in {table} on side {dialect.side}")


def _table_name(dialect: Dialect, table: str) -> str:
    return f"{PG_SCHEMA}.{table}" if dialect.name == "postgres" else f"main.{table}"


def _normalized(dialect: Dialect, table: str, row_id: int) -> str:
    col = _value_column(dialect, table)
    sql = (
        f"select {dialect.normalize(col)} from {dialect.qualify(_table_name(dialect, table))} "
        f"where id = {row_id}"
    )
    rows = dialect.query(sql)
    assert rows, f"row {row_id} missing from {table} on side {dialect.side}"
    return rows[0][0]


def _row_hash(dialect: Dialect, row_id: int) -> int:
    table = _table_name(dialect, ROW_TABLE)
    cols = [c for c in dialect.columns(table) if c.name != "id"]
    cols.sort(key=lambda c: c.name)  # the engine compares columns name-sorted
    sql = (
        f"select {dialect.row_hash(cols)} from {dialect.qualify(table)} "
        f"where id = {row_id}"
    )
    return int(dialect.query(sql)[0][0])


def _row_text(dialect: Dialect, row_id: int) -> str:
    table = _table_name(dialect, ROW_TABLE)
    cols = [c for c in dialect.columns(table) if c.name != "id"]
    cols.sort(key=lambda c: c.name)
    sql = (
        f"select {dialect.row_text(cols)} from {dialect.qualify(table)} "
        f"where id = {row_id}"
    )
    return dialect.query(sql)[0][0]


CASE_IDS = [
    (table, rid, f"{table.removeprefix('enc_')}-{label}")
    for table, rows in VALUE_ROWS.items()
    for rid, label, _lit in rows
]


# --------------------------------------------------------------------------
# 1. Positive: every documented encoding agrees byte for byte
# --------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.parametrize(
    "table,row_id", [(t, r) for t, r, _ in CASE_IDS], ids=[i for _, _, i in CASE_IDS]
)
def test_normalized_text_agrees_across_engines(pg, duck, table: str, row_id: int):
    a = _normalized(pg, table, row_id)
    b = _normalized(duck, table, row_id)
    assert a == b, (
        f"{table} row {row_id}: postgres rendered {a!r}, duckdb rendered {b!r}. "
        f"Canonical text must be byte-identical or the row hashes diverge."
    )


@pytest.mark.postgres
def test_null_renders_as_sentinel_not_sql_null(pg, duck):
    """An un-coalesced NULL would poison the whole concatenation."""
    for table, rows in VALUE_ROWS.items():
        row_id = next(r for r, label, _ in rows if label == "null")
        for dialect in (pg, duck):
            got = _normalized(dialect, table, row_id)
            assert got == "\\N", (
                f"{table} NULL on side {dialect.side} rendered {got!r}, "
                f"expected the sentinel '\\\\N'"
            )


@pytest.mark.postgres
def test_row_hash_agrees_across_engines(pg, duck):
    """A whole multi-column row hashes to the same 60-bit integer on both."""
    for row_id in (1, 2, 3, 4):
        assert _row_text(pg, row_id) == _row_text(duck, row_id), (
            f"row {row_id}: concatenated canonical text differs between engines"
        )
        assert _row_hash(pg, row_id) == _row_hash(duck, row_id), (
            f"row {row_id}: row hash differs between engines"
        )


@pytest.mark.postgres
def test_row_hash_is_a_positive_60_bit_integer(pg, duck):
    """60 bits is the widest MD5 prefix both engines render the same *positive*
    signed 64-bit integer. A negative value here means someone widened it."""
    for dialect in (pg, duck):
        for row_id in (1, 2, 3, 4):
            h = _row_hash(dialect, row_id)
            assert 0 <= h < 2**60, f"side {dialect.side} row {row_id} hash {h}"


@pytest.mark.postgres
def test_documented_hash_constant(pg, duck):
    """CLAUDE.md section 4.1 pins this number. If it moves, the docs are wrong."""
    for dialect in (pg, duck):
        got = dialect.query(f"select {dialect.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
        assert int(got) == 648541476951500027, f"side {dialect.side} produced {got}"


@pytest.mark.postgres
def test_introspected_logical_types_agree(pg, duck):
    """The same DDL reports wildly different type names per engine
    (CLAUDE.md section 4.4). `map_type` must fold them onto the same category,
    or the two sides render the same value with different expressions."""
    pg_cols = {c.name: c for c in pg.columns(f"{PG_SCHEMA}.{ROW_TABLE}")}
    duck_cols = {c.name: c for c in duck.columns(f"main.{ROW_TABLE}")}
    assert set(pg_cols) == set(duck_cols)
    for name, a in pg_cols.items():
        b = duck_cols[name]
        assert a.logical_type is b.logical_type, (
            f"column {name}: postgres {a.raw_type!r} -> {a.logical_type}, "
            f"duckdb {b.raw_type!r} -> {b.logical_type}"
        )
        assert a.logical_type is not LogicalType.UNKNOWN, (
            f"column {name} ({a.raw_type!r}) fell through to UNKNOWN"
        )


# --------------------------------------------------------------------------
# 2. Negative controls: the planted differences.
#
# Without these the file above would pass against an encoder that returned a
# constant. Each test plants a difference and asserts it is visible.
# --------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.parametrize(
    "table,row_a,row_b",
    [
        ("enc_integer", 2, 3),  # 42 vs -42
        ("enc_decimal", 1, 2),  # 1.5 vs 1.0
        ("enc_float", 1, 2),  # 0.1 vs 1/3
        ("enc_boolean", 1, 2),  # true vs false
        ("enc_string", 1, 3),  # ascii vs unicode
        ("enc_date", 1, 2),  # leap day vs epoch
        ("enc_timestamp", 1, 2),  # microseconds vs whole second
    ],
)
def test_planted_difference_is_visible_across_engines(pg, duck, table, row_a, row_b):
    """Cross-engine comparison of two *different* values must not agree.

    This is the control that stops the positive tests passing trivially: if
    `normalize` collapsed everything to a constant, the tests above would be
    green and this one red.
    """
    assert _normalized(pg, table, row_a) != _normalized(duck, table, row_b)
    assert _normalized(duck, table, row_a) != _normalized(pg, table, row_b)


@pytest.mark.postgres
def test_null_and_empty_string_are_different(pg, duck):
    """The trap naive implementations fail, and a real migration bug class.

    Row 1 has `note` NULL; row 4 is identical except `note` is ''. If these
    hashed the same, a migration that turned NULLs into empty strings would
    pass a parity check.
    """
    null_text = _normalized(pg, "enc_string", 5)
    empty_text = _normalized(duck, "enc_string", 2)
    assert null_text == "\\N"
    assert empty_text == ""
    assert null_text != empty_text

    # And at whole-row level, across engines, in both directions.
    assert _row_hash(pg, 1) != _row_hash(duck, 4)
    assert _row_hash(duck, 1) != _row_hash(pg, 4)


@pytest.mark.postgres
def test_null_boolean_is_not_equal_to_false(pg, duck):
    """Regression: `case when c then 'true' else 'false' end` sent NULL down the
    `else` branch, so a NULL boolean rendered 'false' and compared *equal* to a
    real FALSE. Both engines produced the same wrong answer, so cross-engine
    agreement could not catch it - only planting the difference could."""
    null_bool = _normalized(pg, "enc_boolean", 3)
    false_bool = _normalized(duck, "enc_boolean", 2)
    assert null_bool == "\\N", f"NULL boolean rendered {null_bool!r}"
    assert false_bool == "false"
    assert null_bool != false_bool

    # And in the other direction, so neither dialect can regress alone.
    assert _normalized(duck, "enc_boolean", 3) != _normalized(pg, "enc_boolean", 2)
    # TRUE must still be unaffected by the fix.
    assert _normalized(pg, "enc_boolean", 1) == _normalized(duck, "enc_boolean", 1) == "true"


@pytest.mark.postgres
def test_single_changed_column_changes_the_row_hash(pg, duck):
    """Rows 1 and 2 differ only in `amount`. The row hash must move."""
    assert _row_hash(pg, 1) != _row_hash(duck, 2)
    assert _row_hash(duck, 1) != _row_hash(pg, 2)


@pytest.mark.postgres
def test_float_scale_limitation_is_real_and_bounded(pg, duck):
    """CLAUDE.md section 4.2 admits floats are compared at 6 decimal places.

    Assert both halves of that claim, because a silent precision assumption is
    how a parity tool loses trust: differences past the 6th place are *missed*
    (rows 1 and 2), differences at the 6th place are *caught* (rows 3 and 4).
    """
    beyond_a = _normalized(pg, "enc_scale", 1)
    beyond_b = _normalized(duck, "enc_scale", 2)
    assert beyond_a == beyond_b == "1.000000", (
        "values differing past 6 decimal places should collapse to the same "
        f"canonical text; got {beyond_a!r} and {beyond_b!r}"
    )

    within_a = _normalized(pg, "enc_scale", 3)
    within_b = _normalized(duck, "enc_scale", 4)
    assert within_a == "1.000001" and within_b == "1.000002"
    assert within_a != within_b


@pytest.mark.postgres
def test_separator_prevents_field_smearing(pg, duck):
    """Fields are joined with Unit Separator, so canonical text has exactly one
    separator per gap. Without it, ('ab','c') and ('a','bc') would collide."""
    for dialect in (pg, duck):
        text = _row_text(dialect, 1)
        n_cols = len(ROW_TABLE_COLUMNS) - 1  # `id` is the key, not compared
        assert text.count("\x1f") == n_cols - 1, (
            f"side {dialect.side}: expected {n_cols - 1} separators, got "
            f"{text.count(chr(31))} in {text!r}"
        )


# --------------------------------------------------------------------------
# 3. DuckDB-only checks - these still run on a machine with no PostgreSQL
# --------------------------------------------------------------------------


@pytest.mark.duckdb
def test_duckdb_hash_constant_without_postgres(duck):
    got = duck.query(f"select {duck.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
    assert int(got) == 648541476951500027


@pytest.mark.duckdb
def test_duckdb_dialect_is_read_only(duckdb_path, duck):
    """CLAUDE.md section 6 promises the tool never writes. Prove the connection
    itself refuses, so no query-building bug can violate it."""
    with pytest.raises(Exception) as exc:
        duck.query("create table should_not_exist (x integer)")
    assert "read-only" in str(exc.value).lower() or "read only" in str(exc.value).lower()


@pytest.mark.duckdb
def test_duckdb_missing_file_names_the_side(tmp_path):
    missing = tmp_path / "nope.duckdb"
    with pytest.raises(ValueError) as exc:
        get_dialect(f"duckdb:///{missing}", side="B")
    assert "side B" in str(exc.value)
    assert "not found" in str(exc.value)


@pytest.mark.duckdb
def test_duplicate_keys_are_rejected_not_silently_collapsed(tmp_path):
    """The single most dangerous gap: a non-unique key makes rows overwrite one
    another in the key->row mapping, and their differences vanish."""
    path = str(tmp_path / "dupes.duckdb")
    con = duckdb_write(path)
    con.execute("create table t (id bigint, v varchar)")
    con.execute("insert into t values (1, 'a'), (1, 'b'), (2, 'c')")
    con.close()

    d = open_duckdb(path)
    try:
        stats = d.key_stats("main.t", "id")
        assert stats.rows == 3 and stats.distinct == 2
        assert stats.has_duplicate_keys

        cols = [c for c in d.columns("main.t") if c.name != "id"]
        with pytest.raises(ValueError) as exc:
            d.fetch_range("main.t", "id", cols, 0, 10)
        assert "duplicate key 1" in str(exc.value)
        assert "side B" in str(exc.value)
    finally:
        d.close()


@pytest.mark.duckdb
def test_key_stats_on_an_empty_table(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    con = duckdb_write(path)
    con.execute("create table t (id bigint, v varchar)")
    con.close()

    d = open_duckdb(path)
    try:
        stats = d.key_stats("main.t", "id")
        assert stats.empty and not stats.has_duplicate_keys
        assert stats.lo is None and stats.hi is None
    finally:
        d.close()


@pytest.mark.duckdb
def test_non_integer_key_is_reported_clearly(tmp_path):
    path = str(tmp_path / "strkey.duckdb")
    con = duckdb_write(path)
    con.execute("create table t (id varchar, v integer)")
    con.execute("insert into t values ('a', 1)")
    con.close()

    d = open_duckdb(path)
    try:
        with pytest.raises(ValueError) as exc:
            d.key_stats("main.t", "id")
        msg = str(exc.value)
        assert "not an integer" in msg and "side B" in msg
    finally:
        d.close()


@pytest.mark.duckdb
def test_table_not_found_names_the_side_and_schema(duck):
    with pytest.raises(ValueError) as exc:
        duck.columns("main.no_such_table")
    msg = str(exc.value)
    assert "side B" in msg and "no_such_table" in msg


# --------------------------------------------------------------------------
# 4. Pure Python - no database required, so these always run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The verified table in CLAUDE.md section 4.4: same DDL, two engines,
        # two very different reported type names.
        ("BIGINT", LogicalType.INTEGER),
        ("bigint", LogicalType.INTEGER),
        ("INTEGER", LogicalType.INTEGER),
        ("integer", LogicalType.INTEGER),
        ("DECIMAL(12,2)", LogicalType.DECIMAL),
        ("numeric", LogicalType.DECIMAL),
        ("DOUBLE", LogicalType.FLOAT),
        ("double precision", LogicalType.FLOAT),
        ("VARCHAR", LogicalType.STRING),
        ("character varying", LogicalType.STRING),
        ("text", LogicalType.STRING),
        ("BOOLEAN", LogicalType.BOOLEAN),
        ("boolean", LogicalType.BOOLEAN),
        ("DATE", LogicalType.DATE),
        ("date", LogicalType.DATE),
        ("TIMESTAMP", LogicalType.TIMESTAMP),
        # PostgreSQL appends this, so the match has to be by prefix.
        ("timestamp without time zone", LogicalType.TIMESTAMP),
        ("timestamp with time zone", LogicalType.TIMESTAMP),
        # Planted difference: something genuinely unmapped must NOT quietly
        # claim to be a known type.
        ("json", LogicalType.UNKNOWN),
        ("bytea", LogicalType.UNKNOWN),
        ("int4range", LogicalType.UNKNOWN),
    ],
)
def test_map_type(raw: str, expected: LogicalType):
    assert map_type(raw) is expected


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("orders", '"orders"'),
        ("Orders", '"Orders"'),
        ("order count", '"order count"'),
        # The injection boundary: table and column names arrive from the CLI.
        ('a"b', '"a""b"'),
        ('x"; drop table y; --', '"x""; drop table y; --"'),
    ],
)
def test_quoting_is_injection_safe(identifier: str, expected: str):
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    for dialect in (DuckDBDialect(), PostgresDialect()):
        assert dialect.quote(identifier) == expected


def test_schema_qualified_names_quote_each_part():
    from parity.dialects.postgres_dialect import PostgresDialect

    d = PostgresDialect()
    assert d.qualify("public.orders") == '"public"."orders"'
    assert d.qualify("orders") == '"orders"'


def test_get_dialect_rejects_unknown_scheme():
    with pytest.raises(ValueError) as exc:
        get_dialect("mysql://user@host/db", side="A")
    assert "mysql" in str(exc.value) and "side A" in str(exc.value)


def test_float_scale_is_per_instance_not_shared():
    """As a class attribute, setting the scale on one side changed it for every
    dialect of that engine - or failed to, which is worse."""
    from parity.dialects.duckdb_dialect import DuckDBDialect

    a = DuckDBDialect(float_scale=2)
    b = DuckDBDialect()
    assert a.float_scale == 2
    assert b.float_scale == DEFAULT_FLOAT_SCALE

    col = Column("amount", LogicalType.DECIMAL, "decimal(12,2)")
    assert "decimal(38,2)" in a.normalize(col)
    assert f"decimal(38,{DEFAULT_FLOAT_SCALE})" in b.normalize(col)


def test_mismatched_float_scales_are_refused_before_any_query():
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    a = PostgresDialect(float_scale=6, side="A")
    b = DuckDBDialect(float_scale=2, side="B")
    with pytest.raises(ValueError) as exc:
        require_matching_scales(a, b)
    assert "float_scale differs" in str(exc.value)

    require_matching_scales(a, PostgresDialect(float_scale=6))  # no raise


def test_normalize_is_null_safe_for_every_logical_type():
    """Every branch of `normalize`, including the UNKNOWN fallback, must wrap
    in coalesce. One that does not would poison the whole concatenation."""
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    for dialect in (DuckDBDialect(), PostgresDialect()):
        for lt in LogicalType:
            expr = dialect.normalize(Column("c", lt, ""))
            assert expr.startswith("coalesce("), f"{dialect.name}/{lt}: {expr}"
            assert "'\\N'" in expr, f"{dialect.name}/{lt}: {expr}"


def test_row_text_uses_the_unit_separator():
    from parity.dialects.duckdb_dialect import DuckDBDialect

    d = DuckDBDialect()
    cols = [Column("a", LogicalType.STRING, ""), Column("b", LogicalType.INTEGER, "")]
    text = d.row_text(cols)
    assert text.startswith("concat_ws(chr(31), ")
    assert text.count("coalesce(") == 2


# --------------------------------------------------------------------------
# 5. Wide key ranges - the bucket expression must not overflow
# --------------------------------------------------------------------------


def test_the_bucket_expression_always_widens_before_multiplying():
    """`(key - lo) * n` overflows int64 once the span passes ~2.9e17.

    Widening unconditionally was measured at 10M rows to cost nothing outside
    noise, because MD5 over every row dominates. One always-correct path beats
    two paths in the function whose off-by-one would make the walker skip rows.
    """
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    cols = [Column("v", LogicalType.STRING, "varchar")]
    for dialect, widener in (
        (DuckDBDialect(), "hugeint"),
        (PostgresDialect(), "numeric"),
    ):
        for hi in (10**9, 2**62):
            bucket = dialect._segment_sql("t", "id", cols, 0, hi, 32).split(" as seg")[0]
            assert widener in bucket, (
                f"{dialect.name} did not widen the key offset for hi={hi}: {bucket}"
            )


@pytest.mark.duckdb
def test_a_key_span_spanning_the_whole_bigint_range_still_works(tmp_path):
    """Sparse bigint keys, and every hashed-key scheme, land here.

    Both engines raise on int64 overflow rather than wrapping, so before the
    fix this was a crash rather than a wrong answer - but a crash on a
    legitimate key space is still a hole.
    """
    import random

    rng = random.Random(11)
    keys = sorted(rng.randrange(0, 2**62) for _ in range(500))
    assert (keys[-1] - keys[0] + 1) * 32 > 2**63 - 1, "span too small to test"

    def make(path: str, changed: int | None = None) -> str:
        con = duckdb_write(path)
        con.execute("create table t (id bigint, v varchar)")
        con.executemany(
            "insert into t values (?, ?)",
            [(k, "CHANGED" if k == changed else f"v{i}") for i, k in enumerate(keys)],
        )
        con.close()
        return path

    a_path = make(str(tmp_path / "wide_a.duckdb"))
    b_path = make(str(tmp_path / "wide_b.duckdb"), changed=keys[250])

    from parity.engine import diff

    a, b = open_duckdb(a_path, side="A"), open_duckdb(b_path, side="B")
    try:
        result = diff(a, b, "main.t", "main.t", "id")
        assert [(d.key, d.kind) for d in result.diffs] == [(keys[250], "different")]
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------
# 6. Unusable key columns must be diagnosed correctly, not merely refused
# --------------------------------------------------------------------------


@pytest.mark.duckdb
def test_a_null_key_is_reported_as_null_not_as_a_duplicate(tmp_path):
    """`count(distinct k)` ignores NULLs, so a NULL key looks exactly like a
    duplicated one unless non-NULL rows are counted separately. Refusing with
    the wrong reason sends the reader hunting for duplicates that do not exist.
    """
    from parity.engine import diff

    def make(path: str, rows: str) -> str:
        con = duckdb_write(path)
        con.execute("create table t (id bigint, v varchar)")
        con.execute(f"insert into t values {rows}")
        con.close()
        return path

    a_path = make(str(tmp_path / "nullkey_a.duckdb"), "(1,'a'),(2,'b'),(null,'c')")
    b_path = make(str(tmp_path / "nullkey_b.duckdb"), "(1,'a'),(2,'b')")

    a, b = open_duckdb(a_path, side="A"), open_duckdb(b_path, side="B")
    try:
        stats = a.key_stats("main.t", "id")
        assert stats.rows == 3 and stats.non_null == 2 and stats.distinct == 2
        assert stats.has_null_keys and stats.null_keys == 1
        assert not stats.has_duplicate_keys, (
            "two distinct non-NULL keys is not a duplicate"
        )

        with pytest.raises(ValueError) as exc:
            diff(a, b, "main.t", "main.t", "id")
        msg = str(exc.value)
        assert "NULL" in msg and "side A" in msg
        assert "not unique" not in msg, f"misdiagnosed as duplicates: {msg}"
    finally:
        a.close()
        b.close()


@pytest.mark.duckdb
def test_duplicates_are_still_caught_when_no_key_is_null(tmp_path):
    """The negative control for the test above: separating the two checks must
    not stop real duplicates being caught."""
    path = str(tmp_path / "dupe_only.duckdb")
    con = duckdb_write(path)
    con.execute("create table t (id bigint, v varchar)")
    con.execute("insert into t values (1,'a'),(1,'b'),(2,'c')")
    con.close()

    d = open_duckdb(path)
    try:
        stats = d.key_stats("main.t", "id")
        assert not stats.has_null_keys
        assert stats.has_duplicate_keys
    finally:
        d.close()


@pytest.mark.parametrize(
    "table,expected",
    [("orders", ("main", "orders")), ("sales.orders", ("sales", "orders"))],
)
def test_table_names_split_into_schema_and_name(table, expected):
    from parity.dialects.duckdb_dialect import DuckDBDialect

    assert DuckDBDialect().split_table(table, "main") == expected


def test_a_three_part_table_name_is_refused_rather_than_guessed():
    """`db.schema.table` used to fall through to the unqualified branch and be
    looked up as a *table* named `db`, so the eventual "table not found" named
    a schema the user never typed."""
    from parity.dialects.postgres_dialect import PostgresDialect

    with pytest.raises(ValueError) as exc:
        PostgresDialect(side="A").split_table("db.public.orders", "public")
    msg = str(exc.value)
    assert "3 dot-separated parts" in msg and "side A" in msg


@pytest.mark.duckdb
def test_duckdb_sessions_are_pinned_to_utc(duck):
    assert duck.query("select current_setting('TimeZone')")[0][0] == "UTC"


@pytest.mark.postgres
def test_postgres_sessions_are_pinned_to_utc(pg):
    assert pg.query("show timezone")[0][0] == "UTC"


@pytest.mark.postgres
def test_non_finite_floats_render_identically(pg, duck):
    """Infinity and NaN are ordinary in float columns, and DuckDB cannot cast
    them to DECIMAL - it raised "Could not cast value inf to DECIMAL(38,6)"
    and killed the whole diff. Both engines must now spell them the same."""
    for row_id, expected in ((5, "Infinity"), (6, "-Infinity"), (7, "NaN")):
        a = _normalized(pg, "enc_float", row_id)
        b = _normalized(duck, "enc_float", row_id)
        assert a == b == expected, f"row {row_id}: postgres {a!r}, duckdb {b!r}"


@pytest.mark.postgres
def test_non_finite_floats_are_still_distinguishable(pg, duck):
    """The negative control: rendering them as tokens must not make them all
    equal to each other, or to a finite value."""
    tokens = {
        row_id: _normalized(pg, "enc_float", row_id) for row_id in (1, 5, 6, 7)
    }
    assert len(set(tokens.values())) == 4, tokens
    # And across engines, in both directions.
    assert _normalized(pg, "enc_float", 5) != _normalized(duck, "enc_float", 6)
    assert _normalized(duck, "enc_float", 7) != _normalized(pg, "enc_float", 1)


# --------------------------------------------------------------------------
# 7. Identifier case: exact matching, with a useful error when it bites
# --------------------------------------------------------------------------


@pytest.mark.duckdb
def test_identifiers_are_matched_exactly_including_case(tmp_path):
    """Identifiers are always quoted, so lookup is exact. That is correct -
    but unquoted SQL gets folded to lower case by the server, so asking for
    `Orders` when the server stored `orders` is an easy mistake to make and a
    miserable one to diagnose from "table not found" alone."""
    path = str(tmp_path / "case.duckdb")
    con = duckdb_write(path)
    con.execute('create table "Orders" (id bigint, "Amount" numeric)')
    con.execute('insert into "Orders" values (1, 1.5)')
    con.close()

    d = open_duckdb(path, side="B")
    try:
        # Exact name works, and preserves the column's own casing.
        assert [c.name for c in d.columns("Orders")] == ["id", "Amount"]

        # The wrong case fails - and says why.
        with pytest.raises(ValueError) as exc:
            d.columns("orders")
        msg = str(exc.value)
        assert "side B" in msg
        assert "including case" in msg
        assert "main.Orders" in msg, msg

        # A genuinely absent table gets no misleading suggestion.
        with pytest.raises(ValueError) as exc:
            d.columns("totally_absent")
        assert "did you mean" not in str(exc.value)
    finally:
        d.close()


@pytest.mark.duckdb
def test_a_column_whose_name_needs_quoting_round_trips(tmp_path):
    """Mixed case, spaces and an embedded double quote in a column name."""
    from parity.engine import diff

    weird = 'we"ird col'

    def make(path: str, value: str) -> str:
        con = duckdb_write(path)
        con.execute(f'create table t (id bigint, "Mixed Case" varchar, "{weird.replace(chr(34), chr(34) * 2)}" varchar)')
        con.execute(f"insert into t values (1, 'x', '{value}')")
        con.close()
        return path

    a_path = make(str(tmp_path / "q_a.duckdb"), "same")
    b_path = make(str(tmp_path / "q_b.duckdb"), "DIFFERENT")

    a, b = open_duckdb(a_path, side="A"), open_duckdb(b_path, side="B")
    try:
        names = [c.name for c in a.columns("main.t")]
        assert names == ["id", "Mixed Case", weird]

        result = diff(a, b, "main.t", "main.t", "id")
        assert [(d.key, d.kind) for d in result.diffs] == [(1, "different")]
        assert result.diffs[0].columns == [weird], result.diffs[0].columns
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "connection_string,expected",
    [
        # The slashes carry meaning, following the sqlite/SQLAlchemy convention.
        ("duckdb:///relative/path.duckdb", "relative/path.duckdb"),
        ("duckdb:///./demo/data/new.duckdb", "./demo/data/new.duckdb"),
        # Absolute POSIX: four slashes. Stripping them all made this relative,
        # so the tool reported "database file not found" for a file plainly
        # there - on every Linux and macOS machine, invisibly to Windows.
        ("duckdb:////tmp/abs.duckdb", "/tmp/abs.duckdb"),
        ("duckdb:////var/lib/warehouse.duckdb", "/var/lib/warehouse.duckdb"),
        # Absolute Windows: the drive letter supplies its own root.
        ("duckdb:///C:/data/warehouse.duckdb", "C:/data/warehouse.duckdb"),
        ("duckdb:///:memory:", ":memory:"),
        ("duckdb://:memory:", ":memory:"),
    ],
)
def test_duckdb_connection_string_paths(connection_string: str, expected: str):
    from parity.dialects.duckdb_dialect import duckdb_path

    assert duckdb_path(connection_string) == expected


@pytest.mark.duckdb
def test_an_absolute_path_opens(tmp_path):
    """End to end on whatever this platform calls an absolute path."""
    from parity.dialects.duckdb_dialect import duckdb_path

    target = tmp_path / "absolute.duckdb"
    con = duckdb_write(str(target))
    con.execute("create table t (id bigint)")
    con.close()

    # Exactly what conftest.open_duckdb builds, from an absolute path.
    connection_string = f"duckdb:///{target}"
    assert duckdb_path(connection_string) == str(target)

    d = get_dialect(connection_string, side="B")
    try:
        assert d.query("select count(*) from t")[0][0] == 0
    finally:
        d.close()
