"""Bisection logic, tested against in-memory fixtures with no database.

Everything here plants a difference and asserts the walker finds *exactly* it.
Two failure modes are worth more than the rest combined:

- reporting a match when the tables differ (a skipped key range), and
- reporting a difference when they do not (a bucket-boundary off-by-one).

Both come from `bucket_bounds` disagreeing with the SQL bucket expression, so
that inverse is property-tested directly rather than only through end-to-end
behaviour.
"""

from __future__ import annotations

import random

import pytest

from fakes import DictTable, FakeDialect, SyntheticTable, row_hash
from parity.engine import bucket_bounds, diff
from parity.types import Column, LogicalType

COLS = [
    Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
    Column("status", LogicalType.STRING, "varchar"),
]


def run(table_a, table_b, **kwargs):
    """Diff two fixture tables and hand back the result plus both sides."""
    a = FakeDialect(table_a, side="A")
    b = FakeDialect(table_b, side="B")
    result = diff(a, b, "a.t", "b.t", "id", **kwargs)
    return result, a, b


def kinds(result) -> list[tuple[int, str]]:
    return sorted((d.key, d.kind) for d in result.diffs)


# ---------------------------------------------------------------------------
# 1. The bucket inverse. Get this wrong and the walker skips rows silently.
# ---------------------------------------------------------------------------


def sql_bucket(key: int, lo: int, hi: int, n: int) -> int:
    """The SQL expression from `Dialect.segment_checksums`, in Python."""
    return ((key - lo) * n) // (hi - lo)


@pytest.mark.parametrize("seed", range(40))
def test_bucket_bounds_inverts_the_sql_expression_exactly(seed: int):
    rng = random.Random(seed)
    lo = rng.randint(-5_000, 5_000)
    hi = lo + rng.randint(2, 3_000)
    n = min(rng.choice([2, 3, 7, 32, 64, 100, 999]), hi - lo)

    covered: set[int] = set()
    for i in range(n):
        b_lo, b_hi = bucket_bounds(i, lo, hi, n)
        assert lo <= b_lo <= b_hi <= hi, f"bucket {i} escaped [{lo},{hi})"
        for key in range(b_lo, b_hi):
            assert sql_bucket(key, lo, hi, n) == i, (
                f"key {key} is in Python bucket {i} but SQL bucket "
                f"{sql_bucket(key, lo, hi, n)} (lo={lo} hi={hi} n={n})"
            )
        assert not (covered & set(range(b_lo, b_hi))), f"bucket {i} overlaps"
        covered |= set(range(b_lo, b_hi))

    # The union of the buckets must be the whole range - a single uncovered key
    # is a row the tool would never look at while still reporting "identical".
    assert covered == set(range(lo, hi)), (
        f"buckets do not tile [{lo},{hi}) with n={n}: "
        f"{sorted(set(range(lo, hi)) - covered)[:10]} uncovered"
    )


def test_bucket_bounds_handles_a_range_smaller_than_the_factor():
    # n is clamped to the span by the engine, but the maths must hold anyway.
    for i in range(3):
        lo_i, hi_i = bucket_bounds(i, 10, 13, 3)
        assert (lo_i, hi_i) == (10 + i, 11 + i)


# ---------------------------------------------------------------------------
# 2. Identical tables: no differences, and crucially no rows downloaded.
# ---------------------------------------------------------------------------


def test_identical_million_row_tables_download_zero_rows():
    result, a, b = run(SyntheticTable(1_000_000), SyntheticTable(1_000_000))

    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0, (
        "a clean match must not move a single row across the network"
    )
    assert a.rows_served == 0 and b.rows_served == 0
    # 2 key_stats + 2 checksums. Nothing else is justified.
    assert result.stats.queries == 4
    assert a.queries == 2 and b.queries == 2
    assert result.stats.rows_compared_a == 1_000_000


def test_identical_tables_are_not_reported_as_truncated():
    result, _, _ = run(SyntheticTable(5_000), SyntheticTable(5_000))
    assert result.identical and not result.truncated


# ---------------------------------------------------------------------------
# 3. Planted differences: found exactly, classified correctly.
# ---------------------------------------------------------------------------


def test_single_changed_row_in_a_million_is_found():
    key = 734_129
    # Change `amount` only: keep the generated `status` so exactly one column
    # moves and the report can be checked column by column.
    changed = {key: ("999.99", SyntheticTable(1)._generated(key)[1])}
    result, a, b = run(SyntheticTable(1_000_000), SyntheticTable(1_000_000, changed=changed))

    assert kinds(result) == [(key, "different")]
    d = result.diffs[0]
    assert d.columns == ["amount"], "only the column that moved should be named"
    assert d.values_a == {"amount": "129.29"}
    assert d.values_b == {"amount": "999.99"}
    # It must have narrowed in, not dragged the table over.
    assert result.stats.rows_downloaded < 20_000
    assert result.stats.rows_downloaded > 0


