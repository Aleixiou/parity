"""Core value types shared across dialects and the diff engine."""

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
class TableRef:
    """A table on one side of a comparison."""

    connection: str  # dialect-specific connection string
    table: str  # optionally schema-qualified
    key: str  # primary key column (integer for now)

    @property
    def dialect_name(self) -> str:
        return self.connection.split(":", 1)[0]


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

    @property
    def empty(self) -> bool:
        return self.rows == 0

    @property
    def has_duplicate_keys(self) -> bool:
        return self.rows != self.distinct


@dataclass
class Segment:
    """A half-open key range ``[lo, hi)`` and the checksum each side reports."""

    lo: int
    hi: int
    count_a: int = 0
    count_b: int = 0
    checksum_a: int = 0
    checksum_b: int = 0

    @property
    def matches(self) -> bool:
        return self.count_a == self.count_b and self.checksum_a == self.checksum_b

    @property
    def max_rows(self) -> int:
        return max(self.count_a, self.count_b)


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
