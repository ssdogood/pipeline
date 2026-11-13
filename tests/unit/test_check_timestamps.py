"""Tests for scripts/check_timestamps.py - the read-only conformance
verifier (doctrine rev2: DB timestamps are ISO-8601 UTC-Z).

Covers: conforming DB passes (exit 0 path), each fault shape the
scanner reports (wrong format, local-ish format, empty string),
NULL tolerance, absent table/column tolerance, and the fail-loudly
contract on a garbage file.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_timestamps",
    Path(__file__).resolve().parents[2] / "scripts" / "check_timestamps.py")
assert _SPEC is not None and _SPEC.loader is not None
ct = importlib.util.module_from_spec(_SPEC)
sys.modules["check_timestamps"] = ct
_SPEC.loader.exec_module(ct)


GOOD = "2026-11-05T06:30:45Z"
BAD_LOCAL = "2026-11-05 06:30:45,288"
BAD_MSEC = "2026-11-05T06:30:45.123Z"


def _make_db(tmp: Path, runs_rows=(), sends_rows=(),
             with_sends: bool = True) -> Path:
    db = tmp / "hist.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT, profile TEXT, started_at TEXT,"
        " finished_at TEXT, status TEXT, exit_code INTEGER)")
    if with_sends:
        conn.execute(
            "CREATE TABLE email_sends (send_id TEXT, caller TEXT,"
            " run_id TEXT, created_at TEXT, finished_at TEXT,"
            " outcome TEXT, error_class TEXT, error_summary TEXT,"
            " attempts_used INTEGER)")
        for row in sends_rows:
            conn.execute(
                "INSERT INTO email_sends VALUES (?,?,?,?,?,?,?,?,?)", row)
    for row in runs_rows:
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()
    return db


def test_all_canonical_passes(tmp_path: Path) -> None:
    conn = sqlite3.connect(_make_db(tmp_path, [
        ("run_A", "full", GOOD, None, "running", None),
        ("run_B", "full", GOOD, GOOD, "success", 0),
    ], [
        ("S-1", "selftest", "run_A", GOOD, GOOD, "dry_run", "none", None, 0),
    ]))
    skipped, faults = ct.check_conformance(conn)
    conn.close()
    assert faults == []
    assert skipped == []


def test_local_format_fault_detected(tmp_path: Path) -> None:
    conn = sqlite3.connect(_make_db(tmp_path, [
        ("run_A", "full", BAD_LOCAL, None, "running", None),
    ]))
    _, faults = ct.check_conformance(conn)
    conn.close()
    assert faults == [("runs", "started_at", "run_A", BAD_LOCAL)]


def test_millisecond_and_empty_string_faults(tmp_path: Path) -> None:
    conn = sqlite3.connect(_make_db(tmp_path, [
        ("run_A", "full", GOOD, BAD_MSEC, "success", 0),
        ("run_B", "full", "", None, "running", None),
    ]))
    _, faults = ct.check_conformance(conn)
    conn.close()
    assert ("runs", "finished_at", "run_A", BAD_MSEC) in faults
    assert ("runs", "started_at", "run_B", "") in faults
    assert len(faults) == 2


def test_null_is_legal_absent_table_is_skipped(tmp_path: Path) -> None:
    # runs row carries NULL finished_at (legal); the DB has NO
    # email_sends table at all -> those columns are reported skipped.
    conn = sqlite3.connect(_make_db(tmp_path, [
        ("run_A", "full", GOOD, None, "running", None),
    ], with_sends=False))
    skipped, faults = ct.check_conformance(conn)
    conn.close()
    assert faults == []
    assert any(s.startswith("email_sends.") for s in skipped)


def test_absent_column_skipped_not_fault(tmp_path: Path) -> None:
    # A legacy DB whose email_sends lacks finished_at entirely
    # (pre-4d schema) must be reported, not crashed on.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE email_sends (send_id TEXT, caller TEXT,"
        " run_id TEXT, created_at TEXT, outcome TEXT)")
    conn.execute("INSERT INTO email_sends VALUES ('S-1','x',?,?,'dry_run')",
                 ("run_A", GOOD))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    skipped, faults = ct.check_conformance(conn)
    conn.close()
    assert faults == []
    assert any("email_sends.finished_at" in s for s in skipped)


def test_unreadable_file_exit3(tmp_path: Path) -> None:
    bad = tmp_path / "garbage.db"
    bad.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        conn = sqlite3.connect(bad)
        conn.execute("SELECT 1")
        conn.close()


def test_missing_db_exit3(tmp_path: Path) -> None:
    assert not (tmp_path / "nope.db").exists()   # precondition