def test_a_row_missing_from_b_is_only_in_a():
    result, _, _ = run(SyntheticTable(50_000), SyntheticTable(50_000, deleted=[31_337]))
    assert kinds(result) == [(31_337, "only_in_a")]
    assert result.diffs[0].values_a and not result.diffs[0].values_b


def test_a_row_missing_from_a_is_only_in_b():
    result, _, _ = run(SyntheticTable(50_000, deleted=[7]), SyntheticTable(50_000))
    assert kinds(result) == [(7, "only_in_b")]
    assert result.diffs[0].values_b and not result.diffs[0].values_a


def test_all_four_difference_kinds_at_once():
    a_table = SyntheticTable(
        200_000,
        changed={13: ("1.00", "")},  # empty string ...
        deleted=[47],  # ... row missing from A
        extra={999_999_999: ("1.00", "extra")},  # ... row only on A
    )
    b_table = SyntheticTable(
        200_000,
        changed={13: ("1.00", "\\N"), 120_455: ("0.01", "status-2")},  # ... NULL, and a changed value
    )
    result, _, _ = run(a_table, b_table)

    assert kinds(result) == [
        (13, "different"),
        (47, "only_in_b"),
        (120_455, "different"),
        (999_999_999, "only_in_a"),
    ]
    # The NULL-versus-empty-string trap must name the column it hit.
    null_trap = next(d for d in result.diffs if d.key == 13)
    assert null_trap.columns == ["status"]
    assert null_trap.values_a == {"status": ""}
    assert null_trap.values_b == {"status": "\\N"}


def test_a_row_differing_in_several_columns_names_all_of_them():
    a_table = DictTable(COLS, {1: ("1.00", "paid"), 2: ("2.00", "open")})
    b_table = DictTable(COLS, {1: ("1.00", "paid"), 2: ("9.99", "void")})
    result, _, _ = run(a_table, b_table)

    assert kinds(result) == [(2, "different")]
    assert result.diffs[0].columns == ["amount", "status"]


def test_differences_are_returned_in_key_order():
    a_table = SyntheticTable(10_000, deleted=[9_000, 5, 4_321])
    result, _, _ = run(a_table, SyntheticTable(10_000))
    assert [d.key for d in result.diffs] == sorted(d.key for d in result.diffs)
    assert [d.key for d in result.diffs] == [5, 4_321, 9_000]


def test_no_false_positives_on_a_dense_run_of_differences():
    """Every row in a contiguous block differs; none outside it may be flagged."""
    changed = {k: ("0.00", "wrong") for k in range(5_000, 5_100)}
    result, _, _ = run(SyntheticTable(100_000), SyntheticTable(100_000, changed=changed))

    assert len(result.diffs) == 100
    assert {d.key for d in result.diffs} == set(range(5_000, 5_100))
    assert all(d.kind == "different" for d in result.diffs)


