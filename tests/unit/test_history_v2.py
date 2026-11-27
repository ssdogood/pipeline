# tests/unit/test_history_v2.py
"""PT-001 History DB v2: folder_runs, attempts, migration, paused statuses."""

import json
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from dpl.lib.history_db import DDL, init_db, open_db
from dpl.wrapper.update_history import ingest_manifest

#: strip every v2.1 pause column from the production DDL. Trailing
#: `-- comment` after the comma is tolerated (a comment never saves a
#: column from the strip).
_STRIP_V2_1_COLS = re.compile(
    r"^[ \t]+(?:pause_start[ \t]+TEXT"
    r"|pause_end[ \t]+TEXT"
    r"|pause_reason[ \t]+TEXT"
    r"|days_elapsed[ \t]+INTEGER"
    r"|tables_paused[ \t]+INTEGER"
    # v2.2 (PT-032/015): stripped the same way - _v1_ddl must keep
    # yielding the authentic v1 shape whatever the DDL grows
    r"|tables_not_scheduled[ \t]+INTEGER"
    r"|as_of[ \t]+TEXT"
    r"|anchor_date[ \t]+TEXT"
    r"|table_type[ \t]+TEXT"
    r"|coverage_kind[ \t]+TEXT"
    r"|coverage_from[ \t]+TEXT"
    r"|coverage_to[ \t]+TEXT"
    # v2.3 (PT-048): stripped the same way - _v1_ddl must keep
    # yielding the authentic v1 shape whatever the DDL grows
    r"|encryption_key[ \t]+TEXT"
    r"|verification_passed[ \t]+INTEGER)"
    r",([ \t]*--[^\n]*)?[ \t]*$\n?",
    flags=re.MULTILINE,
)

#: strip v1's marker column (table_runs.attempts) - comment-tolerant too.
_STRIP_ATTEMPTS = re.compile(
    r"^[ \t]+attempts[ \t]+INTEGER,([ \t]*--[^\n]*)?[ \t]*$\n?", flags=re.MULTILINE
)


def _v1_ddl() -> str:
    """The v1 schema: production DDL minus the table_runs.attempts line.

    Uses a regex anchored on the line (leading whitespace, 'attempts',
    INTEGER, trailing comma) rather than a literal string replace, so it
    cannot silently fail on whitespace differences. Note: 'attempts_used'
    in email_sends does NOT match - the character after 'attempts' must
    be whitespace here.
    """
    # v2.1 additions are subtracted too: the helper must always yield
    # the AUTHENTIC v1 shape, whatever the production DDL has grown.
    # Trailing DDL comments are tolerated after the comma (some pause
    # lines carry them) - a comment never saves a column from the strip.
    return _STRIP_ATTEMPTS.sub("", _STRIP_V2_1_COLS.sub("", DDL))


def _envelope(
    run_id="run_1",
    folder="stg",
    status="success",
    tables=None,
    counts=None,
    wrapper=None,
    started="2026-11-15T05:46:16Z",
    finished="2026-11-15T06:44:23Z",
):
    """Build an envelope. `counts` = dict of envelope-LEVEL tables_* keys
    (mirroring the real v0.3.0 layout, verified 2026-11-15)."""
    env = {
        "run_id": run_id,
        "profile": "full",
        "started_at": started,
        "finished_at": finished,
        "status": status,
        "pending_tables": [],
        "folder": folder,
        "load_date": "2026-11-15",
        "tables": tables if tables is not None else [],
    }
    if counts:
        env.update(counts)
    if wrapper:
        env["wrapper"] = wrapper
    return env


def _table(name="T1", status="extracted", attempts=1):
    return {
        "connection": "ora_db",
        "schema": "STG",
        "name": name,
        "load_date": "2026-11-15",
        "status": status,
        "rows_read": 5,
        "rows_written": 5,
        "duration_sec": 0.1,
        "output_path": "D:/out/t1",
        "error": None,
        "attempts": attempts,
    }


@pytest.fixture
def db(tmp_path):
    summary = init_db(tmp_path / "h.db")
    assert summary["migrated"] is False
    return tmp_path / "h.db"


def _ingest(db_path, env):
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(env, f)
    conn = open_db(db_path)
    try:
        ingest_manifest(conn, Path(f.name))
        conn.commit()
    finally:
        conn.close()


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── schema ─────────────────────────────────────────────────────────────


