"""Unit-test kit for dpl email-service components.

Runs under the project's pytest suite (tests/unit/) AND standalone
(`python tests/unit/test_email_unit.py` from PROJECT ROOT). Needs NO
network, NO vault, NO database, NO SMTP server - runnable anywhere.

Stage 1: dpl/lib/timefmt.py (FR-20 canonical-time doctrine).
Later stages append their test groups to this same file.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from dpl.lib.timefmt import now_utc_z, require_utc_z

# ----------------------------------------------------------------- helpers --


def _must_reject(value: object, why: str) -> None:
    """Assert require_utc_z raises loudly for a non-canonical value."""
    try:
        require_utc_z(value)
    except ValueError as exc:
        msg = str(exc)
        assert "canonical" in msg and repr(value) in msg, (
            f"error should be instructive and echo the bad value; "
            f"got: {msg!r}")
        return
    raise AssertionError(
        f"require_utc_z accepted a non-canonical value ({why}): {value!r}")


# ------------------------------------------------- stage 1: timefmt (FR-20) --


def test_now_returns_canonical() -> None:
    v = now_utc_z()
    assert require_utc_z(v) == v, "now_utc_z output must itself be canonical"


def test_now_is_genuinely_utc_now() -> None:
    v = now_utc_z()
    parsed = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    skew = abs((datetime.now(UTC) - parsed).total_seconds())
    assert skew < 120, f"now_utc_z drifted from real UTC time: {skew}s"


def test_now_is_monotonic_and_sorts_as_text() -> None:
    s1 = now_utc_z()
    s2 = now_utc_z()
    assert s2 >= s1, "ISO-8601 UTC strings must sort chronologically"


def test_accepts_valid_canonical_string() -> None:
    good = "2026-11-10T14:22:33Z"
    assert require_utc_z(good) == good, "valid input must pass through as-is"


def test_rejects_naive_no_z() -> None:
    _must_reject("2026-11-10T14:22:33", "naive, no Z suffix")


def test_rejects_offset_form() -> None:
    _must_reject("2026-11-10T14:22:33+05:30", "offset instead of Z")


def test_rejects_lowercase_z() -> None:
    _must_reject("2026-11-10T14:22:33z", "lowercase z")


def test_rejects_fractional_seconds() -> None:
    _must_reject("2026-11-10T14:22:33.123Z", "fractional seconds")


def test_rejects_space_separator() -> None:
    _must_reject("2026-11-10 14:22:33Z", "space instead of T")


def test_rejects_month_13() -> None:
    _must_reject("2026-13-10T14:22:33Z", "month 13")


def test_rejects_feb_30() -> None:
    _must_reject("2026-02-30T14:22:33Z", "February 30")


def test_rejects_day_32() -> None:
    _must_reject("2026-11-32T14:22:33Z", "day 32")


def test_rejects_two_digit_year() -> None:
    _must_reject("26-11-10T14:22:33Z", "two-digit year (short shape)")


def test_rejects_empty_and_whitespace() -> None:
    _must_reject("", "empty string")
    _must_reject("   ", "whitespace")


def test_rejects_non_string_types() -> None:
    _must_reject(20261110, "int")
    _must_reject(None, "None")
    _must_reject(datetime(2026, 11, 10), "datetime object")
    _must_reject(b"2026-11-10T14:22:33Z", "bytes that would look right")


def test_format_gate_not_plausibility_gate() -> None:
    # shape-valid but semantically absurd must still be CAUGHT by callers,
    # not by require_utc_z - it validates FORMAT, not plausibility.
    stale = (datetime.now(UTC)
             - timedelta(days=365 * 100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert require_utc_z(stale) == stale, (
        "require_utc_z validates format, not plausibility - by design")


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests = [
        ("now_returns_canonical", test_now_returns_canonical),
        ("now_is_genuinely_utc_now", test_now_is_genuinely_utc_now),
        ("now_is_monotonic_and_sorts_as_text",
         test_now_is_monotonic_and_sorts_as_text),
        ("accepts_valid_canonical_string", test_accepts_valid_canonical_string),
        ("rejects_naive_no_z", test_rejects_naive_no_z),
        ("rejects_offset_form", test_rejects_offset_form),
        ("rejects_lowercase_z", test_rejects_lowercase_z),
        ("rejects_fractional_seconds", test_rejects_fractional_seconds),
        ("rejects_space_separator", test_rejects_space_separator),
        ("rejects_month_13", test_rejects_month_13),
        ("rejects_feb_30", test_rejects_feb_30),
        ("rejects_day_32", test_rejects_day_32),
        ("rejects_two_digit_year", test_rejects_two_digit_year),
        ("rejects_empty_and_whitespace", test_rejects_empty_and_whitespace),
        ("rejects_non_string_types", test_rejects_non_string_types),
        ("format_gate_not_plausibility_gate",
         test_format_gate_not_plausibility_gate),
    ]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name}: {exc}")
    total, bad = len(tests), len(failures)
    if bad:
        print(f"RESULT: {total - bad}/{total} passed, FAILED: {failures}")
        return 1
    print(f"RESULT: ALL GREEN ({total}/{total}) - stage 1 timefmt verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
