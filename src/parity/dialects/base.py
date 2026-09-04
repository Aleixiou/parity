"""Dialect contract.

A dialect knows three things about one database engine:

1. how to read a column's type and map it onto a :class:`LogicalType`
2. how to render a value as *canonical text* - the same bytes any other
   engine would produce for the same logical value
3. how to fold canonical text into a 60-bit integer and aggregate it

Everything else (segmentation, recursion, reporting) is engine-independent
and lives in :mod:`parity.engine`.

The 60-bit width is deliberate: it is the widest prefix of an MD5 hex digest
that both PostgreSQL's ``bit(n)::bigint`` cast and DuckDB's hex-string cast
render as the same *positive* signed 64-bit integer. At 64 bits PostgreSQL
wraps to negative and the two engines disagree.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Sequence

from parity.types import Column, LogicalType

# Field separator inside a row's canonical text. Unit Separator (0x1f) is
# chosen because it effectively never appears in warehouse string data;
# `chr(31)` is spelled the same way in every engine we support.
SEPARATOR_SQL = "chr(31)"
NULL_SENTINEL = "\\N"

# Number of MD5 hex characters folded into the row hash. 15 nibbles = 60 bits.
HASH_HEX_CHARS = 15


class Dialect(ABC):
    """One database engine's half of a comparison."""

    name: str

    #: Decimal places at which DECIMAL/FLOAT columns are compared. Both sides
    #: MUST use the same value or every float row reports as different.
    float_scale: int = 6

    # ---------------------------------------------------------------- setup

    @abstractmethod
    def connect(self, connection_string: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def query(self, sql: str) -> list[tuple[Any, ...]]: ...

    # ------------------------------------------------------------ metadata

    @abstractmethod
    def columns(self, table: str) -> list[Column]: ...

    @abstractmethod
    def quote(self, identifier: str) -> str: ...

    def qualify(self, table: str) -> str:
        """Quote a possibly schema-qualified table name."""
        return ".".join(self.quote(part) for part in table.split("."))

    # ----------------------------------------------------------- rendering

    @abstractmethod
    def normalize(self, column: Column) -> str:
        """SQL expression rendering ``column`` as canonical text.

        Implementations MUST be null-safe: a NULL value renders as
        :data:`NULL_SENTINEL`, never as SQL NULL.
        """

    @abstractmethod
    def hash_expr(self, text_expr: str) -> str:
        """SQL expression folding canonical text into a 60-bit integer."""

    @abstractmethod
    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. ``/`` is *not* portable: PostgreSQL
        truncates on integers, DuckDB promotes to double."""

    @abstractmethod
    def sum_wide(self, expr: str) -> str:
        """Sum ``expr`` in a type wide enough not to overflow.

        Row hashes are up to 2^60; summing millions of them overflows a
        64-bit accumulator, so both sides must aggregate in a 128-bit or
        arbitrary-precision type.
        """

    # ------------------------------------------------------------ building

    def row_text(self, columns: Sequence[Column]) -> str:
        parts = ", ".join(self.normalize(c) for c in columns)
        return f"concat_ws({SEPARATOR_SQL}, {parts})"

    def row_hash(self, columns: Sequence[Column]) -> str:
        return self.hash_expr(self.row_text(columns))

    # ------------------------------------------------------------- queries

    def key_bounds(self, table: str, key: str) -> tuple[int | None, int | None]:
        sql = (
            f"select min({self.quote(key)}), max({self.quote(key)}) "
            f"from {self.qualify(table)}"
        )
        lo, hi = self.query(sql)[0]
        return (None, None) if lo is None else (int(lo), int(hi))

    def segment_checksums(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> dict[int, tuple[int, int]]:
        """Return ``{segment_index: (row_count, checksum)}`` for ``[lo, hi)``.

        This is the whole point of the tool: one query per side per level,
        with the hashing pushed into the engine. Nothing but a handful of
        integers crosses the network.
        """
        k = self.quote(key)
        bucket = self.int_div(f"({k} - {lo}) * {n_segments}", f"({hi} - {lo})")
        sql = (
            f"select {bucket} as seg, count(*), "
            f"{self.sum_wide(self.row_hash(columns))} "
            f"from {self.qualify(table)} "
            f"where {k} >= {lo} and {k} < {hi} "
            f"group by 1"
        )
        return {
            int(seg): (int(count), int(checksum or 0))
            for seg, count, checksum in self.query(sql)
        }

    def fetch_range(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
    ) -> dict[int, tuple[str, ...]]:
        """Download canonical text for every row in ``[lo, hi)``.

        Only ever called on ranges the checksums already proved to differ,
        and only once they are small enough to be cheap.
        """
        k = self.quote(key)
        exprs = ", ".join(self.normalize(c) for c in columns)
        sql = (
            f"select {k}, {exprs} from {self.qualify(table)} "
            f"where {k} >= {lo} and {k} < {hi} order by {k}"
        )
        return {int(row[0]): tuple(row[1:]) for row in self.query(sql)}


def get_dialect(connection_string: str) -> Dialect:
    scheme = connection_string.split(":", 1)[0].lower()
    if scheme in ("duckdb",):
        from parity.dialects.duckdb_dialect import DuckDBDialect

        dialect: Dialect = DuckDBDialect()
    elif scheme in ("postgres", "postgresql"):
        from parity.dialects.postgres_dialect import PostgresDialect

        dialect = PostgresDialect()
    else:
        raise ValueError(
            f"No dialect for {scheme!r}. Supported: duckdb, postgres."
        )
    dialect.connect(connection_string)
    return dialect


def map_type(raw: str) -> LogicalType:
    """Map an engine's type name onto a logical category."""
    t = raw.lower().split("(")[0].strip()
    if t in {
        "int", "int2", "int4", "int8", "integer", "bigint", "smallint",
        "tinyint", "hugeint", "utinyint", "usmallint", "uinteger", "ubigint",
        "serial", "bigserial",
    }:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric"}:
        return LogicalType.DECIMAL
    if t in {"float", "float4", "float8", "double", "real", "double precision"}:
        return LogicalType.FLOAT
    if t in {"bool", "boolean"}:
        return LogicalType.BOOLEAN
    if t in {"date"}:
        return LogicalType.DATE
    if t.startswith("timestamp") or t.startswith("datetime"):
        return LogicalType.TIMESTAMP
    if t in {
        "text", "varchar", "char", "bpchar", "character varying",
        "character", "string", "uuid",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
