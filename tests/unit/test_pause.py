# tests/unit/test_pause.py
"""PT-014 pause window matrix - pure dict/date tests (no database,
no config files; the schema_dispatch testing pattern)."""

from datetime import date, timedelta

import pytest

from dpl.lib.pause import (
    ACTIVE,
    ARMED,
    EXPIRED,
    PAUSED,
    describe,
    effective_block,
    evaluate,
    manifest_fields,
    pause_block_ok,
    staleness,
    state_for,
    validate_pause_block,
    validate_pause_declarations,
)

TODAY = date(2026, 11, 18)
START = "2026-11-10"          # 8 days before TODAY
END = "2026-11-30"            # 12 days after TODAY


# ------------------------------------------------------ validate --------
def test_validate_happy_windowed():
    norm = validate_pause_block(
        {"start": START, "end": END, "reason": "  rebuilt at source "},
        owner="table 'X'")
    assert norm == {"start": date(2026, 11, 10),
                    "end": date(2026, 11, 30),
                    "reason": "rebuilt at source"}


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_validate_blank_end_means_indefinite(blank):
    norm = validate_pause_block(
        {"start": START, "end": blank, "reason": "hold"})
    assert norm["end"] is None


@pytest.mark.parametrize("key", ["start", "end", "reason"])
def test_validate_missing_key_raises(key):
    block = {"start": START, "end": END, "reason": "hold"}
    del block[key]
    with pytest.raises(ValueError, match=key):
        validate_pause_block(block)


def test_validate_not_a_mapping_raises():
    with pytest.raises(ValueError, match="mapping"):
        validate_pause_block("2026-11-10")


def test_validate_bad_start_format_raises():
    with pytest.raises(ValueError, match="not a valid"):
        validate_pause_block({"start": "10-11-2026", "end": END,
                              "reason": "hold"})


def test_validate_blank_start_raises():
    with pytest.raises(ValueError, match="pause.start"):
        validate_pause_block({"start": "  ", "end": END, "reason": "r"})


def test_validate_non_string_start_raises():
    with pytest.raises(ValueError, match="pause.start"):
        validate_pause_block({"start": 20261110, "end": END,
                              "reason": "r"})


def test_validate_end_before_start_raises():
    with pytest.raises(ValueError, match="before pause.start"):
        validate_pause_block({"start": "2026-11-30", "end": "2026-11-10",
                              "reason": "r"})


def test_validate_blank_reason_raises():
    with pytest.raises(ValueError, match="pause.reason"):
        validate_pause_block({"start": START, "end": END, "reason": " "})


def test_pause_block_ok_true_and_false():
    good = {"start": START, "end": END, "reason": "hold"}
    bad = {"start": START, "end": END}          # reason missing
    assert pause_block_ok(good) is True
    assert pause_block_ok(bad) is False


# ------------------------------------------------------- evaluate --------
def test_eval_future_start_is_armed():
    assert evaluate({"start": TODAY + timedelta(days=1),
                     "end": None, "reason": "r"}, TODAY) == ARMED


def test_eval_inside_window_is_paused():
    assert evaluate({"start": date(2026, 11, 1),
                     "end": date(2026, 11, 30), "reason": "r"},
                    TODAY) == PAUSED


def test_eval_boundaries_are_inclusive():
    norm_start_today = {"start": TODAY, "end": TODAY, "reason": "r"}
    assert evaluate(norm_start_today, TODAY) == PAUSED


def test_eval_after_end_is_expired():
    assert evaluate({"start": date(2026, 11, 1),
                     "end": date(2026, 11, 5), "reason": "r"},
                    TODAY) == EXPIRED


def test_eval_indefinite_never_expires():
    assert evaluate({"start": date(2020, 1, 1), "end": None,
                     "reason": "r"}, TODAY) == PAUSED


# ---------------------------------------------------- state resolution ---
def _conn(pause=None):
    return {"folder": "etl", **({"pause": pause} if pause else {})}


def _table(pause=None):
    return {"name": "ACCT_OPENING",
            **({"pause": pause} if pause else {})}


GOOD = {"start": START, "end": END, "reason": "folder maintenance"}


def test_state_active_when_no_blocks():
    state, norm = state_for(_table(), _conn(), TODAY)
    assert state == ACTIVE and norm is None


def test_state_table_block_only():
    state, norm = state_for(
        _table({"start": START, "end": "", "reason": "rebuild"}),
        _conn(), TODAY)
    assert state == PAUSED
    assert norm is not None            # <- add: pyrefly narrowing
    assert norm["reason"] == "rebuild"


def test_state_conn_block_only():
    state, norm = state_for(_table(), _conn(GOOD), TODAY)
    assert state == PAUSED
    assert norm is not None            # <- add
    assert norm["reason"] == "folder maintenance"


