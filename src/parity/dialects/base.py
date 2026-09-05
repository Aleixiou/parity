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

from parity.types import Column, KeyStats, LogicalType

# Field separator inside a row's canonical text. Unit Separator (0x1f) is
# chosen because it effectively never appears in warehouse string data;
# `chr(31)` is spelled the same way in every engine we support.
SEPARATOR_SQL = "chr(31)"
NULL_SENTINEL = "\\N"

# Number of MD5 hex characters folded into the row hash. 15 nibbles = 60 bits.
HASH_HEX_CHARS = 15

#: Decimal places at which DECIMAL/FLOAT columns are compared. A deliberate,
#: documented limitation - see CLAUDE.md section 4.2.
DEFAULT_FLOAT_SCALE = 6


class Dialect(ABC):
    """One database engine's half of a comparison."""

    name: str

    def __init__(
        self,
        float_scale: int = DEFAULT_FLOAT_SCALE,
        side: str = "?",
    ) -> None:
        if float_scale < 0:
            raise ValueError(f"float_scale must be >= 0, got {float_scale}")
        #: Decimal places at which DECIMAL/FLOAT columns are compared. This is
        #: per-instance, not per-class: as a class attribute a change on one
        #: side would leak to the other, or worse, not leak - and two sides
        #: rounding differently reports *every* float row as different. Use
        #: `require_matching_scales` before comparing.
        self.float_scale = float_scale
        #: "A" or "B". Carried only so errors can name which side failed;
        #: "table not found" is useless when two databases are in play.
        self.side = side

    def _err(self, message: str) -> ValueError:
        return ValueError(f"[side {self.side}: {self.name}] {message}")

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
        if not columns:
            # Two tables can legitimately share only their key - after
            # `--columns`/`--exclude`, or when the schemas have diverged
            # entirely. `concat_ws(chr(31), )` is a syntax error, so render a
            # constant instead. Row *contents* then always match, while
            # `count(*)` in the same checksum query still catches rows present
            # on one side only, which is the only difference left to find.
            return "''"
        parts = ", ".join(self.normalize(c) for c in columns)
        return f"concat_ws({SEPARATOR_SQL}, {parts})"

    def row_hash(self, columns: Sequence[Column]) -> str:
        return self.hash_expr(self.row_text(columns))

    # ------------------------------------------------------------- queries

    def key_stats(self, table: str, key: str) -> KeyStats:
        """Key range plus row and distinct-key counts, in one scan.

        ``count(distinct key)`` rides along with min/max deliberately. The
        query already has to visit the key column, and without the distinct
        count a non-unique key goes undetected: `fetch_range` returns a dict
        keyed by the key column, so duplicate rows collapse and their
        differences disappear. Paying for it here costs one aggregation, not
        an extra round trip.
        """
        k = self.quote(key)
        sql = (
            f"select min({k}), max({k}), count(*), count(distinct {k}) "
            f"from {self.qualify(table)}"
        )
        lo, hi, rows, distinct = self.query(sql)[0]
        if lo is None:
            return KeyStats(None, None, 0, 0)
        try:
            return KeyStats(int(lo), int(hi), int(rows), int(distinct))
        except (TypeError, ValueError) as exc:
            # A varchar or uuid key lands here. The bisection arithmetic is
            # integer-only, so say that plainly instead of leaking a cast error.
            raise self._err(
                f"key column {key!r} in {table} is not an integer "
                f"(min value {lo!r}); only integer keys are supported"
            ) from exc

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
        # With no comparable columns the row tuple is empty and only key
        # presence distinguishes the sides - see `row_text`.
        exprs = "".join(", " + self.normalize(c) for c in columns)
        sql = (
            f"select {k}{exprs} from {self.qualify(table)} "
            f"where {k} >= {lo} and {k} < {hi} order by {k}"
        )
        out: dict[int, tuple[str, ...]] = {}
        for row in self.query(sql):
            rk = int(row[0])
            if rk in out:
                # Second line of defence behind the up-front uniqueness check
                # in `key_stats`: a key that duplicates only inside a fetched
                # range would otherwise overwrite the earlier row and silently
                # drop a real difference.
                raise self._err(
                    f"duplicate key {rk} in {table}: key column {key!r} is not "
                    f"unique, so rows cannot be compared one-to-one"
                )
            out[rk] = tuple(row[1:])
        return out


def get_dialect(
    connection_string: str,
    side: str = "?",
    float_scale: int = DEFAULT_FLOAT_SCALE,
) -> Dialect:
    """Open a connection and return the dialect for it.

    Drivers are imported lazily so a DuckDB-only user never needs a PostgreSQL
    driver installed, and vice versa.
    """
    scheme = connection_string.split(":", 1)[0].lower()
    if scheme in ("duckdb",):
        from parity.dialects.duckdb_dialect import DuckDBDialect

        dialect: Dialect = DuckDBDialect(float_scale=float_scale, side=side)
    elif scheme in ("postgres", "postgresql"):
        from parity.dialects.postgres_dialect import PostgresDialect

        dialect = PostgresDialect(float_scale=float_scale, side=side)
    else:
        raise ValueError(
            f"[side {side}] no dialect for scheme {scheme!r}. "
            f"Supported: duckdb, postgres."
        )
    dialect.connect(connection_string)
    return dialect


def require_matching_scales(a: Dialect, b: Dialect) -> None:
    """Refuse to compare two sides that round floats differently.

    If the scales disagree, every DECIMAL and FLOAT row renders to different
    canonical text and the tool reports the whole table as changed. That looks
    like a catastrophic migration bug rather than a configuration mistake, so
    fail before any query runs.
    """
    if a.float_scale != b.float_scale:
        raise ValueError(
            f"float_scale differs between sides: A={a.float_scale} "
            f"B={b.float_scale}. Both sides must round identically or every "
            f"float and decimal row reports as different."
        )


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