def test_v2_tables_exist(db):
    names = {
        r[0] for r in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "folder_runs", "table_runs"} <= names
    cols = {r[1] for r in _rows(db, "PRAGMA table_info(table_runs)")}
    assert "attempts" in cols


def test_attempts_roundtrip(db):
    _ingest(db, _envelope(tables=[_table(attempts=3)]))
    assert _rows(db, "SELECT attempts FROM table_runs") == [(3,)]


def test_legacy_manifest_null_attempts(db):
    t = _table()
    del t["attempts"]
    _ingest(db, _envelope(tables=[t]))
    assert _rows(db, "SELECT attempts FROM table_runs") == [(None,)]


# ── folder_runs: count mirroring (envelope-level, verified layout) ───────


def test_folder_runs_mirrors_envelope_counts_over_tables(db):
    """Counts at envelope level WIN over tables[] reality (mirror ruling).

    Deliberate mismatch: tables[] would give (2,1,1,0) by fallback, but
    the envelope declares (5,4,0,1) - the envelope is the source.
    """
    _ingest(
        db,
        _envelope(
            tables=[_table("A"), _table("B", "skipped")],
            counts={
                "tables_total": 5,
                "tables_extracted": 4,
                "tables_skipped": 0,
                "tables_failed": 1,
            },
            wrapper={"exit_code": 0},
        ),
    )
    row = _rows(
        db,
        "SELECT status, tables_total, tables_extracted, "
        "tables_skipped, tables_failed, exit_code "
        "FROM folder_runs",
    )[0]
    assert row == ("success", 5, 4, 0, 1, 0)


def test_folder_runs_exact_fallback_without_counts(db):
    _ingest(
        db,
        _envelope(tables=[_table("A"), _table("B", "skipped"), _table("C", "failed")]),
    )
    row = _rows(
        db,
        "SELECT tables_total, tables_extracted, "
        "tables_skipped, tables_failed FROM folder_runs",
    )[0]
    assert row == (3, 1, 1, 1)


def test_partial_counts_treated_as_absent(db):
    """Only two of the four count keys present -> fallback, not a mix."""
    _ingest(
        db,
        _envelope(
            tables=[_table("A"), _table("B", "skipped")],
            counts={"tables_total": 2, "tables_extracted": 2},
        ),
    )
    row = _rows(
        db,
        "SELECT tables_total, tables_extracted, "
        "tables_skipped, tables_failed FROM folder_runs",
    )[0]
    assert row == (2, 1, 1, 0)  # exact tables[] counts


def test_real_manifest_shape_roundtrips(db):
    """The exact shape of run_20261115_094603's tail: 136/136/0/0 +
    wrapper exit_code 0."""
    _ingest(
        db,
        _envelope(
            run_id="run_20261115_094603",
            folder="stg",
            tables=[_table("A"), _table("B")],
            counts={
                "tables_total": 136,
                "tables_extracted": 136,
                "tables_skipped": 0,
                "tables_failed": 0,
            },
            wrapper={"exit_code": 0, "run_finished_at": "2026-11-15T06:44:54Z"},
        ),
    )
    row = _rows(
        db, "SELECT run_id, tables_total, tables_extracted, exit_code FROM folder_runs"
    )[0]
    assert row == ("run_20261115_094603", 136, 136, 0)


def test_unknown_status_counts_only_in_total(db):
    _ingest(db, _envelope(tables=[_table("P", "paused")]))
    row = _rows(
        db,
        "SELECT tables_total, tables_extracted, "
        "tables_skipped, tables_failed FROM folder_runs",
    )[0]
    assert row == (1, 0, 0, 0)  # paused is NOT mis-filed


def test_paused_folder_stub_lands_as_row(db):
    _ingest(db, _envelope(folder="app1", status="paused", tables=[]))
    row = _rows(db, "SELECT status, tables_total FROM folder_runs")[0]
    assert row == ("paused", 0)


def test_exit_code_null_when_no_wrapper_block(db):
    _ingest(db, _envelope(tables=[_table()]))
    assert _rows(db, "SELECT exit_code FROM folder_runs") == [(None,)]


def test_table_level_paused_status_roundtrips(db):
    _ingest(db, _envelope(tables=[_table("T_PAUSE", "paused")]))
    assert _rows(db, "SELECT status FROM table_runs") == [("paused",)]


# ── idempotency ─────────────────────────────────────────────────────────


