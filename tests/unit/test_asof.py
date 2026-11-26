# tests/unit/test_asof.py
"""Exhaustive tests for the PT-015 as-of module.

Pure-module coverage doctrine: 100% target; every loud-refusal row of
the design validation table has a test asserting the ValueError and
its message naming the offender. Born pyright-strict.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dpl.lib.asof import (
    AS_OF_LAGS,
    COVERAGE_SNAPSHOT,
    COVERAGE_WINDOW,
    anchor,
    coverage_statement,
    validate_as_of,
    validate_table_type,
    validate_window_days,
)

CTX = "pipe 'p' connection 'c'"


# ---------------------------------------------------------------- as_of ---
class TestAsOfValidation:
    def test_all_three_lags_accepted(self) -> None:
        assert validate_as_of("T-0", CTX) == 0
        assert validate_as_of("T-1", CTX) == 1
        assert validate_as_of("T-2", CTX) == 2

    def test_t3_refused(self) -> None:
        with pytest.raises(ValueError, match="must be one of"):
            validate_as_of("T-3", CTX)

    def test_case_variant_refused(self) -> None:
        for bad in ("t-1", "T1", "T-02", " T-1"):
            with pytest.raises(ValueError, match="must be one of"):
                validate_as_of(bad, CTX)

    def test_non_string_refused(self) -> None:
        for bad in (0, 1, None, ["T-1"]):
            with pytest.raises(ValueError, match="must be one of"):
                validate_as_of(bad, CTX)

    def test_error_names_ctx(self) -> None:
        with pytest.raises(ValueError, match=CTX):
            validate_as_of("T-9", CTX)


# --------------------------------------------------------------- anchor ---
class TestAnchorArithmetic:
    def test_t0_identity(self) -> None:
        d = dt.date(2026, 11, 23)
        assert anchor(d, validate_as_of("T-0", CTX)) == d

    def test_mid_month(self) -> None:
        assert anchor(dt.date(2026, 11, 23), 2) == dt.date(2026, 11, 21)

    def test_across_month_boundary(self) -> None:
        # Dec 31 T-2 -> Dec 29 (stays inside December)
        assert anchor(dt.date(2026, 12, 31), 2) == dt.date(2026, 12, 29)
        # Jan 1 T-2 -> Dec 30 (crosses the year boundary)
        assert anchor(dt.date(2027, 1, 1), 2) == dt.date(2026, 12, 30)

    def test_february_leap(self) -> None:
        # Mar 1 T-2 -> Feb 27 (2026 non-leap)
        assert anchor(dt.date(2026, 3, 1), 2) == dt.date(2026, 2, 27)
        # Mar 1 T-2 -> Feb 28 (2028 leap)
        assert anchor(dt.date(2028, 3, 1), 2) == dt.date(2028, 2, 28)

    def test_one_year_backward(self) -> None:
        assert anchor(dt.date(2027, 3, 1), 365) == dt.date(2026, 3, 1)


# ---------------------------------------------------------- table types ---
class TestTableTypeValidation:
    def test_both_types_accepted(self) -> None:
        assert validate_table_type("master", CTX) == "master"
        assert validate_table_type("transaction", CTX) == "transaction"

    def test_invalid_refused(self) -> None:
        for bad in ("Master", "MASTER", "trans", "snapshot", "", None, 3):
            with pytest.raises(ValueError, match="must be one of"):
                validate_table_type(bad, CTX)

    def test_error_names_ctx(self) -> None:
        with pytest.raises(ValueError, match=CTX):
            validate_table_type("bogus", CTX)


# --------------------------------------------------------- window_days ---
class TestWindowDaysValidation:
    def test_positive_ints_accepted(self) -> None:
        assert validate_window_days(1, CTX) == 1
        assert validate_window_days(3, CTX) == 3
        assert validate_window_days(30, CTX) == 30
        assert validate_window_days(60, CTX) == 60
        assert validate_window_days(100, CTX) == 100

    def test_zero_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            validate_window_days(0, CTX)

    def test_negative_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            validate_window_days(-3, CTX)

    def test_bool_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            validate_window_days(True, CTX)

    def test_non_int_refused(self) -> None:
        for bad in ("3", 1.5, None, [3]):
            with pytest.raises(ValueError, match=">= 1"):
                validate_window_days(bad, CTX)

    def test_error_names_ctx(self) -> None:
        with pytest.raises(ValueError, match=CTX):
            validate_window_days(0, CTX)


# ------------------------------------------------------- coverage build ---
class TestCoverageStatement:
    def test_master_snapshot_as_of_anchor(self) -> None:
        a = dt.date(2026, 11, 21)
        cov = coverage_statement("master", a, 1)
        assert cov == {"kind": COVERAGE_SNAPSHOT, "from": None, "to": a}

    def test_transaction_w1_single_day(self) -> None:
        a = dt.date(2026, 11, 21)
        cov = coverage_statement("transaction", a, 1)
        assert cov == {"kind": COVERAGE_WINDOW, "from": a, "to": a}

    def test_transaction_w3(self) -> None:
        a = dt.date(2026, 11, 21)   # a Saturday: covers Thu/Fri/Sat
        cov = coverage_statement("transaction", a, 3)
        assert cov == {"kind": COVERAGE_WINDOW,
                       "from": dt.date(2026, 11, 19), "to": a}

    def test_transaction_w30(self) -> None:
        a = dt.date(2026, 11, 21)
        cov = coverage_statement("transaction", a, 30)
        assert cov == {"kind": COVERAGE_WINDOW,
                       "from": dt.date(2026, 10, 23), "to": a}

    def test_transaction_w60(self) -> None:
        a = dt.date(2026, 11, 21)
        cov = coverage_statement("transaction", a, 60)
        assert cov == {"kind": COVERAGE_WINDOW,
                       "from": dt.date(2026, 9, 23), "to": a}

    def test_window_crosses_month_and_year(self) -> None:
        a = dt.date(2027, 1, 2)
        cov = coverage_statement("transaction", a, 3)
        assert cov == {"kind": COVERAGE_WINDOW,
                       "from": dt.date(2026, 12, 31), "to": a}

    def test_unknown_type_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown table type"):
            coverage_statement("bogus", dt.date(2026, 11, 21), 3)


# --------------------------------------------------------- module data ---
class TestModuleConstants:
    def test_lag_map_complete(self) -> None:
        assert AS_OF_LAGS == {"T-0": 0, "T-1": 1, "T-2": 2}
