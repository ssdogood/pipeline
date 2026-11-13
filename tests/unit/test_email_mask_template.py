"""Unit tests for email_service mask.py + template.py (stage 4b).

Runs under pytest AND standalone:
    python tests/unit/test_email_mask_template.py

(Lives in its own file rather than appending to test_email_unit.py:
that file is verified on disk and carries ruff's own --fix edits -
reissuing it would risk undoing them. New stage, new file.)

Two groups:
  MASK  - the deterministic address-masking ladder and its refusal to
          touch non-strings;
  TPL   - the renderer: substitution, reserved-key stamping and the
          UTC gate, collect-all TemplateError, single-pass injection
          resistance, unused-key tolerance.

Type-ignore discipline: tests that DELIBERATELY pass wrong types (to
prove TypeError) carry '# type: ignore[arg-type]' on the SAME LINE as
the offending argument - suppressions are matched by line, so multi-
line calls must either be collapsed to one line or have the comment on
the reported line. Wrong-type calls here are kept single-line.
"""
from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable

import pytest

from dpl.lib.email_service.mask import mask_address
from dpl.lib.email_service.template import (
    TemplateError,
    render_template,
    stamp_reserved,
)

SAMPLE_RESERVED = stamp_reserved(
    caller="daily_report", send_id="S-0001",
    generated_at="2026-11-11T09:30:00Z")


# -------------------------------------------------------------------- MASK ---

def test_mask_normal_address_keeps_four_ends() -> None:
    assert mask_address("ops.team@example.org") == "ops.***.org"


def test_mask_twelve_chars_full_rule() -> None:
    assert mask_address("abcdefghijkl") == "abcd***ijkl"


def test_mask_eleven_chars_floor_applies() -> None:
    assert mask_address("abcdefghijk") == "ab***jk"


def test_mask_eight_chars_floor_applies() -> None:
    assert mask_address("abcdefgh") == "ab***gh"


def test_mask_seven_chars_fully_masked() -> None:
    assert mask_address("ab@cdef") == "****"


def test_mask_micro_address_fully_masked() -> None:
    assert mask_address("a@b.co") == "****"


def test_mask_empty_string_fully_masked() -> None:
    assert mask_address("") == "****"


def test_mask_is_deterministic() -> None:
    for sample in ("ops.team@example.org", "abcdefgh", "a@b.co"):
        assert mask_address(sample) == mask_address(sample), sample


def test_mask_never_equals_original() -> None:
    for sample in ("ops.team@example.org", "abcdefghijkl",
                   "abcdefgh", "ab@cdef", "a@b.co"):
        assert mask_address(sample) != sample, sample


def test_mask_rejects_non_strings() -> None:
    for bad in (None, 123, ["a@b.co"], {"a": "b@co"}):
        with pytest.raises(TypeError):
            mask_address(bad)            # type: ignore[arg-type]


# --------------------------------------------------------------------- TPL ---

def test_render_basic_substitution() -> None:
    out = render_template("Hello ${name}, rows=${rows}",
                          {"name": "team", "rows": 1200}, SAMPLE_RESERVED)
    assert out == "Hello team, rows=1200"


def test_render_repeated_placeholder() -> None:
    out = render_template("${run_id} ... ${run_id}",
                          {"run_id": "R9"}, SAMPLE_RESERVED)
    assert out == "R9 ... R9"


def test_render_reserved_keys_stamped() -> None:
    text = "Ref ${send_id} at ${generated_at} by ${caller}"
    out = render_template(text, {}, SAMPLE_RESERVED)
    assert "S-0001" in out
    assert "2026-11-11T09:30:00Z" in out
    assert "daily_report" in out


def test_render_reserved_key_collision_rejected() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("x ${generated_at}",
                        {"generated_at": "2026-01-01T00:00:00Z"},
                        SAMPLE_RESERVED)
    assert ei.value.collisions == ["generated_at"]
    assert "different name" in str(ei.value), "hint must point at renaming"


def test_render_missing_key_rejected_named() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("Rows: ${row_count}", {}, SAMPLE_RESERVED)
    assert ei.value.missing == ["row_count"]


def test_render_collects_all_missing_keys() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("${a} ${b} ${a}", {}, SAMPLE_RESERVED)
    assert ei.value.missing == ["a", "b"], \
        "both missing keys named once each, in appearance order"


def test_render_unused_keys_ignored() -> None:
    out = render_template("Hi ${name}",
                          {"name": "ops", "irrelevant": "x"},
                          SAMPLE_RESERVED)
    assert out == "Hi ops"


def test_render_non_scalar_value_rejected() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("t ${rows}", {"rows": [1, 2]}, SAMPLE_RESERVED)
    assert ei.value.non_scalars == ["rows"]


def test_render_none_value_rejected() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("t ${v}", {"v": None}, SAMPLE_RESERVED)
    assert ei.value.non_scalars == ["v"]


