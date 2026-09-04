"""PostgreSQL dialect."""

from __future__ import annotations

from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, NULL_SENTINEL, Dialect, map_type
from parity.types import Column, LogicalType


class PostgresDialect(Dialect):
    name = "postgres"

    def connect(self, connection_string: str) -> None:
        import psycopg

        # psycopg understands postgres:// and postgresql:// URLs directly.
        self._conn = psycopg.connect(connection_string)
        self._conn.read_only = True

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        with self._conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[arg-type]
            return cur.fetchall()

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def columns(self, table: str) -> list[Column]:
        parts = table.split(".")
        schema, name = (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])
        rows = self.query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = {_lit(schema)} and table_name = {_lit(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise ValueError(f"[side A/B: postgres] table not found: {table}")
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

    # ----------------------------------------------------------- rendering

    def normalize(self, column: Column) -> str:
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"({c})::text"
        elif t in (LogicalType.DECIMAL, LogicalType.FLOAT):
            expr = f"cast(round(({c})::numeric, {self.float_scale}) as text)"
        elif t is LogicalType.BOOLEAN:
            expr = f"case when {c} then 'true' else 'false' end"
        elif t is LogicalType.DATE:
            expr = f"to_char({c}, 'YYYY-MM-DD')"
        elif t is LogicalType.TIMESTAMP:
            expr = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US')"
        else:
            expr = f"({c})::text"
        return f"coalesce({expr}, '{NULL_SENTINEL}')"

    def hash_expr(self, text_expr: str) -> str:
        return f"(('x' || substr(md5({text_expr}), 1, {HASH_HEX_CHARS}))::bit(60)::bigint)"

    def int_div(self, numerator: str, denominator: str) -> str:
        # PostgreSQL's `/` truncates toward zero on integer operands, which is
        # what we want. Both operands here are non-negative bigints.
        return f"(({numerator}) / ({denominator}))"

    def sum_wide(self, expr: str) -> str:
        # numeric is arbitrary precision: cannot overflow no matter the row count.
        return f"coalesce(sum(({expr})::numeric), 0)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
