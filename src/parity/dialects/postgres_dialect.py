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
        self._conn = psycopg.connect(connection_string, autocommit=True)
        # Pin the session to UTC before anything else. `timestamptz` renders
        # through the *session* timezone, so two sides whose sessions differ
        # render the same instant as different text and every row with a
        # timestamptz reports as changed - a false positive that looks exactly
        # like catastrophic data loss. A timestamptz is an instant; comparing
        # instants in UTC is both correct and deterministic. Naive `timestamp`
        # columns carry no zone and are unaffected.
        #
        # Done while autocommit is still on, so it applies to the session
        # rather than to a transaction that later rolls back.
        self._conn.execute("set time zone 'UTC'")
        self._conn.autocommit = False
        # Read-only by construction (CLAUDE.md section 6). Enforced by the
        # server, so no bug in query building can write to a user's database.
        self._conn.read_only = True
        # REPEATABLE READ gives the whole diff one snapshot. Under the default
        # READ COMMITTED every statement sees a fresh snapshot, so a table
        # written to during the walk is a different table at each bisection
        # level - and the tool can then report a difference that never existed
        # at any single point in time, or descend into a range that has since
        # changed. The source side of a migration is live by definition, which
        # makes this the normal case rather than an edge case.
        #
        # The cost is one held snapshot for the duration of the diff, which
        # delays vacuuming dead tuples. At tens of seconds that is the same
        # cost as any analytical query, and far cheaper than an untrustworthy
        # verdict.
        self._conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        with self._conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[arg-type]
            return cur.fetchall()

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def columns(self, table: str) -> list[Column]:
        schema, name = self.split_table(table, "public")
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
            expr = f"({c})::text"
        elif t in (LogicalType.DECIMAL, LogicalType.FLOAT):
            expr = f"cast(round(({c})::numeric, {self.float_scale}) as text)"
        elif t is LogicalType.BOOLEAN:
            # `else` must not swallow NULL. With `case when c then 'true' else
            # 'false' end` a NULL boolean renders as 'false' - identical to a
            # real FALSE - so the coalesce below never fires and NULL-vs-FALSE
            # reports as a match. Both engines agreed on the wrong answer,
            # which is exactly why the encoding tests plant differences.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
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
        # `div()` is PostgreSQL's exact integer quotient and truncates toward
        # zero, matching Python's `//` for the non-negative operands used here.
        # Plain `/` is right for bigints but silently yields a scaled, rounded
        # result once `wide_int` has promoted an operand to numeric - which is
        # precisely when the bucket boundary has to be exact.
        return f"div(({numerator})::numeric, ({denominator})::numeric)"

    def wide_int(self, expr: str) -> str:
        # numeric is arbitrary precision: the key offset cannot overflow it.
        return f"(({expr})::numeric)"

    def sum_wide(self, expr: str) -> str:
        # numeric is arbitrary precision: cannot overflow no matter the row count.
        return f"coalesce(sum(({expr})::numeric), 0)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