# ---------------------------------------------------------------------------
# 4. Cost: the whole point of the tool.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [10_000, 100_000, 1_000_000, 10_000_000])
def test_query_count_grows_logarithmically_not_linearly(n: int):
    """One changed row, tables from 10k to 10M. Queries must barely move."""
    result, _, _ = run(SyntheticTable(n), SyntheticTable(n, changed={n // 3: ("0.00", "x")}))
    assert len(result.diffs) == 1
    assert result.stats.queries <= 24, (
        f"{n:,} rows took {result.stats.queries} queries; a logarithmic walk "
        f"should need roughly 2 per level"
    )


def test_rows_downloaded_stays_a_tiny_fraction_of_the_table():
    n = 1_000_000
    result, _, _ = run(SyntheticTable(n), SyntheticTable(n, changed={500_001: ("0.00", "x")}))
    fraction = result.stats.rows_downloaded / n
    assert fraction < 0.01, f"downloaded {fraction:.2%} of the table"


def test_engine_query_count_matches_what_the_sides_actually_served():
    result, a, b = run(SyntheticTable(20_000), SyntheticTable(20_000, deleted=[1_234]))
    assert result.stats.queries == a.queries + b.queries


def test_a_sparse_key_space_costs_round_trips_not_scans():
    """One row at key 1e9 widens the range 5000x. The walk must still finish."""
    a_table = SyntheticTable(200_000, extra={999_999_999: ("1.00", "extra")})
    result, a, b = run(a_table, SyntheticTable(200_000))

    assert kinds(result) == [(999_999_999, "only_in_a")]
    assert result.stats.queries < 40
    assert result.stats.rows_downloaded < 20_000


def test_bisection_factor_changes_the_shape_of_the_walk():
    n = 500_000
    changed = {n // 2: ("0.00", "x")}
    wide, _, _ = run(SyntheticTable(n), SyntheticTable(n, changed=changed), bisection_factor=256)
    narrow, _, _ = run(SyntheticTable(n), SyntheticTable(n, changed=changed), bisection_factor=2)

    assert len(wide.diffs) == len(narrow.diffs) == 1
    # A wider fan-out means fewer levels, so fewer round trips.
    assert wide.stats.queries < narrow.stats.queries


# ---------------------------------------------------------------------------
# 5. Empty and degenerate inputs.
# ---------------------------------------------------------------------------


def test_two_empty_tables_match():
    result, a, b = run(DictTable(COLS, {}), DictTable(COLS, {}))
    assert result.identical
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 2  # key_stats only; nothing to bisect


def test_an_empty_side_reports_every_row_as_only_in_the_other():
    result, _, _ = run(DictTable(COLS, {1: ("1.00", "a"), 2: ("2.00", "b")}), DictTable(COLS, {}))
    assert kinds(result) == [(1, "only_in_a"), (2, "only_in_a")]


def test_a_single_row_table():
    result, _, _ = run(DictTable(COLS, {5: ("1.00", "a")}), DictTable(COLS, {5: ("1.00", "b")}))
    assert kinds(result) == [(5, "different")]
    assert result.diffs[0].columns == ["status"]


def test_negative_and_zero_keys_are_handled():
    a_table = DictTable(COLS, {-100: ("1.00", "a"), 0: ("2.00", "b"), 100: ("3.00", "c")})
    b_table = DictTable(COLS, {-100: ("1.00", "a"), 0: ("2.00", "CHANGED"), 100: ("3.00", "c")})
    result, _, _ = run(a_table, b_table)
    assert kinds(result) == [(0, "different")]


def test_an_empty_bucket_on_one_side_only_is_not_treated_as_a_match():
    """A gap in one table's key space must not be mistaken for agreement.

    Both sides return no group for an absent bucket, so absent-vs-absent has to
    match while absent-vs-present must not - otherwise the walker either
    recurses forever or skips the rows entirely.
    """
    a_table = DictTable(COLS, {1: ("1.00", "a"), 500: ("2.00", "b"), 1000: ("3.00", "c")})
    b_table = DictTable(COLS, {1: ("1.00", "a"), 1000: ("3.00", "c")})
    result, _, _ = run(a_table, b_table, bisection_factor=4)
    assert kinds(result) == [(500, "only_in_a")]


# ---------------------------------------------------------------------------
# 6. Guard rails - the tool must refuse rather than answer wrongly.
# ---------------------------------------------------------------------------


def test_duplicate_keys_are_refused():
    class Dupes(DictTable):
        def key_stats(self):
            from parity.types import KeyStats

            return KeyStats(1, 3, 4, 3)  # 4 rows, 3 distinct keys

    a = FakeDialect(Dupes(COLS, {1: ("1", "a"), 2: ("2", "b"), 3: ("3", "c")}), side="A")
    b = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="B")
    with pytest.raises(ValueError) as exc:
        diff(a, b, "a.t", "b.t", "id")
    assert "not unique" in str(exc.value) and "side A" in str(exc.value)


def test_a_non_integer_key_is_refused_by_name_and_type():
    a = FakeDialect(
        DictTable(COLS, {1: ("1", "a")}),
        side="A",
        key_type_override=Column("id", LogicalType.STRING, "uuid"),
    )
    b = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="B")
    with pytest.raises(ValueError) as exc:
        diff(a, b, "a.t", "b.t", "id")
    msg = str(exc.value)
    assert "side A" in msg and "uuid" in msg and "integer" in msg


def test_a_missing_key_column_lists_what_is_there():
    a = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="A")
    b = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="B")
    with pytest.raises(ValueError) as exc:
        diff(a, b, "a.t", "b.t", "order_id")
    assert "order_id" in str(exc.value) and "amount" in str(exc.value)


def test_mismatched_float_scales_are_refused_before_any_query():
    a = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="A", float_scale=6)
    b = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="B", float_scale=2)
    with pytest.raises(ValueError) as exc:
        diff(a, b, "a.t", "b.t", "id")
    assert "float_scale differs" in str(exc.value)
    assert a.queries == 0 and b.queries == 0, "it queried before checking"


