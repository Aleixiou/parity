"""Core value types shared across dialects and the diff engine.

BUILD_SPEC sketched a ``TableRef`` and a ``Segment`` dataclass. Neither
survived contact with the implementation: the engine takes a dialect, a
table name and a key as separate arguments rather than bundling them, and a
segment is a plain ``(lo, hi)`` tuple whose checksums live in the dict the
dialect returns. They were carried unused for a while, which is worse than
not having them - an unused public dataclass invites someone to build on it
and then drift out of sync with what the code really does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LogicalType(str, Enum):
    """Engine-independent type categories.

    Every physical column type a dialect knows about is mapped onto one of
    these, and each one has exactly one canonical text encoding (see
    ``Dialect.normalize``). Two rows are equal iff their canonical encodings
    are byte-identical, so this enum is where cross-engine correctness lives.
    """

    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Column:
    name: str
    logical_type: LogicalType
    raw_type: str = ""


@dataclass(frozen=True)
class KeyStats:
    """What one side reports about its key column, from a single scan.

    ``rows`` and ``distinct`` ride along with ``min``/``max`` because the
    min/max query already has to visit the key column. Getting uniqueness for
    free matters: a non-unique key silently collapses rows during comparison
    and makes differences vanish, which is the most dangerous failure this
    tool can have.
    """

    lo: int | None
    hi: int | None
    rows: int
    distinct: int
    #: Rows whose key is not NULL. ``None`` means the dialect did not measure
    #: it, in which case no NULL keys are assumed.
    non_null: int | None = None

    @property
    def empty(self) -> bool:
        return self.rows == 0

    @property
    def null_keys(self) -> int:
        return 0 if self.non_null is None else self.rows - self.non_null

    @property
    def has_null_keys(self) -> bool:
        return self.null_keys > 0

    @property
    def has_duplicate_keys(self) -> bool:
        # Compare like with like: `count(distinct k)` ignores NULLs, so
        # measuring it against `count(*)` would report every NULL key as a
        # duplicate and send the reader hunting for duplicates that do not
        # exist. NULL keys are diagnosed separately.
        comparable = self.rows if self.non_null is None else self.non_null
        return comparable != self.distinct


@dataclass
class RowDiff:
    key: int
    kind: str  # "only_in_a" | "only_in_b" | "different"
    columns: list[str] = field(default_factory=list)
    values_a: dict[str, str] = field(default_factory=dict)
    values_b: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffStats:
    queries: int = 0
    segments_checked: int = 0
    rows_downloaded: int = 0
    rows_compared_a: int = 0
    rows_compared_b: int = 0
    seconds: float = 0.0


@dataclass
class DiffResult:
    diffs: list[RowDiff]
    stats: DiffStats
    columns: list[Column]
    warnings: list[str] = field(default_factory=list)
    #: True when the walk stopped early (``max_diffs``), so ``diffs`` is a
    #: partial answer. CLAUDE.md section 8: never let an approximate result
    #: look exact. Callers must not read ``identical`` as "tables match" when
    #: this is set.
    truncated: bool = False
    #: Decimal places at which DECIMAL/FLOAT columns were compared. Surfaced
    #: in output so a reader knows the comparison was rounded, not exact.
    float_scale: int = 6

    @property
    def identical(self) -> bool:
        """No differences found *and* the whole key space was walked."""
        return not self.diffs and not self.truncated

    def by_kind(self, kind: str) -> list[RowDiff]:
        return [d for d in self.diffs if d.kind == kind]
