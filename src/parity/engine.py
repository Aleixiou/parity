"""Engine-agnostic segmented diff.

Nothing in this module may import a dialect. It talks to two ``Dialect``
objects through their contract and knows nothing about SQL.

The strategy: split the key range into buckets, ask each side for one
checksum per bucket (one query per side per level), and recurse only into
buckets whose checksums disagree. Rows are downloaded solely from ranges
already proven to differ, once those ranges are small.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from parity.dialects.base import Dialect, require_matching_scales
from parity.types import Column, DiffResult, DiffStats, LogicalType, RowDiff

EMPTY = (0, 0)


def bucket_bounds(i: int, lo: int, hi: int, n: int) -> tuple[int, int]:
    """Key range of bucket ``i``, inverting the SQL bucket expression.

    SQL computes ``bucket = (key - lo) * n / (hi - lo)`` with *truncating*
    integer division. The inverse of that is ceiling division. Getting this
    wrong makes the walker skip key ranges while still reporting a clean
    match - the worst failure this tool can have - so it is isolated here and
    tested directly against the engines in ``tests/test_engine.py``.
    """
    span = hi - lo
    b_lo = lo + -(-(i * span) // n)
    b_hi = lo + -(-((i + 1) * span) // n)
    return b_lo, b_hi


def diff(
    a: Dialect,
    b: Dialect,
    a_table: str,
    b_table: str,
    key: str,
    columns: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    bisection_factor: int = 32,
    threshold: int = 10_000,
    max_diffs: int | None = None,
) -> DiffResult:
    started = time.perf_counter()
    stats = DiffStats()
    require_matching_scales(a, b)

    cols_a = {c.name: c for c in a.columns(a_table)}
    cols_b = {c.name: c for c in b.columns(b_table)}
    if key not in cols_a:
        raise ValueError(f"[side A] key column {key!r} not in {a_table}")
    if key not in cols_b:
        raise ValueError(f"[side B] key column {key!r} not in {b_table}")
    for side, table, col in (("A", a_table, cols_a[key]), ("B", b_table, cols_b[key])):
        if col.logical_type is not LogicalType.INTEGER:
            # The bisection arithmetic divides the key range. A varchar or uuid
            # key would produce a confusing SQL cast error deep in the walk.
            raise ValueError(
                f"[side {side}] key column {key!r} in {table} is "
                f"{col.raw_type or col.logical_type.value}, not an integer. "
                f"Only integer keys are supported."
            )

    shared = sorted((set(cols_a) & set(cols_b)) - {key} - set(exclude))
    if columns:
        missing = set(columns) - set(shared)
        if missing:
            raise ValueError(f"columns not present on both sides: {sorted(missing)}")
        shared = [c for c in shared if c in set(columns)]

    warnings: list[str] = []
    for side, only in (("A", set(cols_a) - set(cols_b)), ("B", set(cols_b) - set(cols_a))):
        if only:
            warnings.append(f"ignored: columns only on side {side}: {sorted(only)}")
    unknown = [c for c in shared if cols_a[c].logical_type is LogicalType.UNKNOWN]
    if unknown:
        warnings.append(f"unmapped types compared as text: {unknown}")

    a_cols = [cols_a[c] for c in shared]
    b_cols = [cols_b[c] for c in shared]

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(a.key_stats, a_table, key)
        fb = pool.submit(b.key_stats, b_table, key)
        ks_a, ks_b = fa.result(), fb.result()
    stats.queries += 2

    # A non-unique key is fatal, not a warning: `fetch_range` maps key -> row,
    # so duplicates collapse and their differences disappear. Reporting
    # "identical" for a table we could not actually compare is the one outcome
    # this tool must never produce.
    for side, table, ks in (("A", a_table, ks_a), ("B", b_table, ks_b)):
        if ks.has_duplicate_keys:
            raise ValueError(
                f"[side {side}] key column {key!r} in {table} is not unique: "
                f"{ks.rows:,} rows but only {ks.distinct:,} distinct keys. "
                f"Rows cannot be compared one-to-one."
            )
    stats.rows_compared_a, stats.rows_compared_b = ks_a.rows, ks_b.rows

    bounds = [v for v in (ks_a.lo, ks_a.hi, ks_b.lo, ks_b.hi) if v is not None]
    if not bounds:
        stats.seconds = time.perf_counter() - started
        return DiffResult([], stats, a_cols, warnings, float_scale=a.float_scale)

    lo, hi = min(bounds), max(bounds) + 1
    diffs: list[RowDiff] = []
    queue: list[tuple[int, int]] = [(lo, hi)]

    truncated = False
    while queue:
        if max_diffs is not None and len(diffs) >= max_diffs:
            # The remaining queue is unexplored, so this answer is partial.
            # Flag it on the result, not only in a warning string, so no caller
            # can mistake it for a clean comparison.
            warnings.append(
                f"stopped after {max_diffs} differences (--max-diffs); "
                f"{len(queue)} key range(s) left unchecked"
            )
            truncated = True
            break

        s_lo, s_hi = queue.pop()
        span = s_hi - s_lo
        if span <= 0:
            continue
        stats.segments_checked += 1

        if span <= 1:
            _compare_rows(a, b, a_table, b_table, key, a_cols, b_cols, s_lo, s_hi, diffs, stats)
            continue

        n = min(bisection_factor, span)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(a.segment_checksums, a_table, key, a_cols, s_lo, s_hi, n)
            fb = pool.submit(b.segment_checksums, b_table, key, b_cols, s_lo, s_hi, n)
            cs_a, cs_b = fa.result(), fb.result()
        stats.queries += 2

        for i in range(n):
            va, vb = cs_a.get(i, EMPTY), cs_b.get(i, EMPTY)
            if va == vb:
                continue
            b_lo_i, b_hi_i = bucket_bounds(i, s_lo, s_hi, n)
            if max(va[0], vb[0]) <= threshold or b_hi_i - b_lo_i <= 1:
                _compare_rows(
                    a, b, a_table, b_table, key, a_cols, b_cols,
                    b_lo_i, b_hi_i, diffs, stats,
                )
            else:
                queue.append((b_lo_i, b_hi_i))

    stats.seconds = time.perf_counter() - started
    diffs.sort(key=lambda d: d.key)
    return DiffResult(
        diffs, stats, a_cols, warnings,
        truncated=truncated, float_scale=a.float_scale,
    )


def _compare_rows(
    a: Dialect,
    b: Dialect,
    a_table: str,
    b_table: str,
    key: str,
    a_cols: list[Column],
    b_cols: list[Column],
    lo: int,
    hi: int,
    diffs: list[RowDiff],
    stats: DiffStats,
) -> None:
    """Download a proven-different range from both sides and diff it locally."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(a.fetch_range, a_table, key, a_cols, lo, hi)
        fb = pool.submit(b.fetch_range, b_table, key, b_cols, lo, hi)
        rows_a, rows_b = fa.result(), fb.result()
    stats.queries += 2
    stats.rows_downloaded += len(rows_a) + len(rows_b)

    names = [c.name for c in a_cols]
    for k in sorted(set(rows_a) | set(rows_b)):
        ra, rb = rows_a.get(k), rows_b.get(k)
        if ra is None:
            diffs.append(RowDiff(k, "only_in_b", names, {}, dict(zip(names, rb or ()))))
        elif rb is None:
            diffs.append(RowDiff(k, "only_in_a", names, dict(zip(names, ra)), {}))
        elif ra != rb:
            changed = [n for n, x, y in zip(names, ra, rb) if x != y]
            diffs.append(
                RowDiff(
                    k, "different", changed,
                    {n: v for n, v in zip(names, ra) if n in changed},
                    {n: v for n, v in zip(names, rb) if n in changed},
                )
            )