def test_reingest_is_idempotent(db):
    env = _envelope(tables=[_table()])
    _ingest(db, env)
    _ingest(db, env)
    assert _rows(db, "SELECT COUNT(*) FROM folder_runs") == [(1,)]
    assert _rows(db, "SELECT COUNT(*) FROM table_runs") == [(1,)]


# ── migration ───────────────────────────────────────────────────────────


def test_migration_renames_v1_and_keeps_rows(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v1_ddl())  # authentic v1: no attempts col
    conn.execute("INSERT INTO runs VALUES ('r','full','t',NULL,'success',0)")
    conn.execute(
        "INSERT INTO table_runs VALUES "
        "('r','stg','t1','c','S','extracted',1,1,0.1,'p',NULL)"
    )
    conn.commit()
    conn.close()

    summary = init_db(db_path)
    assert summary["migrated"] is True

    names = {
        r[0]
        for r in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "runs",
        "folder_runs",
        "table_runs",
        "runs_legacy",
        "table_runs_legacy",
    } <= names
    # old rows survive untouched in the legacy tables
    assert _rows(db_path, "SELECT table_name FROM table_runs_legacy") == [("t1",)]
    # fresh v2 tables are empty until re-ingest
    assert _rows(db_path, "SELECT COUNT(*) FROM table_runs") == [(0,)]


def test_migration_is_idempotent_rerun(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v1_ddl())
    conn.commit()
    conn.close()
    init_db(db_path)
    summary = init_db(db_path)  # second run: no-op
    assert summary["migrated"] is False


