# tests/unit/test_extraction_config.py
"""Unit tests for extraction_config: knobs, serial groups, plan math.

Offline/hermetic per doctrine - dict fixtures only, no connections.
Covering the fail-loud matrix and the plan arithmetic the dry-run
report shows operators.
"""
from __future__ import annotations

import pytest

from dpl.lib.extraction_config import (
    build_plan,
    format_plan,
    validate_connection,
    validate_global_extraction,
    validate_pipe_connections,
)


def _cfg(**over):
    """A valid minimal extractor-yaml-shaped config; kwargs override."""
    conn = {"type": "oracle", "folder": "etl"}
    conn.update(over)
    return {"connections": {"ora_db": conn}}


def _settings(**over):
    block = {"max_parallel_folders": 4, "keepalive_minutes": 10}
    block.update(over)
    return {"extraction": block}


# ------------------------------------------------------------------ global --

def test_global_defaults_when_block_missing():
    assert validate_global_extraction({}) == {
        "max_parallel_folders": 1, "keepalive_minutes": 10}


def test_global_missing_block_warns(caplog):
    validate_global_extraction({})
    assert any("SEQUENTIAL" in r.message for r in caplog.records)


def test_global_explicit_values_roundtrip():
    assert validate_global_extraction(_settings(
        max_parallel_folders=2, keepalive_minutes=15)) == {
        "max_parallel_folders": 2, "keepalive_minutes": 15}


@pytest.mark.parametrize("bad", [0, -1, True, "4", 2.5, None])
def test_global_bad_max_parallel_folders_fails_loud(bad):
    with pytest.raises(ValueError, match="max_parallel_folders"):
        validate_global_extraction(_settings(max_parallel_folders=bad))


@pytest.mark.parametrize("bad", [0, -1, True, "10", None])
def test_global_bad_keepalive_fails_loud(bad):
    with pytest.raises(ValueError, match="keepalive_minutes"):
        validate_global_extraction(_settings(keepalive_minutes=bad))


# ------------------------------------------------------------- connections --

def test_connection_defaults_applied():
    k = validate_connection("p", "ora_db", _cfg()["connections"]["ora_db"])
    assert k["table_concurrency"] == 1
    assert k["retries"] == 2
    assert k["retry_backoff_seconds"] == 5
    assert "serial_group" not in k


def test_connection_overrides_roundtrip():
    k = validate_connection("p", "ora_db", _cfg(
        table_concurrency=2, retries=1,
        retry_backoff_seconds=10, serial_group="box_a"
    )["connections"]["ora_db"])
    assert (k["table_concurrency"], k["retries"],
            k["retry_backoff_seconds"], k["serial_group"]) == (2, 1, 10, "box_a")


@pytest.mark.parametrize("bad", [0, -1, 3, True, "2", None])
def test_bad_table_concurrency_fails_loud(bad):
    with pytest.raises(ValueError, match="table_concurrency"):
        validate_connection("p", "ora_db", _cfg(table_concurrency=bad)["connections"]["ora_db"])

@pytest.mark.parametrize("good", [1, 2])
def test_table_concurrency_ceiling_allows_1_and_2(good):
    k = validate_connection(
        "p", "ora_db", _cfg(table_concurrency=good)["connections"]["ora_db"])
    assert k["table_concurrency"] == good

@pytest.mark.parametrize("bad", [-1, True, "2", None])
def test_bad_retries_fails_loud(bad):
    with pytest.raises(ValueError, match="'retries'"):
        validate_connection("p", "ora_db", _cfg(retries=bad)["connections"]["ora_db"])


@pytest.mark.parametrize("bad", [0, -1, True, "5", None])
def test_bad_backoff_fails_loud(bad):
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        validate_connection("p", "ora_db", _cfg(retry_backoff_seconds=bad)["connections"]["ora_db"])


@pytest.mark.parametrize("good", ["box_a", "a1", "box-a", "box1", "a"])
def test_serial_group_valid_names(good):
    k = validate_connection("p", "ora_db", _cfg(serial_group=good)["connections"]["ora_db"])
    assert k["serial_group"] == good


@pytest.mark.parametrize("bad", ["Box", "box a", "", "-x", "_x", "box.a", 5, None])
def test_serial_group_invalid_names_fail_loud(bad):
    with pytest.raises(ValueError, match="serial_group"):
        validate_connection("p", "ora_db", _cfg(serial_group=bad)["connections"]["ora_db"])


def test_pipe_validation_names_pipe_and_connection():
    with pytest.raises(ValueError) as ei:
        validate_pipe_connections("oracle_to_stg", _cfg(retries=-5))
    msg = str(ei.value)
    assert "oracle_to_stg" in msg and "ora_db" in msg and "'retries'" in msg


# -------------------------------------------------------------------- plan --

def _three_conns():
    return {
        "ora_db": {"type": "oracle", "folder": "etl",
                   "table_concurrency": 1, "retries": 2,
                   "retry_backoff_seconds": 5},
        "ad_main": {"type": "ldap", "folder": "ad-main",
                    "table_concurrency": 2, "retries": 2,
                    "retry_backoff_seconds": 5, "serial_group": "box_a"},
        "ad_atm": {"type": "ldap", "folder": "ad-atm",
                   "table_concurrency": 1, "retries": 2,
                   "retry_backoff_seconds": 5, "serial_group": "box_a"},
    }


def test_plan_counts_tasks_with_chain():
    plan = build_plan(4, _three_conns())
    assert plan["ungrouped"] == ["ora_db"]
    assert plan["serial_groups"] == {"box_a": ["ad_main", "ad_atm"]}
    assert plan["tasks"] == 2          # 1 free folder + 1 chain


def test_plan_no_groups_all_free():
    conns = {f"c{i}": {"type": "oracle", "folder": f"f{i}",
                       "table_concurrency": 1} for i in range(3)}
    plan = build_plan(4, conns)
    assert plan["tasks"] == 3
    assert plan["serial_groups"] == {}


def test_plan_worst_case_sessions_mixed_concurrency():
    # cap 2 x max conc 2 = 4 ; cap 4 x max conc 2 = 8
    assert build_plan(2, _three_conns())["worst_case_sessions"] == 4
    assert build_plan(4, _three_conns())["worst_case_sessions"] == 8


def test_plan_duplicate_folders_raise():
    conns = {
        "a": {"type": "oracle", "folder": "same", "table_concurrency": 1},
        "b": {"type": "ldap", "folder": "same", "table_concurrency": 1},
    }
    with pytest.raises(ValueError, match="duplicate folder 'same'"):
        build_plan(4, conns)


def test_plan_group_order_preserved():
    plan = build_plan(4, _three_conns())
    assert plan["serial_groups"]["box_a"] == ["ad_main", "ad_atm"]


def test_single_member_group_is_one_task():
    conns = {
        "a": {"type": "oracle", "folder": "f1",
              "table_concurrency": 1, "serial_group": "solo"},
        "b": {"type": "oracle", "folder": "f2", "table_concurrency": 1},
    }
    assert build_plan(4, conns)["tasks"] == 2


def test_format_plan_operator_block_mentions_keys():
    text = format_plan(build_plan(4, _three_conns()))
    for needle in ("max_parallel_folders", "box_a", "worst-case sessions",
                   "schedulable tasks", "NEVER run in parallel"):
        assert needle in text
