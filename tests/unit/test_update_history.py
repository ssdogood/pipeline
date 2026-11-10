"""Regression tests for the stage-3 refactor of wrapper/update_history.py.

Purpose: prove the plumbing swap (connections/DDL/path rules moved to
dpl.lib.history_db) changed NOTHING observable in manifest ingestion.

Runs under pytest AND standalone:
    python tests/unit/test_update_history.py

Needs NO real settings file, NO real landing root, NO network - every
test fabricates a miniature runs_root with synthetic manifest envelopes
and a throwaway database.

Doctrine note: this file, like the module it tests, never imports
sqlite3 - all DB contact is via dpl.lib.history_db.
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from dpl.lib.history_db import init_db, open_db
from dpl.wrapper.update_history import discover_manifests, update_history

# ----------------------------------------------------------------- helpers --

def fake_settings(tmp: Path) -> dict:
    """Settings dict shaped like the real one, rooted in tmp."""
    return {"paths": {"landing_root": str(tmp / "landing"),
                      "runs_root": str(tmp / "runs")}}


def write_manifest(tmp: Path, *, date_dir: str, folder: str, run_id: str,
                   status: str = "success", finished_at: str = "2026-11-09T21:45:12",
                   tables: list[dict] | None = None) -> Path:
    """Create <runs_root>/<date_dir>/<folder>/_manifest_<run_id>.json."""
    folder_dir = tmp / "runs" / date_dir / folder
    folder_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "run_id": run_id,
        "profile": "laptop",
        "started_at": "2026-11-09T21:00:00",
        "finished_at": finished_at,
        "status": status,
        "folder": folder,
        "tables": tables if tables is not None else [
            {"name": "HR_EMPLOYEES", "status": "success",
             "rows_read": 1200, "rows_written": 1200,
             "duration_sec": 4.2, "output_path": "x.parquet"},
            {"name": "AD_USERS", "status": "success",
             "rows_read": 340, "rows_written": 340,
             "duration_sec": 1.1, "output_path": "y.parquet"},
        ],
    }
    path = folder_dir / f"_manifest_{run_id}.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def rows_in(db: Path, sql: str) -> list[tuple]:
    conn = open_db(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------ regression ---

def test_ingest_populates_runs_and_table_runs(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_db(db)
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    ingested, skipped = update_history(fake_settings(tmp_path), db)
    assert (ingested, skipped) == (1, 0)
    assert len(rows_in(db, "SELECT * FROM runs")) == 1
    assert len(rows_in(db, "SELECT * FROM table_runs")) == 2


def test_table_names_lowercased(tmp_path: Path) -> None:
    # original behavior: envelope table names are stored lowercased
    db = tmp_path / "hist.db"
    init_db(db)
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    update_history(fake_settings(tmp_path), db)
    names = {r[0] for r in rows_in(db, "SELECT table_name FROM table_runs")}
    assert names == {"hr_employees", "ad_users"}, \
        "upper-case envelope names must be lowercased on ingest"


def test_reingest_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_db(db)
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    update_history(fake_settings(tmp_path), db)
    ingested, skipped = update_history(fake_settings(tmp_path), db)
    assert (ingested, skipped) == (0, 1), "second pass must skip everything"
    assert len(rows_in(db, "SELECT * FROM table_runs")) == 2, \
        "rows must not duplicate on re-ingest"


def test_differential_backfill_new_manifest_only(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_db(db)
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    update_history(fake_settings(tmp_path), db)
    # a NEW run appears; the old one is unchanged
    write_manifest(tmp_path, date_dir="2026-11-10", folder="ad",
                   run_id="RUN-2")
    ingested, skipped = update_history(fake_settings(tmp_path), db)
    assert (ingested, skipped) == (1, 1), \
        "exactly the new manifest ingested, the old one skipped"
    assert len(rows_in(db, "SELECT * FROM table_runs")) == 4


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_db(db)
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    todo, skipped = update_history(fake_settings(tmp_path), db, dry_run=True)
    assert (todo, skipped) == (1, 0)
    assert rows_in(db, "SELECT * FROM table_runs") == [], \
        "dry-run must not write any rows"


def test_uninitialized_db_fails_loudly_with_instruction(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    # deliberately NO init_db(): schema was never installed
    write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                   run_id="RUN-1")
    with pytest.raises(RuntimeError, match="init_history_db"):
        update_history(fake_settings(tmp_path), db)


def test_discover_finds_only_dated_dirs_with_runid(tmp_path: Path) -> None:
    settings = fake_settings(tmp_path)
    p1 = write_manifest(tmp_path, date_dir="2026-11-09", folder="oracle",
                        run_id="RUN-1")
    # decoy: dated dir with a malformed (no run_id) manifest
    bad_dir = tmp_path / "runs" / "2026-11-09" / "junk"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "_manifest_bad.json").write_text('{"profile": "laptop"}',
                                                encoding="utf-8")
    # decoy: non-dated directory
    nd = tmp_path / "runs" / "_runs" / "f"
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "_manifest_x.json").write_text('{"run_id": "R", "folder": "f"}',
                                         encoding="utf-8")
    found = discover_manifests(settings["paths"]["runs_root"])
    assert found == [("RUN-1", "oracle", p1)], \
        "discovery must accept only dated-dir manifests carrying run_id"


def test_main_exit_3_on_missing_settings(tmp_path: Path) -> None:
    from dpl.wrapper.update_history import main
    rc = main(["--settings", "config/does_not_exist.yaml",
               "--db", str(tmp_path / "unused.db")])
    assert rc == 3, "config problems must exit 3 (wrapper contract)"


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests: list[tuple[str, Callable[[Path], object]]] = [
        ("ingest_populates_runs_and_table_runs",
         test_ingest_populates_runs_and_table_runs),
        ("table_names_lowercased", test_table_names_lowercased),
        ("reingest_is_idempotent", test_reingest_is_idempotent),
        ("differential_backfill_new_manifest_only",
         test_differential_backfill_new_manifest_only),
        ("dry_run_touches_nothing", test_dry_run_touches_nothing),
        ("uninitialized_db_fails_loudly_with_instruction",
         test_uninitialized_db_fails_loudly_with_instruction),
        ("discover_finds_only_dated_dirs_with_runid",
         test_discover_finds_only_dated_dirs_with_runid),
        ("main_exit_3_on_missing_settings", test_main_exit_3_on_missing_settings),
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for name, fn in tests:
            try:
                fn(Path(td))
                print(f"  PASS {name}")
            except Exception as exc:        # noqa: BLE001 - runner sentinel
                failures.append(name)
                print(f"  FAIL {name}: {exc}")
    total, bad = len(tests), len(failures)
    if bad:
        print(f"RESULT: {total - bad}/{total} passed, FAILED: {failures}")
        return 1
    print(f"RESULT: ALL GREEN ({total}/{total}) - "
          f"stage 3 update_history regression verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
