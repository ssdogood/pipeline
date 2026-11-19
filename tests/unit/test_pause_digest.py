# tests/unit/test_pause_digest.py
"""PT-014 Commit 2: stale-pause digest scanning (pure module)."""

from datetime import date, timedelta

import pytest

from dpl.lib.common import SETTINGS_SCHEMA, SchemaError
from dpl.lib.pause_digest import digest_payload, stale_pause_entries

TODAY = date(2026, 11, 19)          # fixed clock - deterministic tests


def _iso(days_ago: int) -> str:
    """Calendar date TODAY-minus-N as an ISO string (negative = future)."""
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _pipe_cfg(*, conn_pause=None, table_pauses=()):
    """One pipe config snapshot; table_pauses are (name, block) pairs."""
    tables = [{"name": name, "connection": "ora_db", "pause": block}
              for name, block in table_pauses]
    conns = {"ora_db": {"type": "oracle", "folder": "stg"}}
    if conn_pause is not None:
        conns["ora_db"]["pause"] = conn_pause
    return {"connections": conns, "tables": tables}


def _indefinite(start_days_ago: int,
                reason: str = "waiting on vendor") -> dict:
    return {"start": _iso(start_days_ago), "end": "", "reason": reason}


def _expired(start_days_ago: int, end_days_ago: int,
             reason: str = "leftover tag") -> dict:
    return {"start": _iso(start_days_ago), "end": _iso(end_days_ago),
            "reason": reason}


def test_indefinite_pause_stale():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("ACCT", _indefinite(21))])}
    entries = stale_pause_entries(cfgs, 14, today=TODAY)
    assert len(entries) == 1
    e = entries[0]
    assert (e["pipe"], e["owner"], e["kind"], e["days"]) == (
        "oracle_to_stg", "table 'ACCT'", "indefinite", 21)
    assert e["reason"] == "waiting on vendor"


def test_expired_tag_stale():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("FUND", _expired(60, 16))])}
    entries = stale_pause_entries(cfgs, 14, today=TODAY)
    assert [(e["kind"], e["days"]) for e in entries] == [("expired", 16)]


def test_live_window_not_stale():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("T", {"start": _iso(5), "end": _iso(-9),
                             "reason": "inside its window"})])}
    assert stale_pause_entries(cfgs, 14, today=TODAY) == []


def test_armed_not_stale():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("T", {"start": _iso(-2), "end": "",
                             "reason": "staged for next week"})])}
    assert stale_pause_entries(cfgs, 14, today=TODAY) == []


def test_threshold_is_inclusive():
    at = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("T", _indefinite(14))])}
    under = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("T", _indefinite(13))])}
    assert len(stale_pause_entries(at, 14, today=TODAY)) == 1
    assert stale_pause_entries(under, 14, today=TODAY) == []


def test_one_day_old_counts_with_low_threshold():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("T", _expired(30, 1))])}
    entries = stale_pause_entries(cfgs, 1, today=TODAY)
    assert [e["days"] for e in entries] == [1]


def test_line_text_exact():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        table_pauses=[("ACCT", _indefinite(21, reason="year-end close"))])}
    (e,) = stale_pause_entries(cfgs, 14, today=TODAY)
    assert e["line"] == (
        "oracle_to_stg, table 'ACCT': indefinite, 21 day(s) "
        f"(since {_iso(21)}): year-end close")


def test_folder_and_moot_table_both_scanned():
    cfgs = {"oracle_to_stg": _pipe_cfg(
        conn_pause=_indefinite(30, reason="folder freeze"),
        table_pauses=[("T", _expired(60, 20))])}
    entries = stale_pause_entries(cfgs, 14, today=TODAY)
    assert [e["owner"] for e in entries] == [
        "connection 'ora_db'", "table 'T'"]


def test_digest_payload_none_when_nothing_stale():
    assert digest_payload([], 14) is None


def test_digest_payload_shape():
    payload = digest_payload([{"line": "a-line"}, {"line": "b-line"}], 14)
    assert payload == {"stale_threshold": 14, "stale_count": 2,
                       "stale_lines": "a-line\nb-line"}


def _base_settings():
    return {
        "paths": {"landing_root": "data/staging",
                  "scratch_root": "data/scratch"},
        "duckdb": {"memory_limit": "16GB"},
    }


def test_settings_pause_block_valid_and_defaulted():
    filled = SETTINGS_SCHEMA.validate({**_base_settings(),
                                       "pause": {"stale_days": 7}})
    assert filled["pause"]["stale_days"] == 7
    # empty block -> schema default 14 (the wrapper also defaults when
    # the whole block is absent - the two defaults must agree)
    assert SETTINGS_SCHEMA.validate({**_base_settings(), "pause": {}})[
        "pause"]["stale_days"] == 14

def test_settings_pause_block_invalid_is_loud():
    with pytest.raises(SchemaError):
        SETTINGS_SCHEMA.validate({**_base_settings(),
                                   "pause": {"stale_days": 0}})
    with pytest.raises(SchemaError):
        SETTINGS_SCHEMA.validate({**_base_settings(),
                                   "pause": {"stale_days": "x"}})