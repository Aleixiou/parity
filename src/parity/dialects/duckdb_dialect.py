"""DuckDB dialect."""

from __future__ import annotations

from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, NULL_SENTINEL, Dialect, map_type
from parity.types import Column, LogicalType


class DuckDBDialect(Dialect):
    name = "duckdb"

    def connect(self, connection_string: str) -> None:
        import duckdb

        # duckdb:///path/to.db  |  duckdb://:memory:  |  duckdb:///:memory:
        path = connection_string.split("://", 1)[1].lstrip("/")
        self._conn = duckdb.connect(path if path else ":memory:", read_only=False)

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        return self._conn.execute(sql).fetchall()

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def columns(self, table: str) -> list[Column]:
        parts = table.split(".")
        schema, name = (parts[0], parts[1]) if len(parts) == 2 else ("main", parts[0])
        rows = self.query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = {_lit(schema)} and table_name = {_lit(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise ValueError(f"[side A/B: duckdb] table not found: {table}")
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
            expr = f"case when {c} then 'true' else 'false' end"
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

    def sum_wide(self, expr: str) -> str:
        # DECIMAL(38,0) holds sums far beyond any realistic row count * 2^60.
        return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