@pytest.mark.parametrize("factor", [-1, 0, 1])
def test_a_useless_bisection_factor_is_refused(factor: int):
    a = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="A")
    b = FakeDialect(DictTable(COLS, {1: ("1", "a")}), side="B")
    with pytest.raises(ValueError, match="bisection_factor"):
        diff(a, b, "a.t", "b.t", "id", bisection_factor=factor)


# ---------------------------------------------------------------------------
# 7. Truncation must never look like a clean result.
# ---------------------------------------------------------------------------


def test_max_diffs_marks_the_result_truncated():
    changed = {k: ("0.00", "wrong") for k in range(1, 5_000, 7)}
    result, _, _ = run(
        SyntheticTable(100_000), SyntheticTable(100_000, changed=changed), max_diffs=10
    )
    assert result.truncated
    assert len(result.diffs) >= 10
    assert not result.identical, (
        "a partial answer with differences must never read as identical"
    )
    assert any("max-diffs" in w for w in result.warnings)


def test_a_complete_walk_is_not_marked_truncated_even_at_the_limit():
    a_table = SyntheticTable(10_000, deleted=[100, 200])
    result, _, _ = run(a_table, SyntheticTable(10_000), max_diffs=100)
    assert len(result.diffs) == 2
    assert not result.truncated


# ---------------------------------------------------------------------------
# 8. Column selection and its warnings.
# ---------------------------------------------------------------------------


def _sided_tables():
    a_cols = [*COLS, Column("only_a", LogicalType.STRING, "varchar")]
    b_cols = [*COLS, Column("only_b", LogicalType.STRING, "varchar")]
    a_table = DictTable(a_cols, {1: ("1.00", "paid", "x"), 2: ("2.00", "open", "y")})
    b_table = DictTable(b_cols, {1: ("1.00", "paid", "p"), 2: ("2.00", "open", "q")})
    return a_table, b_table


def test_columns_present_on_one_side_only_are_skipped_with_a_warning():
    result, _, _ = run(*_sided_tables())
    assert result.identical, "one-sided columns must not create differences"
    assert any("only_a" in w and "side A" in w for w in result.warnings)
    assert any("only_b" in w and "side B" in w for w in result.warnings)
    assert [c.name for c in result.columns] == ["amount", "status"]


def test_exclude_drops_a_column_from_the_comparison():
    a_table = DictTable(COLS, {1: ("1.00", "paid")})
    b_table = DictTable(COLS, {1: ("1.00", "CHANGED")})

    assert kinds(run(a_table, b_table)[0]) == [(1, "different")]
    assert run(a_table, b_table, exclude=["status"])[0].identical


def test_columns_restricts_the_comparison():
    a_table = DictTable(COLS, {1: ("1.00", "paid")})
    b_table = DictTable(COLS, {1: ("9.99", "paid")})

    assert kinds(run(a_table, b_table)[0]) == [(1, "different")]
    assert run(a_table, b_table, columns=["status"])[0].identical


def test_columns_naming_the_key_is_refused():
    a_table, b_table = _sided_tables()
    with pytest.raises(ValueError, match="key column"):
        run(a_table, b_table, columns=["id"])


def test_columns_naming_a_one_sided_column_is_refused():
    a_table, b_table = _sided_tables()
    with pytest.raises(ValueError, match="only one side"):
        run(a_table, b_table, columns=["only_a"])


def test_columns_naming_an_unknown_column_is_refused():
    a_table, b_table = _sided_tables()
    with pytest.raises(ValueError, match="unknown columns"):
        run(a_table, b_table, columns=["nope"])


def test_columns_and_exclude_contradicting_each_other_is_refused():
    a_table, b_table = _sided_tables()
    with pytest.raises(ValueError, match="both name"):
        run(a_table, b_table, columns=["amount"], exclude=["amount"])


def test_no_shared_columns_still_checks_key_presence():
    """Schemas that share only the key: contents cannot differ, presence can."""
    a_cols = [Column("only_a", LogicalType.STRING, "varchar")]
    b_cols = [Column("only_b", LogicalType.STRING, "varchar")]
    a_table = DictTable(a_cols, {1: ("x",), 2: ("y",), 3: ("z",)})
    b_table = DictTable(b_cols, {1: ("p",), 3: ("r",)})

    result, _, _ = run(a_table, b_table)
    assert kinds(result) == [(2, "only_in_a")]
    assert any("no comparable columns" in w for w in result.warnings)


