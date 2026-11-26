# tests/unit/test_schedule.py
"""Exhaustive tests for the PT-032 schedule grammar module.

Pure-module coverage doctrine: 100% target, every refusal named in the
design's validation table gets a test that asserts the ValueError and
its message naming the offender (loud-refusal contract).
"""

from __future__ import annotations

import datetime as dt

import pytest

from dpl.lib.schedule import (
    is_due,
    is_due_connection,
    validate_schedule_block,
)

CTX = "pipe 'p' connection 'c'"


# ----------------------------------------------------------- happy paths ---
class TestWeekdayTokens:
    @pytest.mark.parametrize("token,offset", [
        ("mon", 0), ("tue", 1), ("wed", 2), ("thu", 3),
        ("fri", 4), ("sat", 5), ("sun", 6),
    ])
    def test_each_weekday_matches_only_its_day(
            self, token: str, offset: int) -> None:
        block = validate_schedule_block({"on": [token]}, CTX)
        base = dt.date(2026, 11, 23)          # a Monday
        assert is_due(block, base + dt.timedelta(days=offset))
        other = base + dt.timedelta(days=(offset + 1) % 7)
        assert not is_due(block, other)

    def test_range_mon_to_sun(self):
        block = validate_schedule_block(
            {"on": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, CTX)
        for offset in range(7):
            assert is_due(block, dt.date(2026, 11, 23)
                          + dt.timedelta(days=offset))


class TestDayOfMonthTokens:
    def test_fixed_day_matches(self):
        block = validate_schedule_block({"on": [2]}, CTX)
        assert is_due(block, dt.date(2026, 12, 2))
        assert not is_due(block, dt.date(2026, 12, 3))

    def test_31_never_matches_a_30_day_month(self):
        block = validate_schedule_block({"on": [31]}, CTX)
        assert is_due(block, dt.date(2026, 12, 31))     # December has 31
        assert not is_due(block, dt.date(2026, 11, 30))  # November has 30

    def test_29_february_leap_vs_non_leap(self):
        block = validate_schedule_block({"on": [29]}, CTX)
        assert is_due(block, dt.date(2028, 2, 29))      # leap year
        assert not is_due(block, dt.date(2026, 2, 28))  # non-leap: no 29th

    def test_last_feb_leap_year(self):
        block = validate_schedule_block({"on": ["last"]}, CTX)
        assert is_due(block, dt.date(2028, 2, 29))
        assert not is_due(block, dt.date(2028, 2, 28))

    def test_last_all_months_2026(self):
        block = validate_schedule_block({"on": ["last"]}, CTX)
        expected_last = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                         7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
        for month, last in expected_last.items():
            assert is_due(block, dt.date(2026, month, last))
            assert not is_due(block, dt.date(2026, month, last - 1))

    def test_mixed_list_any_match_is_due(self):
        block = validate_schedule_block({"on": ["sun", 15, "last"]}, CTX)
        assert is_due(block, dt.date(2026, 11, 15))     # integer token
        assert is_due(block, dt.date(2026, 11, 30))     # last day
        assert is_due(block, dt.date(2026, 11, 29))     # Sunday
        assert not is_due(block, dt.date(2026, 11, 16))  # Monday, not last

    def test_quoted_digit_string_accepted(self):
        block = validate_schedule_block({"on": ["15"]}, CTX)
        assert is_due(block, dt.date(2026, 11, 15))


class TestExceptForm:
    def test_except_inverts(self):
        block = validate_schedule_block({"except": ["sun", "mon"]}, CTX)
        # 2026-11-25 is a Wednesday
        assert is_due(block, dt.date(2026, 11, 25))
        assert not is_due(block, dt.date(2026, 11, 29))  # Sunday
        assert not is_due(block, dt.date(2026, 11, 30))  # Monday

    def test_except_with_dom_token(self):
        block = validate_schedule_block({"except": [17]}, CTX)
        assert not is_due(block, dt.date(2026, 12, 17))
        assert is_due(block, dt.date(2026, 12, 18))

    def test_except_empty_list_refused(self):
        with pytest.raises(ValueError, match="'except' must not be empty"):
            validate_schedule_block({"except": []}, CTX)


# ------------------------------------------------------------ refusals ---
class TestRefusals:
    def test_unknown_token_word_refused(self):
        for bad in ("mid", "frid", "MONDAY", "Mon", "tomorrow"):
            with pytest.raises(ValueError, match="invalid schedule token"):
                validate_schedule_block({"on": [bad]}, CTX)

    def test_integer_out_of_range_refused(self):
        for bad in (0, 32, 100, -3):
            with pytest.raises(ValueError, match="invalid schedule token"):
                validate_schedule_block({"on": [bad]}, CTX)

    def test_bad_quoted_digit_refused(self):
        with pytest.raises(ValueError, match="invalid schedule token"):
            validate_schedule_block({"on": ["32"]}, CTX)
        with pytest.raises(ValueError, match="invalid schedule token"):
            validate_schedule_block({"on": ["0"]}, CTX)

    def test_bool_refused(self):
        with pytest.raises(ValueError, match="invalid schedule token"):
            validate_schedule_block({"on": [True]}, CTX)

    def test_float_refused(self):
        with pytest.raises(ValueError, match="invalid schedule token"):
            validate_schedule_block({"on": [1.5]}, CTX)

    def test_on_and_except_together_refused(self):
        with pytest.raises(ValueError, match="contradiction"):
            validate_schedule_block(
                {"on": ["mon"], "except": ["tue"]}, CTX)

    def test_unknown_key_refused_forever(self):
        with pytest.raises(ValueError, match="unknown key"):
            validate_schedule_block({"on": ["mon"], "hour": [6]}, CTX)

    def test_reserved_months_key_named_in_hint(self):
        with pytest.raises(ValueError, match="months.*reserved future key"):
            validate_schedule_block({"months": [1, 4]}, CTX)

    def test_neither_on_nor_except_refused(self):
        with pytest.raises(ValueError, match="must declare 'on' or 'except'"):
            validate_schedule_block({}, CTX)

    def test_non_mapping_refused(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            validate_schedule_block(["mon"], CTX)

    def test_non_list_value_refused(self):
        with pytest.raises(ValueError, match="must be a list of tokens"):
            validate_schedule_block({"on": "mon"}, CTX)

    def test_empty_on_list_refused(self):
        with pytest.raises(ValueError, match="'on' must not be empty"):
            validate_schedule_block({"on": []}, CTX)

    def test_error_names_ctx(self):
        with pytest.raises(ValueError, match=CTX.replace("'", "'")):
            validate_schedule_block({"on": ["mid"]}, CTX)


# ------------------------------------------------------ decider contract ---
class TestDeciderContract:
    def test_is_due_never_revalidates_garbage(self):
        """The decider trusts pre-validated blocks; a garbage block's
        behavior is undefined (contract), so we only pin that valid
        shapes behave."""
        block: dict[str, list[int | str]] = {"on": ["mon"]}
        assert is_due(block, dt.date(2026, 11, 23))

    def test_block_returned_unchanged(self):
        block = validate_schedule_block({"on": [15, "last"]}, CTX)
        assert block == {"on": [15, "last"]}


class TestConnectionLevel:
    def test_absent_schedule_means_every_day(self):
        assert is_due_connection({"folder": "f"}, dt.date(2026, 11, 25))
        assert is_due_connection(
            {"folder": "f", "schedule": None}, dt.date(2026, 11, 25))

    def test_present_schedule_validated_and_applied(self):
        conn = {"folder": "ora", "schedule": {"on": ["sat"]}}
        assert is_due_connection(conn, dt.date(2026, 11, 28))  # Saturday
        assert not is_due_connection(conn, dt.date(2026, 11, 25))

    def test_present_invalid_schedule_loud_at_decider(self):
        conn = {"folder": "ora", "schedule": {"on": ["mid"]}}
        with pytest.raises(ValueError, match="invalid schedule token"):
            is_due_connection(conn, dt.date(2026, 11, 25))

    def test_ctx_uses_folder_name(self):
        conn = {"folder": "ora_prod", "schedule": {"on": ["x"]}}
        with pytest.raises(ValueError, match="ora_prod"):
            is_due_connection(conn, dt.date(2026, 11, 25))
