# tests/unit/test_schedule_decider.py
"""PT-032 B3: due-today decider wiring in BOTH extractors.

Fixture doctrine: prepare_run is heavy (vault, master CSV, rules) so
these tests reuse the same neutral-monkeypatch pattern as
test_prepare_selection.py, then drive extract_folder directly with a
fixed load_date - determinism without clock dependence. The decider
consumes the run's ONE calendar date; is_due arithmetic itself is
exhaustively covered in test_schedule.py. Here we pin the WIRING:
non-due -> zero sessions + recorded skips + manifest; due ->
normal path untouched; pause wins the status word; malformed
schedule refuses at prepare_run regardless of due-ness.

Standard zone, joins strict when the extractor package ratchet lands
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest

import dpl.extractors.ad_to_stg as adx
import dpl.extractors.oracle_to_stg as orx
from dpl.lib.environment import ResolvedEnvironment

_EVERGREEN: dict[str, str] = {
    "start": "1970-01-01",
    "end": "2999-12-31",
    "reason": "paused for test",
}

# a Sunday
SUN = dt.date(2026, 11, 29)
# a Wednesday
WED = dt.date(2026, 11, 25)


def _cfg(
    schedule: dict[str, Any] | None = None, pause: dict[str, Any] | None = None
) -> dict[str, Any]:
    conn: dict[str, Any] = {
        "type": "oracle",
        "folder": "stg",
        "retries": 1,
        "retry_backoff_seconds": 1,
        "as_of": "T-0",
    }
    if schedule is not None:
        conn["schedule"] = schedule
    if pause is not None:
        conn["pause"] = dict(pause)
    tables: list[dict[str, Any]] = [
        {
            "connection": "ora_db",
            "schema": "STG",
            "name": "T1",
            "query": "SELECT 1 FROM STG.T1",
            "type": "master",
        },
        {
            "connection": "ora_db",
            "schema": "STG",
            "name": "T2",
            "query": "SELECT 1 FROM STG.T2",
            "type": "transaction",
            "window_days": 3,
        },
    ]
    return {"connections": {"ora_db": conn}, "tables": tables, "transforms": {}}


@pytest.fixture
def neutral(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scratch = ResolvedEnvironment(
        name="development", folder=tmp_path / "config" / "development"
    )

    def _no_secrets() -> None:
        return None

    def _resolve_env() -> ResolvedEnvironment:
        return scratch

    def _pass_init(_e: object) -> None:
        return None

    def _empty_dict(_p: object) -> dict[str, Any]:
        return {}

    def _nothing(*a: object) -> None:
        return None

    for mod in (orx, adx):
        monkeypatch.setattr(mod, "validate_secrets_layout", _no_secrets)
        monkeypatch.setattr(mod, "resolve_environment", _resolve_env)
        monkeypatch.setattr(mod.pass_mgr, "init", _pass_init)
        monkeypatch.setattr(mod, "load_schema_master", _empty_dict)
        monkeypatch.setattr(mod, "validate_master_vs_config", _nothing)
        monkeypatch.setattr(mod, "load_table_rules", _empty_dict)
        monkeypatch.setattr(mod, "validate_rules_references", _nothing)


@pytest.fixture
def settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "runs_root": str(tmp_path / "runs"),
            "landing_root": str(tmp_path / "land"),
        }
    }


def _stub_ok_extractor(
    ctx: dict[str, Any], opened: list[str], load_date: dt.date
) -> Any:
    """Return a run_folder_tables stub recording the folder and
    reporting every table as extracted (attempts=1, as the real
    worker adds)."""

    def _stub(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        opened.append(str(args[0]))
        return [
            {
                "status": "ok",
                "result": orx._build_stats(t, load_date, "extracted"),
                "attempts": 1,
            }
            for t in ctx["by_folder"][args[0]]
        ]

    return _stub


class TestDueWiring:
    def test_due_connection_extracts_normally(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Schedule saying TODAY must not perturb the normal path."""
        cfg = _cfg(schedule={"on": ["sun"]})
        ctx = orx.prepare_run(cfg, settings)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", _stub_ok_extractor(ctx, opened, SUN)
        )
        monkeypatch.setattr(orx, "connect", lambda *a, **k: None)
        env = orx.extract_folder(ctx, cfg, settings, SUN, "stg")
        assert opened == ["stg"]  # session path taken
        assert env["tables_not_scheduled"] == 0
        assert all(t["status"] == "extracted" for t in env["tables"])

    def test_not_due_yields_zero_sessions_recorded_skip(
        self,
        neutral: None,
        settings: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """sun-only connection on a Wednesday: no session, manifest
        records not_scheduled for every table, run succeeds."""
        cfg = _cfg(schedule={"on": ["sun"]})
        ctx = orx.prepare_run(cfg, settings)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", lambda *a, **k: opened.append(str(a[0])) or []
        )
        env = orx.extract_folder(ctx, cfg, settings, WED, "stg")
        assert opened == []  # ZERO sessions
        assert env["status"] == "success"
        assert env["tables_not_scheduled"] == 2
        assert [t["status"] for t in env["tables"]] == [
            "not_scheduled",
            "not_scheduled",
        ]
        # manifest was written despite no extraction
        mdir = Path(str(settings["paths"]["runs_root"])) / "2026-11-25" / "stg"
        written = list(mdir.glob("_manifest_*.json"))
        assert len(written) == 1
        payload: dict[str, Any] = json.loads(written[0].read_text())
        assert payload["tables_not_scheduled"] == 2
        # quiet in logs (loudness-doctrine exception): no WARNING+
        # about the skip itself
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("not_scheduled" in r.getMessage() for r in warnings)

    def test_except_form_inverts(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """except: [sun, mon] on a Wednesday = due (Tue-Sat app)."""
        cfg = _cfg(schedule={"except": ["sun", "mon"]})
        ctx = orx.prepare_run(cfg, settings)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", lambda *a, **k: opened.append(str(a[0])) or []
        )
        orx.extract_folder(ctx, cfg, settings, WED, "stg")
        assert opened == ["stg"]  # due -> normal path

    def test_absent_schedule_due_every_day(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(schedule=None)
        ctx = orx.prepare_run(cfg, settings)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", lambda *a, **k: opened.append(str(a[0])) or []
        )
        orx.extract_folder(ctx, cfg, settings, WED, "stg")
        assert opened == ["stg"]

    def test_pause_wins_status_word(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        """Composition ruling: paused table on a non-due day is
        recorded 'paused' - the more specific absence explanation."""
        cfg = _cfg(schedule={"on": ["sun"]})
        cfg["tables"][0]["pause"] = dict(_EVERGREEN)
        ctx = orx.prepare_run(cfg, settings)
        env = orx.extract_folder(ctx, cfg, settings, WED, "stg")
        statuses = {t["name"]: t["status"] for t in env["tables"]}
        assert statuses == {"T1": "paused", "T2": "not_scheduled"}
        assert env["tables_paused"] == 1
        assert env["tables_not_scheduled"] == 1

    def test_malformed_schedule_refuses_at_prepare(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        """Grammar errors refuse the run at STARTUP, due or not - never
        silently disable the cadence."""
        cfg = _cfg(schedule={"on": ["funday"]})
        with pytest.raises(ValueError, match="invalid schedule token"):
            orx.prepare_run(cfg, settings)

    def test_both_on_and_except_refuses(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        cfg = _cfg(schedule={"on": ["sun"], "except": ["mon"]})
        with pytest.raises(ValueError, match="BOTH 'on' and 'except'"):
            orx.prepare_run(cfg, settings)


class TestADMirror:
    """R4: the AD extractor carries the identical contract."""

    def _ad_cfg(self, schedule: dict[str, Any] | None) -> dict[str, Any]:
        conn: dict[str, Any] = {
            "type": "ldap",
            "folder": "ad-main",
            "page_size": 5,
            "as_of": "T-0",
        }
        if schedule is not None:
            conn["schedule"] = schedule
        tables: list[dict[str, Any]] = [
            {
                "connection": "ad_main",
                "schema": "AD",
                "name": "subd-users",
                "search_base": "ou=x",
                "filter": "(objectClass=*)",
                "type": "master",
            }
        ]
        return {"connections": {"ad_main": conn}, "tables": tables}

    def test_ad_not_due_records_skip(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        cfg = self._ad_cfg({"on": ["sun"]})
        ctx = adx.prepare_run(cfg, settings)
        env = adx.extract_folder(ctx, cfg, settings, WED, "ad-main")
        assert env["tables_not_scheduled"] == 1
        assert env["tables"][0]["status"] == "not_scheduled"

    def test_ad_due_extracts_normally(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._ad_cfg({"on": ["sun"]})
        ctx = adx.prepare_run(cfg, settings)
        opened: list[str] = []

        def _ad_stub(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            opened.append(str(args[0]))
            return [
                {
                    "status": "ok",
                    "result": adx._build_stats(t, SUN, "extracted"),
                    "attempts": 1,
                }
                for t in ctx["by_folder"][args[0]]
            ]

        monkeypatch.setattr(adx, "run_folder_tables", _ad_stub)
        adx.extract_folder(ctx, cfg, settings, SUN, "ad-main")
        assert opened == ["ad-main"]

    def test_ad_malformed_refuses(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        cfg = self._ad_cfg({"on": ["noday"]})
        with pytest.raises(ValueError, match="invalid schedule token"):
            adx.prepare_run(cfg, settings)


class TestIgnoreSchedule:
    """B4: --ignore-schedule (Option B) - deliberate, named,
    stamped. The flag always needs a selection; forcing a non-due
    connection runs + stamps schedule_overridden in the envelope;
    due connections are never marked."""

    def test_override_forces_non_due_connection(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(schedule={"on": ["sun"]})  # Wednesday: not due
        ctx = orx.prepare_run(cfg, settings, ignore_schedule=True)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", lambda *a, **k: opened.append(str(a[0])) or []
        )
        env = orx.extract_folder(ctx, cfg, settings, WED, "stg")
        assert opened == ["stg"]  # extraction happened
        assert env.get("schedule_overridden") is True
        assert env["tables_not_scheduled"] == 0

    def test_override_on_due_connection_never_marks(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(schedule={"on": ["sun"]})
        ctx = orx.prepare_run(cfg, settings, ignore_schedule=True)
        opened: list[str] = []
        monkeypatch.setattr(
            orx, "run_folder_tables", lambda *a, **k: opened.append(str(a[0])) or []
        )
        env = orx.extract_folder(ctx, cfg, settings, SUN, "stg")
        assert opened == ["stg"]  # due anyway
        assert "schedule_overridden" not in env

    def test_no_flag_non_due_still_skips(
        self, neutral: None, settings: dict[str, Any]
    ) -> None:
        """Regression: B3 behaviour untouched when flag absent."""
        cfg = _cfg(schedule={"on": ["sun"]})
        ctx = orx.prepare_run(cfg, settings)
        env = orx.extract_folder(ctx, cfg, settings, WED, "stg")
        assert env["tables_not_scheduled"] == 2
        assert "schedule_overridden" not in env

    def test_ad_override_mirror(
        self, neutral: None, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = TestADMirror()._ad_cfg({"on": ["sun"]})
        ctx = adx.prepare_run(cfg, settings, ignore_schedule=True)

        def _ad_stub(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "status": "ok",
                    "result": adx._build_stats(t, WED, "extracted"),
                    "attempts": 1,
                }
                for t in ctx["by_folder"][args[0]]
            ]

        monkeypatch.setattr(adx, "run_folder_tables", _ad_stub)
        env = adx.extract_folder(ctx, cfg, settings, WED, "ad-main")
        assert env.get("schedule_overridden") is True
        assert env["tables"][0]["status"] == "extracted"

    def test_bare_flag_refused_at_cli_pairing_helper(self, neutral: None) -> None:
        from dpl.lib.schedule import validate_ignore_schedule_pairing

        err = validate_ignore_schedule_pairing(True, None, None)
        assert err is not None and "--ignore-schedule" in err
        assert "--connection" not in err   # extractor hint: tables only
        werr = validate_ignore_schedule_pairing(
            True, None, None, has_connection_flag=True
        )
        assert werr is not None and "--connection" in werr  # wrapper hint
        assert validate_ignore_schedule_pairing(True, ["T1"], None) is None
        assert validate_ignore_schedule_pairing(True, None, "ora_db") is None
        assert validate_ignore_schedule_pairing(False, None, None) is None
