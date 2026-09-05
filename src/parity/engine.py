"""Engine-agnostic segmented diff.

This module must not import a concrete dialect. It talks to two ``Dialect``
objects through their contract and knows nothing about SQL - that separation is
what makes adding Snowflake or BigQuery a single new file.

The strategy: split the key range into buckets, ask each side for one checksum
per bucket (one query per side per level), and recurse only into buckets whose
checksums disagree. Rows are downloaded solely from ranges already proven to
differ, and only once those ranges are small.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, TypeVar

from parity.dialects.base import Dialect, require_matching_scales
from parity.types import Column, DiffResult, DiffStats, LogicalType, RowDiff

#: What a bucket looks like when a side returned no group for it at all.
#: `group by` only emits non-empty groups, so an absent bucket genuinely holds
#: zero rows. Two absent buckets match; absent on one side only does not.
EMPTY = (0, 0)

#: DECIMAL and FLOAT render through the same rounded-text encoding, so a column
#: that is decimal on one side and double on the other still compares correctly.
#: This is the common migration case and must not raise a warning.
_NUMERIC_EQUIVALENT = frozenset({LogicalType.DECIMAL, LogicalType.FLOAT})

_A = TypeVar("_A")
_B = TypeVar("_B")


def bucket_bounds(i: int, lo: int, hi: int, n: int) -> tuple[int, int]:
    """Key range of bucket ``i``, inverting the SQL bucket expression.

    SQL computes ``bucket = (key - lo) * n / (hi - lo)`` with *truncating*
    integer division. The inverse of that is ceiling division. Getting this
    wrong makes the walker skip key ranges while still reporting a clean
    match - the worst failure this tool can have - so it is isolated here and
    property-tested against the SQL formula in ``tests/test_engine.py``.
    """
    span = hi - lo
    b_lo = lo + -(-(i * span) // n)
    b_hi = lo + -(-((i + 1) * span) // n)
    return b_lo, b_hi


#: How often the main thread wakes while waiting on the two sides.
_POLL_SECONDS = 0.2


def _gather(fa: Future[_A], fb: Future[_B]) -> tuple[_A, _B]:
    """Wait for both sides, staying interruptible while doing so.

    ``Future.result()`` with no timeout blocks in a lock acquire that Windows
    will not deliver a KeyboardInterrupt through, so Ctrl-C is not noticed
    until the query returns on its own - measured at 17 seconds into a 40
    second diff. Passing a timeout makes the wait a series of short sleeps the
    interrupt can land between, at a cost of one cheap wakeup every fifth of a
    second against queries that run for tens of seconds.
    """
    while True:
        try:
            return fa.result(timeout=_POLL_SECONDS), fb.result(timeout=_POLL_SECONDS)
        except FuturesTimeout:
            continue


@contextmanager
def _cancel_on_interrupt(a: Dialect, b: Dialect) -> Iterator[None]:
    """Turn a Ctrl-C into an actual abort of both in-flight queries.

    Both sides are queried on worker threads, so the interrupt lands on the
    main thread while the workers sit blocked in the database driver. Exiting
    the `ThreadPoolExecutor` context then *waits* for those queries to finish -
    so without this, Ctrl-C on the ten-minute diff someone actually wants to
    abort does nothing for ten minutes.

    Cancelling makes the workers' queries raise, the threads end, and the pool
    shuts down promptly. The original KeyboardInterrupt is re-raised either way.
    """
    try:
        yield
    except BaseException:
        for side in (a, b):
            try:
                side.cancel()
            except Exception:  # noqa: BLE001, S110
                # A failed cancel must never replace the real exception -
                # the KeyboardInterrupt below is what the caller needs.
                pass
        raise


def _is_tz_aware(column: Column) -> bool:
    """Whether a column carries a timezone, judged from the engine's own name.

    Both engines report `timestamp with time zone` (DuckDB in upper case), and
    `map_type` folds it onto TIMESTAMP by prefix - so the logical type cannot
    tell these apart and the raw name is the only signal there is.
    """
    return "with time zone" in column.raw_type.lower()


def _select_columns(
    cols_a: dict[str, Column],
    cols_b: dict[str, Column],
    key: str,
    columns: Sequence[str] | None,
    exclude: Sequence[str],
    warnings: list[str],
) -> list[str]:
    """Decide which columns to compare, explaining anything dropped."""
    both = (set(cols_a) & set(cols_b)) - {key}
    excluded = set(exclude)

    unknown_exclude = excluded - set(cols_a) - set(cols_b)
    if unknown_exclude:
        warnings.append(
            f"--exclude named columns that exist on neither side: "
            f"{sorted(unknown_exclude)}"
        )

    shared = sorted(both - excluded)
    if columns:
        requested = list(dict.fromkeys(columns))  # de-duplicate, keep order
        nowhere = [c for c in requested if c not in cols_a and c not in cols_b]
        one_side = [c for c in requested if c not in nowhere and c not in both]
        dropped = [c for c in requested if c in excluded]
        # Order matters: the key is present on both sides but excluded from
        # `both`, so it would otherwise be misreported as one-sided.
        if key in requested:
            raise ValueError(
                f"--columns named the key column {key!r}; the key is how rows "
                f"are matched up, not something compared between them"
            )
        if nowhere:
            raise ValueError(f"--columns named unknown columns: {nowhere}")
        if one_side:
            raise ValueError(
                f"--columns named columns present on only one side: {one_side}"
            )
        if dropped:
            raise ValueError(
                f"--columns and --exclude both name: {dropped}"
            )
        shared = [c for c in shared if c in set(requested)]

    for side, only in (
        ("A", sorted(set(cols_a) - set(cols_b) - {key})),
        ("B", sorted(set(cols_b) - set(cols_a) - {key})),
    ):
        if only:
            warnings.append(f"not compared, present only on side {side}: {only}")

    # A column whose logical type differs between sides renders through a
    # different canonical encoding, so every row would report as changed. That
    # looks like a catastrophic data difference but is really a schema
    # difference, so name it explicitly.
    for name in shared:
        ta, tb = cols_a[name].logical_type, cols_b[name].logical_type
        if ta is tb or {ta, tb} <= _NUMERIC_EQUIVALENT:
            continue
        warnings.append(
            f"column {name!r} is {cols_a[name].raw_type or ta.value} on side A "
            f"but {cols_b[name].raw_type or tb.value} on side B; values are "
            f"compared as text and will very likely all differ"
        )

    # Timezone-awareness is invisible to the logical type - both sides map to
    # TIMESTAMP - but it is a real semantic difference and `timestamptz` to
    # `timestamp` is one of the commonest migration changes there is. Sessions
    # are pinned to UTC, so a migration that stored UTC compares clean. One
    # that stored local wall-clock reports *every* row as different, and
    # without this line there is nothing pointing at which axis to look along.
    for name in shared:
        aware_a = _is_tz_aware(cols_a[name])
        if aware_a is _is_tz_aware(cols_b[name]):
            continue
        aware, naive = ("A", "B") if aware_a else ("B", "A")
        warnings.append(
            f"column {name!r} is timezone-aware on side {aware} but not on "
            f"side {naive}; both are read in UTC, so a migration that stored "
            f"local wall-clock time rather than UTC will show every row as "
            f"different"
        )

    unknown = sorted(
        {n for n in shared if cols_a[n].logical_type is LogicalType.UNKNOWN}
        | {n for n in shared if cols_b[n].logical_type is LogicalType.UNKNOWN}
    )
    if unknown:
        warnings.append(
            f"unmapped types, compared as raw text (may differ across engines "
            f"for reasons other than the data): {unknown}"
        )

    if not shared:
        warnings.append(
            "no comparable columns: only the presence of each key is checked, "
            "not row contents"
        )
    return shared


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
    """Compare ``a_table`` on side ``a`` with ``b_table`` on side ``b``."""
    started = time.perf_counter()
    stats = DiffStats()
    warnings: list[str] = []

    if bisection_factor < 2:
        raise ValueError(f"bisection_factor must be >= 2, got {bisection_factor}")
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    # Checked before any query: two sides rounding floats differently would
    # report every float row as changed.
    require_matching_scales(a, b)

    # Both sides in parallel from here on. One pool for the whole walk - the
    # comparison is almost entirely IO-wait on two independent engines.
    # Order matters: context managers exit in reverse, so `_cancel_on_interrupt`
    # must be the *inner* one. The pool's own exit blocks waiting for its
    # threads, so cancelling has to happen before that, not after.
    with ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="parity"
    ) as pool, _cancel_on_interrupt(a, b):

        def both(
            fn_name: str,
            *args_a: Any,
            _args_b: tuple[Any, ...] | None = None,
        ) -> tuple[Any, Any]:
            fa = pool.submit(getattr(a, fn_name), a_table, *args_a)
            fb = pool.submit(getattr(b, fn_name), b_table, *(_args_b or args_a))
            stats.queries += 2
            return _gather(fa, fb)

        cols_a_list, cols_b_list = both("columns")
        cols_a = {c.name: c for c in cols_a_list}
        cols_b = {c.name: c for c in cols_b_list}
        # Introspection is metadata, not a scan; do not inflate the query count
        # users read as "how much work did this cost".
        stats.queries -= 2

        for side, table, cols in (("A", a_table, cols_a), ("B", b_table, cols_b)):
            if key not in cols:
                raise ValueError(
                    f"[side {side}] key column {key!r} is not in {table}. "
                    f"Columns are: {sorted(cols)}"
                )
            col = cols[key]
            if col.logical_type is not LogicalType.INTEGER:
                # The bisection arithmetic divides the key range. A varchar or
                # uuid key would otherwise fail as a cast error mid-walk.
                raise ValueError(
                    f"[side {side}] key column {key!r} in {table} is "
                    f"{col.raw_type or col.logical_type.value}, not an integer. "
                    f"Only integer keys are supported."
                )

        shared = _select_columns(cols_a, cols_b, key, columns, exclude, warnings)
        a_cols = [cols_a[c] for c in shared]
        b_cols = [cols_b[c] for c in shared]

        ks_a, ks_b = both("key_stats", key)

        # A non-unique key is fatal, not a warning: `fetch_range` maps key to
        # row, so duplicates collapse and their differences disappear.
        # Reporting "identical" for a table we could not actually compare is
        # the one outcome this tool must never produce.
        for side, table, ks in (("A", a_table, ks_a), ("B", b_table, ks_b)):
            # NULL keys first: `count(distinct)` ignores NULLs, so checking
            # uniqueness alone would report a NULL key as a duplicate and send
            # the reader hunting for duplicates that do not exist.
            if ks.has_null_keys:
                raise ValueError(
                    f"[side {side}] key column {key!r} in {table} contains "
                    f"{ks.null_keys:,} NULL value(s). A row with no key cannot "
                    f"be matched to anything on the other side."
                )
            if ks.has_duplicate_keys:
                raise ValueError(
                    f"[side {side}] key column {key!r} in {table} is not "
                    f"unique: {ks.rows:,} rows but only {ks.distinct:,} "
                    f"distinct keys. Rows cannot be compared one-to-one."
                )
        stats.rows_compared_a, stats.rows_compared_b = ks_a.rows, ks_b.rows

        diffs: list[RowDiff] = []
        truncated = False
        #: Differences or key ranges the walk knowingly did not look at. Any
        #: non-zero value means the answer is partial.
        unchecked = 0
        bounds = [v for v in (ks_a.lo, ks_a.hi, ks_b.lo, ks_b.hi) if v is not None]

        if bounds:
            # Half-open [lo, hi): +1 so the largest key is inside the range.
            lo, hi = min(bounds), max(bounds) + 1
            queue: list[tuple[int, int]] = [(lo, hi)]

            def limit_reached() -> bool:
                return max_diffs is not None and len(diffs) >= max_diffs

            while queue:
                if limit_reached():
                    unchecked += len(queue)
                    break

                s_lo, s_hi = queue.pop()
                span = s_hi - s_lo
                if span <= 0:
                    continue
                stats.segments_checked += 1

                if span <= 1:
                    _compare_rows(
                        pool, a, b, a_table, b_table, key,
                        a_cols, b_cols, s_lo, s_hi, diffs, stats,
                    )
                    continue

                n = min(bisection_factor, span)
                fa = pool.submit(
                    a.segment_checksums, a_table, key, a_cols, s_lo, s_hi, n
                )
                fb = pool.submit(
                    b.segment_checksums, b_table, key, b_cols, s_lo, s_hi, n
                )
                cs_a, cs_b = _gather(fa, fb)
                stats.queries += 2

                differing = [
                    i for i in range(n)
                    if cs_a.get(i, EMPTY) != cs_b.get(i, EMPTY)
                ]
                for position, i in enumerate(differing):
                    # The limit has to be honoured inside the level too. A
                    # single bucket can yield thousands of differences, so
                    # checking only between queue pops would blow past
                    # max_diffs and still call the walk complete.
                    if limit_reached():
                        unchecked += len(differing) - position + len(queue)
                        break
                    va, vb = cs_a.get(i, EMPTY), cs_b.get(i, EMPTY)
                    b_lo_i, b_hi_i = bucket_bounds(i, s_lo, s_hi, n)
                    if max(va[0], vb[0]) <= threshold or b_hi_i - b_lo_i <= 1:
                        _compare_rows(
                            pool, a, b, a_table, b_table, key,
                            a_cols, b_cols, b_lo_i, b_hi_i, diffs, stats,
                        )
                    else:
                        queue.append((b_lo_i, b_hi_i))

    diffs.sort(key=lambda d: d.key)
    if max_diffs is not None and len(diffs) > max_diffs:
        # A single bucket download can overshoot the limit by a lot. Report the
        # first `max_diffs` in key order and say the rest were not listed.
        unchecked += len(diffs) - max_diffs
        del diffs[max_diffs:]
    if unchecked:
        # Partial answers get a flag on the result, not merely a warning
        # string, so no caller can mistake one for a clean comparison.
        truncated = True
        warnings.append(
            f"stopped at the --max-diffs limit of {max_diffs}; "
            f"{unchecked} further difference(s) or key range(s) were not "
            f"reported, so this is a partial answer"
        )

    stats.seconds = time.perf_counter() - started
    return DiffResult(
        diffs, stats, a_cols, warnings,
        truncated=truncated, float_scale=a.float_scale,
    )


def _compare_rows(
    pool: ThreadPoolExecutor,
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
    fa = pool.submit(a.fetch_range, a_table, key, a_cols, lo, hi)
    fb = pool.submit(b.fetch_range, b_table, key, b_cols, lo, hi)
    rows_a, rows_b = _gather(fa, fb)
    stats.queries += 2
    stats.rows_downloaded += len(rows_a) + len(rows_b)

    names = [c.name for c in a_cols]
    # `strict=True` on every zip below is load-bearing, not tidiness. Both sides
    # render the same column list in the same order, so the tuples must be the
    # same length as `names`. If that invariant ever broke, a plain zip would
    # silently truncate and simply not report the trailing columns - a
    # difference the tool found and then dropped, which is the one outcome it
    # must never produce. Better to raise.
    for k in sorted(set(rows_a) | set(rows_b)):
        ra, rb = rows_a.get(k), rows_b.get(k)
        if ra is None:
            diffs.append(
                RowDiff(k, "only_in_b", names, {}, dict(zip(names, rb or (), strict=True)))
            )
        elif rb is None:
            diffs.append(
                RowDiff(k, "only_in_a", names, dict(zip(names, ra, strict=True)), {})
            )
        elif ra != rb:
            changed = [n for n, x, y in zip(names, ra, rb, strict=True) if x != y]
            diffs.append(
                RowDiff(
                    k, "different", changed,
                    {n: v for n, v in zip(names, ra, strict=True) if n in changed},
                    {n: v for n, v in zip(names, rb, strict=True) if n in changed},
                )
            )