def test_a_type_mismatch_between_sides_is_called_out():
    a_cols = [Column("amount", LogicalType.DECIMAL, "decimal(12,2)")]
    b_cols = [Column("amount", LogicalType.STRING, "varchar")]
    result, _, _ = run(
        DictTable(a_cols, {1: ("1.00",)}), DictTable(b_cols, {1: ("1.00",)})
    )
    assert any(
        "amount" in w and "side A" in w and "side B" in w for w in result.warnings
    ), result.warnings


def test_decimal_versus_double_is_not_warned_about():
    """The common migration case: both render through the same rounded text."""
    a_cols = [Column("amount", LogicalType.DECIMAL, "decimal(12,2)")]
    b_cols = [Column("amount", LogicalType.FLOAT, "double")]
    result, _, _ = run(
        DictTable(a_cols, {1: ("1.00",)}), DictTable(b_cols, {1: ("1.00",)})
    )
    assert not [w for w in result.warnings if "amount" in w], result.warnings


def test_unmapped_types_are_flagged():
    a_cols = [Column("payload", LogicalType.UNKNOWN, "json")]
    b_cols = [Column("payload", LogicalType.UNKNOWN, "json")]
    result, _, _ = run(
        DictTable(a_cols, {1: ("{}",)}), DictTable(b_cols, {1: ("{}",)})
    )
    assert any("unmapped types" in w and "payload" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 9. Architecture: the engine must stay engine-agnostic.
# ---------------------------------------------------------------------------


def test_engine_imports_no_concrete_dialect():
    """Adding Snowflake must mean one new file and no edit here."""
    import inspect

    import parity.engine

    source = inspect.getsource(parity.engine)
    for forbidden in ("duckdb", "postgres", "psycopg"):
        assert forbidden not in source.lower(), (
            f"engine.py mentions {forbidden!r}; the bisection algorithm must "
            f"not know which engines exist"
        )


def test_the_engine_never_builds_sql_itself():
    """FakeDialect.query raises, so a clean run proves the contract held."""
    result, _, _ = run(SyntheticTable(20_000), SyntheticTable(20_000, deleted=[5_000]))
    assert kinds(result) == [(5_000, "only_in_a")]


def test_row_hash_helper_matches_the_documented_constant():
    assert row_hash("abc") == 648541476951500027


# ---------------------------------------------------------------------------
# 10. Interruption: a long diff must be abortable
# ---------------------------------------------------------------------------


def test_an_interrupt_cancels_both_sides_and_propagates():
    """Ctrl-C during a walk must reach the databases, not just the process.

    Both sides are queried on worker threads, so the interrupt lands on the
    main thread while the workers sit blocked in the driver. Without an
    explicit cancel the pool's shutdown then waits for those queries - which on
    the long diff someone actually wants to abort is the entire problem.
    """
    class Interrupting(FakeDialect):
        def __init__(self, *args, raise_on_checksums=False, **kwargs):
            super().__init__(*args, **kwargs)
            self.raise_on_checksums = raise_on_checksums
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def segment_checksums(self, *args, **kwargs):
            if self.raise_on_checksums:
                raise KeyboardInterrupt
            return super().segment_checksums(*args, **kwargs)

    a = Interrupting(SyntheticTable(10_000), side="A", raise_on_checksums=True)
    b = Interrupting(SyntheticTable(10_000), side="B")

    with pytest.raises(KeyboardInterrupt):
        diff(a, b, "a.t", "b.t", "id")

    assert a.cancelled and b.cancelled, (
        "both sides must be cancelled, not only the one that raised"
    )


def test_a_failing_cancel_does_not_replace_the_real_error():
    """Diagnosing an interrupt must never lose the interrupt."""
    class Broken(FakeDialect):
        def cancel(self):
            raise RuntimeError("cancel itself is broken")

        def key_stats(self, table, key):
            raise KeyboardInterrupt

    a = Broken(SyntheticTable(10), side="A")
    b = Broken(SyntheticTable(10), side="B")
    with pytest.raises(KeyboardInterrupt):
        diff(a, b, "a.t", "b.t", "id")


def test_gather_returns_both_results_in_order():
    """The polling wait must behave exactly like waiting on both futures."""
    from concurrent.futures import ThreadPoolExecutor

    from parity.engine import _gather

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(lambda: "a")
        fb = pool.submit(lambda: "b")
        assert _gather(fa, fb) == ("a", "b")

        # An exception on either side still propagates rather than looping.
        def boom():
            raise ValueError("side failed")

        fa = pool.submit(boom)
        fb = pool.submit(lambda: "b")
        with pytest.raises(ValueError, match="side failed"):
            _gather(fa, fb)