def test_migration_refuses_on_legacy_collision(tmp_path):
    """Interrupted-migration state: *_legacy tables already exist AND a
    v1 table_runs has reappeared. init must REFUSE (RuntimeError),
    never overwrite anything."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v1_ddl())  # fresh v1 database
    conn.execute("ALTER TABLE runs RENAME TO runs_legacy")
    conn.execute("ALTER TABLE table_runs RENAME TO table_runs_legacy")
    conn.executescript(_v1_ddl())  # v1 tables back again
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        init_db(db_path)


# ── v2.1 (PT-014 Commit 2): pause columns, in-place upgrade ────────────


def _v2_ddl() -> str:
    """Authentic v2: production DDL minus the v2.1 pause additions."""
    return _STRIP_V2_1_COLS.sub("", DDL)


def test_migration_upgrades_v2_in_place(tmp_path):
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v2_ddl())
    conn.commit()
    conn.close()
    summary = init_db(db_path)
    assert summary["migrated"] is False
    assert summary["upgraded_v2_1"] is True
    cols = {r[1] for r in _rows(db_path, "PRAGMA table_info(table_runs)")}
    assert {"pause_start", "pause_end", "pause_reason", "days_elapsed"} <= cols
    fcols = {r[1] for r in _rows(db_path, "PRAGMA table_info(folder_runs)")}
    assert "tables_paused" in fcols


def test_v2_1_upgrade_idempotent(tmp_path):
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v2_ddl())
    conn.commit()
    conn.close()
    init_db(db_path)
    summary = init_db(db_path)  # second run: clean no-op
    assert summary["migrated"] is False
    assert summary["upgraded_v2_1"] is False


def test_paused_table_fields_roundtrip(db):
    t = _table("T_PAUSE", "paused")
    t.update(
        {
            "pause_start": "2026-11-10",
            "pause_end": "2026-11-30",
            "pause_reason": "year-end close",
            "days_elapsed": 3,
        }
    )
    _ingest(db, _envelope(tables=[t]))
    row = _rows(
        db,
        "SELECT status, pause_start, pause_end, pause_reason, "
        "days_elapsed FROM table_runs",
    )[0]
    assert row == ("paused", "2026-11-10", "2026-11-30", "year-end close", 3)


def test_unpaused_table_has_null_pause_fields(db):
    _ingest(db, _envelope(tables=[_table()]))
    row = _rows(
        db, "SELECT pause_start, pause_end, pause_reason, days_elapsed FROM table_runs"
    )[0]
    assert row == (None, None, None, None)


def test_five_key_envelope_mirrors_including_paused(db):
    _ingest(
        db,
        _envelope(
            tables=[_table("A"), _table("P", "paused")],
            counts={
                "tables_total": 2,
                "tables_extracted": 1,
                "tables_skipped": 0,
                "tables_failed": 0,
                "tables_paused": 1,
            },
            wrapper={"exit_code": 0},
        ),
    )
    row = _rows(
        db,
        "SELECT tables_total, tables_extracted, "
        "tables_skipped, tables_failed, tables_paused "
        "FROM folder_runs",
    )[0]
    assert row == (2, 1, 0, 0, 1)


def test_four_key_envelope_mirrors_four_and_computes_paused(db):
    """Old v0.3.0 envelopes: the four declared counts WIN (mirror
    ruling preserved) AND tables_paused lands as a true computed 0."""
    _ingest(
        db,
        _envelope(
            tables=[_table("A")],
            counts={
                "tables_total": 5,
                "tables_extracted": 4,
                "tables_skipped": 0,
                "tables_failed": 1,
            },
        ),
    )
    row = _rows(
        db,
        "SELECT tables_total, tables_extracted, "
        "tables_skipped, tables_failed, tables_paused "
        "FROM folder_runs",
    )[0]
    assert row == (5, 4, 0, 1, 0)


def _v2_1_ddl() -> str:
    """The v2.1 schema: production DDL minus the v2.2 columns.

    Same technique as _v1_ddl: regex-strip the additions of the newest
    version (comment-tolerant), so the helper keeps yielding the
    authentic pre-v2.2 shape whatever the production DDL grows.
    """
    return re.sub(
        r"^[ \t]+(?:tables_not_scheduled[ \t]+INTEGER"
        r"|as_of[ \t]+TEXT"
        r"|anchor_date[ \t]+TEXT"
        r"|table_type[ \t]+TEXT"
        r"|coverage_kind[ \t]+TEXT"
        r"|coverage_from[ \t]+TEXT"
        r"|coverage_to[ \t]+TEXT)"
        r",([ \t]*--[^\n]*)?[ \t]*$\n?",
        "",
        DDL,
        flags=re.MULTILINE,
    )


class TestV2_2Migration:
    """B6: the additive v2.1 -> v2.2 migration (PT-032/015).

    Doctrine mirrored from the v2.1 tests: deploy a v2.1 DB, run
    init_db, columns appear with NULLs preserved. Scenarios: fresh,
    v2.1->v2.2, already-v2.2, half-upgraded.
    """

    @staticmethod
    def _v2_1_db(tmp_path: Path) -> Path:
        """A deployed v2.1 database with one real row, ready to upgrade."""
        db = tmp_path / "v21.db"
        conn = sqlite3.connect(db)
        conn.executescript(_v2_1_ddl())
        conn.execute(
            "INSERT INTO runs (run_id, profile, started_at, status)"
            " VALUES ('r21', 'nightly', '2026-11-20T05:00:00Z', 'success')"
        )
        conn.execute(
            "INSERT INTO folder_runs (run_id, folder, load_date, status,"
            " tables_total, tables_paused)"
            " VALUES ('r21', 'stg', '2026-11-20', 'success', 3, 1)"
        )
        conn.commit()
        conn.close()
        return db

    def test_fresh_db_has_v2_2_columns(self, tmp_path: Path) -> None:
        """A brand-new DB: the DDL alone carries every v2.2 column."""
        summary = init_db(tmp_path / "fresh.db")
        assert summary["upgraded_v2_2"] is False  # DDL, not migration
        conn = sqlite3.connect(tmp_path / "fresh.db")
        tr = {r[1] for r in conn.execute("PRAGMA table_info(table_runs)")}
        fr = {r[1] for r in conn.execute("PRAGMA table_info(folder_runs)")}
        conn.close()
        assert {"table_type", "coverage_kind", "coverage_from", "coverage_to"} <= tr
        assert {"tables_not_scheduled", "as_of", "anchor_date"} <= fr

    def test_v2_1_gains_columns_nulls_preserved(self, tmp_path: Path) -> None:
        db = self._v2_1_db(tmp_path)
        summary = init_db(db)
        assert summary["upgraded_v2_2"] is True
        conn = sqlite3.connect(db)
        fr = conn.execute(
            "SELECT tables_total, tables_paused, tables_not_scheduled,"
            " as_of, anchor_date FROM folder_runs WHERE run_id='r21'"
        ).fetchone()
        conn.close()
        # the v2.1 row survived untouched; new columns are NULL
        assert fr[0] == 3 and fr[1] == 1
        assert fr[2] is None and fr[3] is None and fr[4] is None

    def test_already_v2_2_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "v22.db"
        init_db(db)  # first init: DDL fresh shape
        summary = init_db(db)  # second init: no-op
        assert summary["upgraded_v2_2"] is False

    def test_half_upgraded_completes(self, tmp_path: Path) -> None:
        """A DB with ONE v2.2 column present must complete on re-init."""
        db = self._v2_1_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE folder_runs ADD COLUMN as_of TEXT")
        conn.commit()
        conn.close()
        init_db(db)  # must not raise
        conn = sqlite3.connect(db)
        fr = {r[1] for r in conn.execute("PRAGMA table_info(folder_runs)")}
        conn.close()
        assert {"tables_not_scheduled", "as_of", "anchor_date"} <= fr


class TestIngestV2_2:
    """B6: ingest_manifest fills the v2.2 columns from the envelope -
    and tolerates pre-B5 manifests by leaving them NULL."""

    @staticmethod
    def _ingest(tmp_path: Path, env: dict) -> dict:
        db = tmp_path / "h.db"
        init_db(db)
        mpath = tmp_path / "_manifest_test.json"
        mpath.write_text(json.dumps(env), encoding="utf-8")
        conn = open_db(db)
        try:
            ingest_manifest(conn, mpath)
            conn.commit()
            fr = conn.execute(
                "SELECT tables_not_scheduled, as_of, anchor_date,"
                " missed_days FROM folder_runs"
            ).fetchone()
            trs = conn.execute(
                "SELECT table_name, table_type, coverage_kind,"
                " coverage_from, coverage_to FROM table_runs"
                " ORDER BY table_name"
            ).fetchall()
        finally:
            conn.close()
        return {"folder_runs": fr, "table_runs": trs}

    def test_full_envelope_lands_all_columns(self, tmp_path: Path) -> None:
        env = _envelope(
            tables=[
                {
                    "name": "MASTER1",
                    "status": "extracted",
                    "type": "master",
                    "coverage_kind": "snapshot-as-of",
                    "coverage_from": None,
                    "coverage_to": "2026-11-23",
                },
                {
                    "name": "TRANS1",
                    "status": "extracted",
                    "type": "transaction",
                    "coverage_kind": "window",
                    "coverage_from": "2026-11-21",
                    "coverage_to": "2026-11-23",
                },
            ],
            counts={
                "tables_total": 2,
                "tables_extracted": 2,
                "tables_skipped": 0,
                "tables_failed": 0,
                "tables_paused": 0,
                "tables_not_scheduled": 0,
            },
        )
        env["as_of"] = "T-2"
        env["anchor_date"] = "2026-11-23"
        got = self._ingest(tmp_path, env)
        fr = got["folder_runs"]
        assert fr[0] == 0 and fr[1] == "T-2" and fr[2] == "2026-11-23"
        by_name = {r[0]: r for r in got["table_runs"]}
        m = by_name["master1"]
        assert m[1] == "master" and m[2] == "snapshot-as-of"
        assert m[3] is None and m[4] == "2026-11-23"
        t = by_name["trans1"]
        assert t[1] == "transaction" and t[2] == "window"
        assert t[3] == "2026-11-21" and t[4] == "2026-11-23"

    def test_legacy_envelope_lands_nulls(self, tmp_path: Path) -> None:
        """A pre-B5 envelope (no as_of/coverage keys) must ingest
        cleanly - history never breaks on additive evolution."""
        env = _envelope(
            tables=[
                {"name": "OLD1", "status": "extracted"},
            ],
            counts={
                "tables_total": 1,
                "tables_extracted": 1,
                "tables_skipped": 0,
                "tables_failed": 0,
                "tables_paused": 0,
            },
        )
        got = self._ingest(tmp_path, env)
        fr = got["folder_runs"]
        assert fr[0] == 0 and fr[1] is None and fr[2] is None
        row = got["table_runs"][0]
        assert row[1] is None and row[2] is None
        assert row[3] is None and row[4] is None

    def test_missed_days_ingests_as_json_not_list(self, tmp_path: Path) -> None:
        """Regression (office incident 2026-11-26): an envelope carrying a
        B7 miss report must land as JSON text in folder_runs.missed_days -
        SQLite cannot bind a Python list, and the raw list crashed the
        post-run history ingest (exit 2, repeated re-reporting of the
        same miss). The helper is the only serialization point."""
        env = _envelope(
            tables=[{"name": "T1", "status": "extracted"}],
            counts={
                "tables_total": 1,
                "tables_extracted": 1,
                "tables_skipped": 0,
                "tables_failed": 0,
                "tables_paused": 0,
                "tables_not_scheduled": 0,
            },
        )
        env["as_of"] = "T-2"
        env["anchor_date"] = "2026-11-24"
        env["missed_days"] = ["2026-11-25"]
        env["missed_days_from"] = "2026-11-25"
        env["missed_days_truncated"] = False
        got = self._ingest(tmp_path, env)
        raw = got["folder_runs"][3]
        assert raw is not None, "missed_days column must be populated"
        parsed = json.loads(raw)
        assert parsed["days"] == ["2026-11-25"]
        assert parsed["from"] == "2026-11-25"
        assert parsed["truncated"] is False

    def test_missless_envelope_ingests_null_column(self, tmp_path: Path) -> None:
        """No miss -> column stays NULL (never an empty-string list)."""
        env = _envelope(
            tables=[{"name": "T1", "status": "extracted"}],
            counts={
                "tables_total": 1,
                "tables_extracted": 1,
                "tables_skipped": 0,
                "tables_failed": 0,
                "tables_paused": 0,
                "tables_not_scheduled": 0,
            },
        )
        env["missed_days"] = []
        got = self._ingest(tmp_path, env)
        assert got["folder_runs"][3] is None


# ── v2.3 (PT-048): stg encryption columns, in-place upgrade ────────────

_STRIP_V2_3_COLS = re.compile(
    r"^[ \t]+(?:encryption_key[ \t]+TEXT"
    r"|verification_passed[ \t]+INTEGER)"
    r",([ \t]*--[^\n]*)?[ \t]*$\n?",
    flags=re.MULTILINE,
)


def _v2_2_ddl() -> str:
    """Authentic v2.2: production DDL minus the PT-048 v2.3 columns."""
    return _STRIP_V2_3_COLS.sub("", DDL)


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v2_3_upgrade_in_place_preserves_rows(tmp_path):
    db_path = tmp_path / "v22.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v2_2_ddl())
    conn.execute(
        "INSERT INTO table_runs (run_id, folder, table_name, status) "
        "VALUES ('r','stg','t1','extracted')"
    )
    conn.commit()
    conn.close()

    summary = init_db(db_path)
    assert summary["migrated"] is False
    assert summary["upgraded_v2_3"] is True

    conn = sqlite3.connect(db_path)
    assert {"encryption_key", "verification_passed"} <= _cols(conn, "table_runs")
    row = conn.execute(
        "SELECT encryption_key, verification_passed FROM table_runs "
        "WHERE table_name = 't1'"
    ).fetchone()
    assert row == (None, None)  # pre-v2.3 row keeps its NULLs
    conn.close()


def test_v2_3_upgrade_idempotent_rerun(tmp_path):
    db_path = tmp_path / "v22.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_v2_2_ddl())
    conn.commit()
    conn.close()

    init_db(db_path)
    summary = init_db(db_path)  # re-run on the upgraded DB
    assert summary["upgraded_v2_3"] is False


def test_fresh_db_is_v2_3_without_migration(tmp_path):
    db_path = tmp_path / "fresh.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)  # production DDL = v2.3 shape already
    conn.commit()
    conn.close()

    summary = init_db(db_path)
    assert summary["migrated"] is False
    assert summary["upgraded_v2_3"] is False  # DDL already had the columns
    conn = sqlite3.connect(db_path)
    assert {"encryption_key", "verification_passed"} <= _cols(conn, "table_runs")
    conn.close()


# ── v2.3 (PT-048): encryption stamp ingest ─────────────────────────────


def test_ingest_carries_encryption_stamp(db):
    env = _envelope(tables=[_table()])
    env["encryption_key"] = "stg.stg"  # envelope ROOT field (PT-048 R-14)
    env["tables"][0]["verification_passed"] = True
    _ingest(db, env)
    conn = open_db(db)
    try:
        row = conn.execute(
            "SELECT encryption_key, verification_passed FROM table_runs"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("stg.stg", 1)  # TRUE becomes SQLite INTEGER 1


def test_ingest_legacy_manifest_yields_nulls(db):
    """Pre-PT-048 manifest: no stamp fields anywhere - NULL, never 0."""
    env = _envelope(tables=[_table()])
    _ingest(db, env)
    conn = open_db(db)
    try:
        row = conn.execute(
            "SELECT encryption_key, verification_passed FROM table_runs"
        ).fetchone()
    finally:
        conn.close()
    assert row == (None, None)
