# tests/unit/test_schema_dispatch.py
"""Unit tests for the PT-034 dispatch matrix and tag validation.

Covers all four matrix rows, the aggregated missing-tag rule, the
mixed-intent refusal, and the aggregator's sorting. No database, no
network: decisions are pure functions of dicts.
"""

import pytest

from dpl.lib import schema_dispatch as sd

ORACLE_CONNECTOR = {"generator": "dpl.lib.generate_schemas"}
HAND_CURATED_CONNECTOR = {"generator": None}

AUTO = {"schema": "auto", "type": "oracle"}
MANUAL = {"schema": "manual", "type": "ldap"}


def test_missing_tag_fails_loud_for_pipe():
    """Row gate: no tag declared -> fail decision naming the connection."""
    d = sd.evaluate("p", ORACLE_CONNECTOR, {"ora_db": {"type": "oracle"}}, "oracle")
    assert d.action == "fail"
    assert "ora_db" in d.message
    assert "schema: auto|manual" in d.message


def test_invalid_tag_fails_loud():
    """A misspelled tag ('Auto', 'gen') is as bad as no tag."""
    d = sd.evaluate("p", ORACLE_CONNECTOR, {"ora_db": {"schema": "Auto"}}, "oracle")
    assert d.action == "fail"
    assert "ora_db" in d.message


def test_missing_tags_are_aggregated_in_one_message():
    """All offenders named together - one edit session fixes all."""
    conns = {
        "a": {"type": "oracle"},
        "b": {"schema": "auto"},
        "c": {"schema": "typo"},
        "d": AUTO,
    }
    assert sd.missing_schema_tags(conns) == ["a", "c"]
    d = sd.evaluate("p", ORACLE_CONNECTOR, conns, "oracle")
    assert d.action == "fail"
    assert "tag: a, c" in d.message   # both offenders, one enumeration


def test_row1_auto_with_generator_introspects():
    d = sd.evaluate("p", ORACLE_CONNECTOR, {"ora_db": AUTO}, "oracle")
    assert d.action == "introspect"
    assert d.generator == "dpl.lib.generate_schemas"


def test_row2_auto_without_generator_fails_that_pipe_only():
    d = sd.evaluate("ad_pipe", HAND_CURATED_CONNECTOR, {"ad_main": AUTO}, "ldap")
    assert d.action == "fail"
    assert "no generator" in d.message
    assert "schema: manual" in d.message  # tells the operator the fix


def test_row3_manual_with_generator_is_loud_skip():
    d = sd.evaluate("p", ORACLE_CONNECTOR, {"ora_db": MANUAL}, "oracle")
    assert d.action == "skip"
    assert "manual" in d.message


def test_row4_manual_without_generator_is_skip():
    d = sd.evaluate("ad_pipe", HAND_CURATED_CONNECTOR, {"ad_main": MANUAL}, "ldap")
    assert d.action == "skip"


def test_mixed_intent_is_refused():
    conns = {"a": AUTO, "b": MANUAL}
    d = sd.evaluate("p", ORACLE_CONNECTOR, conns, "oracle")
    assert d.action == "fail"
    assert "mixed schema ownership" in d.message


def test_require_schema_tags_raises_with_all_offenders():
    conns = {"x": {}, "y": {"schema": "nope"}, "z": AUTO}
    with pytest.raises(ValueError) as ei:
        sd.require_schema_tags(conns)
    msg = str(ei.value)
    assert "x" in msg and "y" in msg and "z" not in msg


def test_missing_tags_list_is_sorted_and_empty_safe():
    assert sd.missing_schema_tags({}) == []
    bad = sd.missing_schema_tags({"b": {}, "a": {}})
    assert bad == ["a", "b"]
