# tests/unit/test_prepare_selection.py
"""PT-036: --tables selection vs pauses in prepare_run (both extractors).

Contract (PT-014 design + PT-036 fix, 2026-11-20):
  - naming a PAUSED table without --paused-table refuses it (excluded);
    unnamed tables proceed untouched
  - a refusal that empties the selection means ZERO jobs ({} by_folder),
    never the whole pipe (the 2026-11-18 live-run collapse bug, pinned)
  - with --paused-table the refusal does not fire; the lift verdict
    (and folder-level never-liftable) belongs to the attempt layer
Startup deps neutralized; cfg-only validators run for real.
"""

from __future__ import annotations

import pytest

from dpl.extractors import ad_to_stg as adx
from dpl.extractors import oracle_to_stg as orx

_EVERGREEN = {"start": "1970-01-01", "end": "2999-12-31",
              "reason": "paused for test"}


def _cfg(pause=None, table_pauses=None):
    conns: dict = {"ora_db": {"type": "oracle", "folder": "stg",
                              "retries": 1, "retry_backoff_seconds": 1}}
    if pause is not None:
        conns["ora_db"]["pause"] = dict(pause)
    tables = []
    for name in ("T1", "T2", "T3"):
        t: dict = {"connection": "ora_db", "schema": "STG", "name": name,
                   "query": f"SELECT 1 FROM STG.{name}"}
        if table_pauses and name in table_pauses:
            t["pause"] = dict(table_pauses[name])
        tables.append(t)
    return {"connections": conns, "tables": tables, "transforms": {}}


@pytest.fixture
def settings():
    return {"pass_mgr": {"environment": "test"}}


@pytest.fixture
def neutral(monkeypatch):
    for mod in (orx, adx):
        monkeypatch.setattr(mod, "validate_secrets_layout", lambda: None)
        monkeypatch.setattr(mod.pass_mgr, "init", lambda _e: None)
        monkeypatch.setattr(mod, "load_schema_master", lambda _p: {})
        monkeypatch.setattr(mod, "validate_master_vs_config",
                            lambda *a, **k: None)
        monkeypatch.setattr(mod, "load_table_rules", lambda _p: {})
        monkeypatch.setattr(mod, "validate_rules_references",
                            lambda *a, **k: None)


def _names(ctx):
    return [t["name"] for ts in ctx["by_folder"].values() for t in ts]


def test_unnamed_paused_table_is_not_refused(neutral, settings):
    """Pauses only bite for tables you NAME."""
    ctx = orx.prepare_run(_cfg(table_pauses={"T2": _EVERGREEN}),
                          settings, only_tables=["T1"])
    assert _names(ctx) == ["T1"]


def test_named_paused_table_refused_and_excluded(neutral, settings):
    ctx = orx.prepare_run(_cfg(table_pauses={"T2": _EVERGREEN}),
                          settings, only_tables=["T1", "T2"])
    assert _names(ctx) == ["T1"]


def test_all_named_paused_collapses_to_zero_jobs(neutral, settings):
    """2026-11-18 live-run finding, pinned forever."""
    ctx = orx.prepare_run(_cfg(table_pauses={"T2": _EVERGREEN}),
                          settings, only_tables=["T2"])
    assert ctx["by_folder"] == {}


def test_folder_pause_refuses_named_table(neutral, settings):
    ctx = orx.prepare_run(_cfg(pause=_EVERGREEN), settings,
                          only_tables=["T1"])
    assert ctx["by_folder"] == {}


def test_override_suppresses_refusal(neutral, settings):
    """PT-036 fix: with the switch given, selection proceeds; the lift
    verdict (incl. folder never-liftable) is the attempt layer's call."""
    ctx = orx.prepare_run(_cfg(table_pauses={"T2": _EVERGREEN}),
                          settings, only_tables=["T2"],
                          paused_override=True)
    assert _names(ctx) == ["T2"]


def test_ad_mirror_same_contract(neutral, settings):
    conns = {"ad_main": {"type": "ldap", "folder": "ad-main",
                         "page_size": 5}}
    tables = []
    for name in ("subd-users", "subd-groups"):
        t: dict = {"connection": "ad_main", "schema": "AD", "name": name,
                   "search_base": "ou=x", "filter": "(objectClass=*)"}
        if name == "subd-users":
            t["pause"] = dict(_EVERGREEN)
        tables.append(t)
    cfg = {"connections": conns, "tables": tables}

    ctx = adx.prepare_run(cfg, settings, only_tables=["subd-users"])
    assert ctx["by_folder"] == {}                       # refused

    ctx = adx.prepare_run(cfg, settings, only_tables=["subd-users"],
                          paused_override=True)
    assert _names(ctx) == ["subd-users"]                # fix: proceeds