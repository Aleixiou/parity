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

    @property
    def identical(self) -> bool:
        return not self.diffs

    def by_kind(self, kind: str) -> list[RowDiff]:
        return [d for d in self.diffs if d.kind == kind]