def test_state_folder_dominates_table():
    _state, norm = state_for(          # <- 'state' -> '_state' (ruff RUF059)
        _table({"start": START, "end": "", "reason": "table reason"}),
        _conn(GOOD), TODAY)
    assert norm is not None            # <- add
    assert norm["reason"] == "folder maintenance"


def test_state_moot_garbage_table_block_tolerated():
    # state_for never looks at the moot block; loud validation of ALL
    # blocks happens in validate_pause_declarations instead.
    state, norm = state_for(
        _table({"start": "garbage"}),
        _conn(GOOD), TODAY)
    assert state == PAUSED
    assert norm is not None            # <- add (split the chained assert)
    assert norm["reason"] == "folder maintenance"


def test_effective_block_prefers_conn():
    assert effective_block(_table({"reason": "t"}), _conn({"reason": "c"})) \
        == {"reason": "c"}
    assert effective_block(_table({"reason": "t"}), None) \
        == {"reason": "t"}
    assert effective_block(_table(), _conn()) is None


# ------------------------------------------------------- describe -------
def test_describe_armed():
    msg = describe(ARMED, {"start": TODAY + timedelta(days=2),
                           "end": None, "reason": "maintenance"},
                  TODAY)
    assert "arms in 2 day(s)" in msg
    assert "maintenance" in msg


def test_describe_paused_windowed():
    msg = describe(PAUSED, {"start": date(2026, 11, 10),
                            "end": date(2026, 11, 30),
                            "reason": "rebuild"}, TODAY)
    assert "8 day(s) elapsed" in msg
    assert "12 day(s) left" in msg
    assert "2026-11-10" in msg and "2026-11-30" in msg
    assert "rebuild" in msg


def test_describe_paused_indefinite():
    msg = describe(PAUSED, {"start": date(2026, 11, 10),
                            "end": None, "reason": "hold"}, TODAY)
    assert "no end date" in msg
    assert "8 day(s) elapsed" in msg


def test_describe_expired_nags():
    msg = describe(EXPIRED, {"start": date(2026, 11, 1),
                             "end": date(2026, 11, 5),
                             "reason": "old pause"}, TODAY)
    assert "WARNING" in msg
    assert "expired 13 day(s) ago" in msg
    assert "remove or extend the tag" in msg


# ------------------------------------------------ manifest / staleness ---
def test_manifest_fields_windowed():
    f = manifest_fields({"start": date(2026, 11, 10),
                         "end": date(2026, 11, 30),
                         "reason": "rebuild"}, TODAY)
    assert f == {"pause_start": "2026-11-10",
                 "pause_end": "2026-11-30",
                 "pause_reason": "rebuild",
                 "days_elapsed": 8}


def test_manifest_fields_indefinite():
    f = manifest_fields({"start": date(2026, 11, 10),
                         "end": None, "reason": "hold"}, TODAY)
    assert f["pause_end"] == ""


def test_staleness_indefinite_ages_from_start():
    kind, days = staleness({"start": date(2026, 11, 1),
                            "end": None, "reason": "r"}, TODAY)
    assert kind == "indefinite" and days == 17


def test_staleness_expired_ages_from_end():
    kind, days = staleness({"start": date(2026, 11, 1),
                            "end": date(2026, 11, 5),
                            "reason": "r"}, TODAY)
    assert kind == "expired" and days == 13


def test_staleness_live_window_not_stale():
    kind, days = staleness({"start": date(2026, 11, 1),
                           "end": date(2026, 11, 30),
                           "reason": "r"}, TODAY)
    assert kind is None and days == 0


def test_staleness_armed_not_stale():
    kind, days = staleness({"start": TODAY + timedelta(days=3),
                            "end": None, "reason": "r"}, TODAY)
    assert kind is None and days == 0


# ------------------------------------------------ named declarations ----
def test_declarations_pass_on_good_cfg():
    cfg = {"connections": {"ora_db": dict(_conn(GOOD))},
           "tables": [_table({"start": START, "end": "",
                              "reason": "rebuild"})]}
    assert validate_pause_declarations(cfg) is None


def test_declarations_name_the_table():
    cfg = {"connections": {"ora_db": _conn()},
           "tables": [_table({"start": "garbage"})]}
    with pytest.raises(ValueError, match="table 'ACCT_OPENING'"):
        validate_pause_declarations(cfg)


def test_declarations_name_the_connection():
    cfg = {"connections": {"ora_db": _conn({"start": START,
                                            "end": "", "reason": ""})},
           "tables": []}
    with pytest.raises(ValueError, match="connection 'ora_db'"):
        validate_pause_declarations(cfg)