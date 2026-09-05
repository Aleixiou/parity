"""DuckDB dialect."""

from __future__ import annotations

import os
from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, NULL_SENTINEL, Dialect, map_type
from parity.types import Column, LogicalType


class DuckDBDialect(Dialect):
    name = "duckdb"

    def connect(self, connection_string: str) -> None:
        import duckdb

        # duckdb:///path/to.db  |  duckdb://:memory:  |  duckdb:///:memory:
        path = connection_string.split("://", 1)[1].lstrip("/")
        if not path or path == ":memory:":
            # An in-memory database holds no user data to protect, and a
            # read-only in-memory database is empty by definition.
            self._conn = duckdb.connect(":memory:")
            return
        if not os.path.exists(path):
            # read_only=True on a missing path fails with a driver-level error
            # that does not say which side or which file. Say it ourselves.
            raise self._err(f"database file not found: {path}")
        # CLAUDE.md section 6: read-only by construction. Enforcing it at the
        # connection means no bug in query building can ever write to a user's
        # database. It also lets several parity runs share one file.
        self._conn = duckdb.connect(path, read_only=True)

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        return self._conn.execute(sql).fetchall()

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def columns(self, table: str) -> list[Column]:
        schema, name = self.split_table(table, "main")
        rows = self.query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = {_lit(schema)} and table_name = {_lit(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(
                f"table not found: {table} (looked in schema {schema!r})"
            )
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

    # ----------------------------------------------------------- rendering

    def normalize(self, column: Column) -> str:
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as varchar)"
        elif t in (LogicalType.DECIMAL, LogicalType.FLOAT):
            expr = f"cast(cast({c} as decimal(38,{self.float_scale})) as varchar)"
        elif t is LogicalType.BOOLEAN:
            # `else` must not swallow NULL. With `case when c then 'true' else
            # 'false' end` a NULL boolean renders as 'false' - identical to a
            # real FALSE - so the coalesce below never fires and NULL-vs-FALSE
            # reports as a match. Both engines agreed on the wrong answer,
            # which is exactly why the encoding tests plant differences.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"strftime({c}, '%Y-%m-%d')"
        elif t is LogicalType.TIMESTAMP:
            expr = f"strftime({c}, '%Y-%m-%d %H:%M:%S.%f')"
        else:
            expr = f"cast({c} as varchar)"
        return f"coalesce({expr}, '{NULL_SENTINEL}')"

    def hash_expr(self, text_expr: str) -> str:
        return f"cast(('0x' || substr(md5({text_expr}), 1, {HASH_HEX_CHARS})) as bigint)"

    def int_div(self, numerator: str, denominator: str) -> str:
        # `//` is DuckDB's truncating integer division. Plain `/` would promote
        # to DOUBLE and silently lose precision on large key ranges.
        return f"(({numerator}) // ({denominator}))"

    def wide_int(self, expr: str) -> str:
        # hugeint is 128-bit, which the key offset cannot overflow.
        return f"cast(({expr}) as hugeint)"

    def sum_wide(self, expr: str) -> str:
        # DECIMAL(38,0) holds sums far beyond any realistic row count * 2^60.
        return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
