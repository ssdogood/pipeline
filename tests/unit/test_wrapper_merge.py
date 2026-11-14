# tests/unit/test_wrapper_merge.py
"""Unit tests for the C2 merged-scheduler helpers in dpl.wrapper.run.

Pure, offline: fake Pipe-like objects and fake connection summaries.
Proves the merge guarantees the wrapper depends on - no silent
cross-pipe name collisions, idle folders never scheduled, serial
groups spanning pipes fold into ONE chain, duplicate folders raise.
"""
from __future__ import annotations

import pytest

from dpl.lib.extraction_config import build_plan
from dpl.wrapper.run import _folder_owners, _namespaced_connections


def _conn(folder, tconc=1, serial=None):
    out = {"folder": folder, "type": "oracle",
           "table_concurrency": tconc, "retries": 2,
           "retry_backoff_seconds": 5}
    if serial:
        out["serial_group"] = serial
    return out


class _Pipe:
    def __init__(self, name):
        self.name = name


# ---------------------------------------------------------- namespace ---

def test_same_connection_name_in_two_pipes_both_survive():
    merged = _namespaced_connections([
        ("oracle_to_stg", {"ora_db": _conn("f1")}),
        ("mssql_app", {"ora_db": _conn("f2")}),
    ])
    assert set(merged) == {"oracle_to_stg::ora_db", "mssql_app::ora_db"}
    assert merged["oracle_to_stg::ora_db"]["folder"] == "f1"
    assert merged["mssql_app::ora_db"]["folder"] == "f2"


def test_no_filter_includes_everything():
    merged = _namespaced_connections([("p", {"a": _conn("f1"),
                                             "b": _conn("f2")})])
    assert set(merged) == {"p::a", "p::b"}


def test_work_filter_excludes_idle_folders():
    # --tables selected nothing in f2's pipe: f2 must not be scheduled
    merged = _namespaced_connections(
        [("p", {"a": _conn("f1"), "b": _conn("f2")})],
        folders_with_work={"f1"})
    assert set(merged) == {"p::a"}


# ------------------------------------------------------------ owners ---

def test_folder_owners_routes_each_folder_to_its_pipe():
    p1, p2 = _Pipe("oracle_to_stg"), _Pipe("ad_to_stg")
    ready = [
        (p1, {"cfg": 1}, {"ora": _conn("f1"), "hr": _conn("f2")},
         {"by_folder": {"f1": [], "f2": []}}),
        (p2, {"cfg": 2}, {"ldap": _conn("users")}, {"by_folder": {"users": []}}),
    ]
    owners = _folder_owners(ready)
    assert owners["f1"] == (p1, {"cfg": 1}, {"by_folder": {"f1": [], "f2": []}})
    assert owners["f2"] == (p1, {"cfg": 1}, {"by_folder": {"f1": [], "f2": []}})
    assert owners["users"] == (p2, {"cfg": 2}, {"by_folder": {"users": []}})
    assert len(owners) == 3


# ------------------------------------------------- plan integration ---

def test_serial_group_spans_pipes_as_one_chain():
    merged = _namespaced_connections([
        ("p1", {"a": _conn("f1", serial="box-a")}),
        ("p2", {"b": _conn("f2", serial="box-a")}),
    ])
    plan = build_plan(1, merged)
    assert plan["serial_groups"] == {"box-a": ["p1::a", "p2::b"]}
    assert plan["ungrouped"] == []
    assert plan["tasks"] == 1          # one chain, not two folders


def test_duplicate_folder_across_pipes_raises_loudly():
    merged = _namespaced_connections([
        ("p1", {"a": _conn("dup")}),
        ("p2", {"b": _conn("dup")}),
    ])
    with pytest.raises(ValueError, match="duplicate folder"):
        build_plan(1, merged)
