"""An in-memory ``Dialect`` so the bisection engine can be tested with no database.

Two things make this worth the code:

1. It proves the engine really is engine-agnostic. ``FakeDialect.query`` raises,
   so if the engine ever reached past the dialect contract to build SQL itself,
   every test here would fail loudly.
2. It counts round trips and rows served, which is how the "downloads zero rows
   on identical tables" and "queries grow logarithmically" claims get asserted
   rather than assumed.

The bucket arithmetic below deliberately mirrors the SQL expression in
``Dialect.segment_checksums`` character for character in intent:
``(key - lo) * n / (hi - lo)`` with truncating division. If the two ever drift,
``test_engine.py`` catches it - that is the point of the property test there.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, NULL_SENTINEL, Dialect
from parity.types import Column, KeyStats, LogicalType


def row_hash(text: str) -> int:
    """The same fold the real dialects perform, in Python.

    15 hex characters = 60 bits, matching CLAUDE.md section 4.1.
    """
    # MD5 here is the checksum both engines compute, chosen for
    # cross-engine agreement, not for any security property.
    digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:HASH_HEX_CHARS], 16)


class Table:
    """Rows addressed by an integer key.

    Subclasses either hold a dict or generate rows on demand; the generated
    flavour is what makes a million-row test run in a second and a few
    megabytes instead of gigabytes.
    """

    columns: list[Column]

    def keys_in(self, lo: int, hi: int) -> Iterable[int]:
        raise NotImplementedError

    def row(self, key: int) -> tuple[str, ...]:
        raise NotImplementedError

    def all_keys(self) -> Iterable[int]:
        raise NotImplementedError

    # -- derived -----------------------------------------------------------

    def key_stats(self) -> KeyStats:
        keys = list(self.all_keys())
        if not keys:
            return KeyStats(None, None, 0, 0)
        return KeyStats(min(keys), max(keys), len(keys), len(set(keys)))

    def text(self, key: int, columns: Sequence[Column]) -> str:
        """Canonical row text, restricted to ``columns`` and in their order."""
        if not columns:
            return ""  # mirrors Dialect.row_text's empty-column fallback
        index = {c.name: i for i, c in enumerate(self.columns)}
        values = self.row(key)
        return "\x1f".join(values[index[c.name]] for c in columns)


class DictTable(Table):
    """A handful of rows spelled out literally. For readable small tests."""

    def __init__(self, columns: list[Column], rows: dict[int, tuple[str, ...]]):
        self.columns = columns
        self.rows = dict(rows)

    def keys_in(self, lo: int, hi: int) -> Iterable[int]:
        return [k for k in self.rows if lo <= k < hi]

    def row(self, key: int) -> tuple[str, ...]:
        return self.rows[key]

    def all_keys(self) -> Iterable[int]:
        return list(self.rows)


class SyntheticTable(Table):
    """Keys ``1..n`` generated on the fly, with planted differences.

    Memory is O(number of planted differences), not O(n), so a comparison of
    two million-row tables costs a few kilobytes.
    """

    def __init__(
        self,
        n: int,
        columns: list[Column] | None = None,
        changed: dict[int, tuple[str, ...]] | None = None,
        deleted: Iterable[int] = (),
        extra: dict[int, tuple[str, ...]] | None = None,
    ):
        self.n = n
        self.columns = columns or [
            Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
            Column("status", LogicalType.STRING, "varchar"),
        ]
        self.changed = dict(changed or {})
        self.deleted = set(deleted)
        self.extra = dict(extra or {})

    # The generated row for a key. Deterministic, so both sides agree unless a
    # difference was deliberately planted.
    def _generated(self, key: int) -> tuple[str, ...]:
        return (f"{key % 1000}.{key % 100:02d}", f"status-{key % 7}")

    def row(self, key: int) -> tuple[str, ...]:
        if key in self.extra:
            return self.extra[key]
        if key in self.changed:
            return self.changed[key]
        return self._generated(key)

    def keys_in(self, lo: int, hi: int) -> Iterator[int]:
        start, stop = max(1, lo), min(self.n + 1, hi)
        for k in range(start, stop):
            if k not in self.deleted:
                yield k
        for k in sorted(self.extra):
            if lo <= k < hi and not (1 <= k <= self.n):
                yield k

    def all_keys(self) -> Iterator[int]:
        return self.keys_in(1, max(self.n, *self.extra) + 1 if self.extra else self.n + 1)


class FakeDialect(Dialect):
    """A ``Dialect`` over a :class:`Table`. Serves one table only."""

    name = "fake"

    def __init__(
        self,
        table: Table,
        side: str = "A",
        float_scale: int = 6,
        key_type_override: Column | None = None,
    ):
        super().__init__(float_scale=float_scale, side=side)
        self.table = table
        self.key_column = key_type_override or Column("id", LogicalType.INTEGER, "bigint")
        #: Round trips this side served. The engine's own counter is checked
        #: against the sum of both sides' counters.
        self.queries = 0
        #: Rows actually handed back, i.e. what crossed the "network".
        self.rows_served = 0

    # -- contract ----------------------------------------------------------

    def connect(self, connection_string: str) -> None:  # pragma: no cover
        pass

    def close(self) -> None:
        pass

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        raise AssertionError(
            "the engine built SQL directly instead of going through the "
            f"dialect contract: {sql!r}"
        )

    def columns(self, table: str) -> list[Column]:
        return [self.key_column, *self.table.columns]

    def quote(self, identifier: str) -> str:
        return f'"{identifier}"'

    def normalize(self, column: Column) -> str:  # pragma: no cover
        return f"norm({column.name})"

    def hash_expr(self, text_expr: str) -> str:  # pragma: no cover
        return f"hash({text_expr})"

    def int_div(self, numerator: str, denominator: str) -> str:  # pragma: no cover
        return f"(({numerator}) // ({denominator}))"

    def sum_wide(self, expr: str) -> str:  # pragma: no cover
        return f"sum({expr})"

    def wide_int(self, expr: str) -> str:  # pragma: no cover
        # Python integers are already arbitrary precision.
        return f"({expr})"

    # -- the three operations the engine actually calls ---------------------

    def key_stats(self, table: str, key: str) -> KeyStats:
        self.queries += 1
        return self.table.key_stats()

    def segment_checksums(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> dict[int, tuple[int, int]]:
        self.queries += 1
        span = hi - lo
        out: dict[int, list[int]] = {}
        for k in self.table.keys_in(lo, hi):
            # Exactly the SQL expression: truncating integer division.
            bucket = ((k - lo) * n_segments) // span
            acc = out.setdefault(bucket, [0, 0])
            acc[0] += 1
            acc[1] += row_hash(self.table.text(k, columns))
        return {i: (c, s) for i, (c, s) in out.items()}

    def fetch_range(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
    ) -> dict[int, tuple[str, ...]]:
        self.queries += 1
        index = {c.name: i for i, c in enumerate(self.table.columns)}
        out: dict[int, tuple[str, ...]] = {}
        for k in sorted(self.table.keys_in(lo, hi)):
            if k in out:
                raise self._err(f"duplicate key {k} in {table}")
            values = self.table.row(k)
            out[k] = tuple(values[index[c.name]] for c in columns)
        self.rows_served += len(out)
        return out


__all__ = [
    "NULL_SENTINEL",
    "DictTable",
    "FakeDialect",
    "SyntheticTable",
    "Table",
    "row_hash",
]