def test_render_collision_and_missing_collected_together() -> None:
    with pytest.raises(TemplateError) as ei:
        render_template("${send_id} and ${other}",
                        {"send_id": "FAKE"}, SAMPLE_RESERVED)
    assert ei.value.collisions == ["send_id"]
    assert ei.value.missing == ["other"], \
        "the collision must not hide the missing key"


def test_stamp_reserved_rejects_non_utc_generated_at() -> None:
    for bad in ("2026-11-11T09:30:00",        # no Z
                "2026-11-11 09:30:00Z",      # space separator
                "2026-11-11T09:30:00.123Z"):  # fractional seconds
        with pytest.raises(ValueError):
            stamp_reserved("c", "S-1", bad)


def test_render_non_mapping_arguments_rejected() -> None:
    with pytest.raises(TypeError):
        render_template("t", ["not", "a", "map"], SAMPLE_RESERVED)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        render_template("t", {}, ["not", "a", "map"])  # type: ignore[arg-type]


def test_render_invalid_placeholder_names_left_literal() -> None:
    out = render_template("cost ${1abc} ${}", {}, SAMPLE_RESERVED)
    assert out == "cost ${1abc} ${}"


def test_render_value_placeholder_not_reexpanded() -> None:
    # single-pass substitution: a ${...} inside a VALUE stays literal -
    # caller data cannot smuggle in a second-round substitution
    out = render_template("note ${note}",
                          {"note": "see ${send_id} here"},
                          SAMPLE_RESERVED)
    assert out == "note see ${send_id} here"


def test_render_scalar_types_converted() -> None:
    out = render_template(
        "${i} ${f} ${b} ${s}",
        {"i": 7, "f": 1.5, "b": True, "s": "x"},
        SAMPLE_RESERVED)
    assert out == "7 1.5 True x"


def test_render_empty_and_placeholder_free_text() -> None:
    assert render_template("", {}, SAMPLE_RESERVED) == ""
    assert render_template("no placeholders here", {},
                           SAMPLE_RESERVED) == "no placeholders here"


def test_render_is_repeatable() -> None:
    args: tuple[str, dict[str, object], dict[str, str]] = (
        "${a}-${a}", {"a": "z"}, SAMPLE_RESERVED)
    assert render_template(*args) == render_template(*args) == "z-z"


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests: list[tuple[str, Callable[[], object]]] = [
        ("mask_normal_address_keeps_four_ends",
         test_mask_normal_address_keeps_four_ends),
        ("mask_twelve_chars_full_rule", test_mask_twelve_chars_full_rule),
        ("mask_eleven_chars_floor_applies",
         test_mask_eleven_chars_floor_applies),
        ("mask_eight_chars_floor_applies",
         test_mask_eight_chars_floor_applies),
        ("mask_seven_chars_fully_masked",
         test_mask_seven_chars_fully_masked),
        ("mask_micro_address_fully_masked",
         test_mask_micro_address_fully_masked),
        ("mask_empty_string_fully_masked",
         test_mask_empty_string_fully_masked),
        ("mask_is_deterministic", test_mask_is_deterministic),
        ("mask_never_equals_original", test_mask_never_equals_original),
        ("mask_rejects_non_strings", test_mask_rejects_non_strings),
        ("render_basic_substitution", test_render_basic_substitution),
        ("render_repeated_placeholder", test_render_repeated_placeholder),
        ("render_reserved_keys_stamped", test_render_reserved_keys_stamped),
        ("render_reserved_key_collision_rejected",
         test_render_reserved_key_collision_rejected),
        ("render_missing_key_rejected_named",
         test_render_missing_key_rejected_named),
        ("render_collects_all_missing_keys",
         test_render_collects_all_missing_keys),
        ("render_unused_keys_ignored", test_render_unused_keys_ignored),
        ("render_non_scalar_value_rejected",
         test_render_non_scalar_value_rejected),
        ("render_none_value_rejected", test_render_none_value_rejected),
        ("render_collision_and_missing_collected_together",
         test_render_collision_and_missing_collected_together),
        ("stamp_reserved_rejects_non_utc_generated_at",
         test_stamp_reserved_rejects_non_utc_generated_at),
        ("render_non_mapping_arguments_rejected",
         test_render_non_mapping_arguments_rejected),
        ("render_invalid_placeholder_names_left_literal",
         test_render_invalid_placeholder_names_left_literal),
        ("render_value_placeholder_not_reexpanded",
         test_render_value_placeholder_not_reexpanded),
        ("render_scalar_types_converted", test_render_scalar_types_converted),
        ("render_empty_and_placeholder_free_text",
         test_render_empty_and_placeholder_free_text),
        ("render_is_repeatable", test_render_is_repeatable),
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory():
        for name, fn in tests:
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as exc:        # noqa: BLE001 - runner sentinel
                failures.append(name)
                print(f"  FAIL {name}: {exc}")
    total, bad = len(tests), len(failures)
    if bad:
        print(f"RESULT: {total - bad}/{total} passed, FAILED: {failures}")
        return 1
    print(f"RESULT: ALL GREEN ({total}/{total}) - "
          f"stage 4b mask+template verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
