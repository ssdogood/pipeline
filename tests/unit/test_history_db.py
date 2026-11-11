"""Unit tests for dpl/lib/history_db.py (stage 2).

Runs under the project's pytest suite AND standalone:
    python tests/unit/test_history_db.py

Needs NO network, NO vault, NO real landing root - every test works on a
throwaway temporary database via tmp_path / tempfile.

Doctrine note: these tests deliberately NEVER import sqlite3 - all
interaction goes through the history_db public API, proving the
sole-connector doctrine is livable. Table inspection uses the
connection object returned by open_db().

The CLI script is loaded BY FILE LOCATION (importlib), not by module
path: the project's import root is src/, so 'scripts.*' is not a
resolvable package to static analysis (pyrefly) even though runtime
imports from the project root happen to work. Loading by path is the
honest, robust way to test a script.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from dpl.lib.history_db import (
    EMAIL_SEND_ROW_KEYS,
    IntegrityError,
    default_db_path,
    init_db,
    open_db,
    record_email_send,
    resolve_db_path,
    validate_db_path,
)
from dpl.lib.timefmt import now_utc_z

# ----------------------------------------------------------------- helpers --

def sample_row(send_id: str, **overrides: object) -> dict:
    """A valid, fully-populated email_sends row."""
    row: dict = {
        "send_id": send_id,
        "caller": "test_caller",
        "created_at": now_utc_z(),
        "mode": "live",
        "outcome": "sent",
        "error_class": "none",
        "attempts_used": 1,
        "recipient_count": 2,
        "subject": "[dpl] test",
        "error_summary": None,
        "run_id": "RUN-TEST",
        "finished_at": None,
    }
    row.update(overrides)
    return row


def count_rows(db: Path) -> int:
    conn = open_db(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM email_sends").fetchone()[0]
    finally:
        conn.close()


def table_names(db: Path) -> list[str]:
    conn = open_db(db)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


def init_once(db: Path) -> None:
    init_db(db)


def load_cli_main() -> Callable[..., int]:
    """Load scripts/init_history_db.py by file location, return its main()."""
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "init_history_db.py"
    spec = importlib.util.spec_from_file_location("init_history_db_cli",
                                                  script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


@contextmanager
def _env(key: str, value: str):
    """Temporarily set an env var; restore on exit (standalone-runner-safe)."""
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def fake_settings(landing_root: Path) -> dict:
    """Minimal settings dict shaped like the real one (paths.landing_root)."""
    return {"paths": {"landing_root": str(landing_root)}}


# ------------------------------------------------------------- init / pragmas


def test_init_creates_all_three_tables(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    assert table_names(db) == ["email_sends", "runs", "table_runs"]


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    init_once(db)                       # re-run must be a no-op, not an error
    assert table_names(db) == ["email_sends", "runs", "table_runs"]


def test_open_db_sets_wal_and_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    conn = open_db(db)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert journal == "wal", "WAL must be active on every connection"
    assert busy == 5000, "busy_timeout must be set on every connection"


# ------------------------------------------------------- record_email_send --


def test_record_and_read_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    row = sample_row("S-001")
    record_email_send(row, db_path=db)
    conn = open_db(db)
    try:
        got = conn.execute(
            "SELECT * FROM email_sends WHERE send_id = 'S-001'"
        ).fetchone()
    finally:
        conn.close()
    assert got is not None, "row must exist after record_email_send"
    assert dict(zip(EMAIL_SEND_ROW_KEYS, got)) == row

def test_record_rejects_malformed_finished_at(tmp_path: Path) -> None:
    """finished_at provenance gate (ratified 2026-11-11): when present,
    it must be canonical UTC-Z - same doctrine as created_at."""
    db = tmp_path / "hist.db"
    init_once(db)
    row = sample_row("S-BADFIN", finished_at="2026-11-11 13:00:00")
    with pytest.raises(ValueError, match="canonical"):
        record_email_send(row, db_path=db)


def test_stamped_finished_at_roundtrip(tmp_path: Path) -> None:
    """A stamped finished_at survives write + read unchanged."""
    db = tmp_path / "hist.db"
    init_once(db)
    fin = now_utc_z()
    record_email_send(sample_row("S-FIN", finished_at=fin), db_path=db)
    conn = open_db(db)
    try:
        got = conn.execute(
            "SELECT finished_at FROM email_sends WHERE send_id = ?",
            ("S-FIN",)).fetchone()[0]
    finally:
        conn.close()
    assert got == fin

def test_record_rejects_non_utc_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    with pytest.raises(ValueError, match="canonical"):
        record_email_send(
            sample_row("S-002", created_at="2026-11-10T14:22:33"),
            db_path=db)
    assert count_rows(db) == 0, "rejected row must not be written"


def test_record_rejects_missing_and_unknown_keys(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    missing_key = sample_row("S-003")
    del missing_key["subject"]
    with pytest.raises(ValueError, match="missing"):
        record_email_send(missing_key, db_path=db)
    with pytest.raises(ValueError, match="unknown"):
        record_email_send(
            sample_row("S-004", surprise_key=1), db_path=db)
    assert count_rows(db) == 0


def test_record_rejects_duplicate_send_id(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    record_email_send(sample_row("S-DUP"), db_path=db)
    with pytest.raises(IntegrityError):
        record_email_send(sample_row("S-DUP"), db_path=db)
    assert count_rows(db) == 1, "duplicate must NOT overwrite history"


def test_uninitialized_db_fails_loudly_with_instruction(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    # NOTE: no init_db call - schema was never installed
    with pytest.raises(RuntimeError, match="init_history_db"):
        record_email_send(sample_row("S-005"), db_path=db)


# ------------------------------------------------------ db path resolution --


def test_resolve_explicit_path_beats_env_var(tmp_path: Path) -> None:
    with _env("DPL_HISTORY_DB", str(tmp_path / "from_env.db")):
        explicit = tmp_path / "explicit.db"
        assert resolve_db_path(explicit) == explicit, \
            "explicit argument must have top precedence"


def test_resolve_env_var_when_no_argument(tmp_path: Path) -> None:
    env_db = tmp_path / "from_env.db"
    with _env("DPL_HISTORY_DB", str(env_db)):
        assert resolve_db_path() == env_db


def test_default_path_is_history_sibling_of_landing(tmp_path: Path) -> None:
    landing = tmp_path / "landing"
    settings = fake_settings(landing)
    expected = landing / "_history" / "dpl_history.db"
    assert default_db_path(settings) == expected


def test_validate_rejects_db_inside_date_dir(tmp_path: Path) -> None:
    landing = tmp_path / "landing"
    settings = fake_settings(landing)
    inside = landing / "2026-11-10" / "x" / "hist.db"
    with pytest.raises(ValueError, match="landing data"):
        validate_db_path(inside, settings)


def test_validate_allows_history_sibling(tmp_path: Path) -> None:
    landing = tmp_path / "landing"
    settings = fake_settings(landing)
    sibling = landing / "_history" / "dpl_history.db"
    validate_db_path(sibling, settings)          # must NOT raise


def test_validate_allows_outside_landing(tmp_path: Path) -> None:
    settings = fake_settings(tmp_path / "landing")
    outside = tmp_path / "elsewhere" / "hist.db"
    validate_db_path(outside, settings)           # must NOT raise


# -------------------------------------------------------------- concurrency --


def test_concurrent_writers_all_land(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    init_once(db)
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            for i in range(5):
                record_email_send(
                    sample_row(f"S-W{worker_id}-{i}"), db_path=db)
        except Exception as exc:            # noqa: BLE001 - test sentinel
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent writes failed under WAL/busy_timeout: {errors}"
    assert count_rows(db) == 20, "every interleaved write must land exactly once"


# --------------------------------------------------------------- CLI script --


def test_init_cli_creates_schema(tmp_path: Path) -> None:
    cli_main = load_cli_main()
    db = tmp_path / "cli_hist.db"
    rc = cli_main(["--db", str(db)])
    assert rc == 0, "install script must exit 0 on success"
    assert table_names(db) == ["email_sends", "runs", "table_runs"]


def test_init_cli_exit_codes(tmp_path: Path) -> None:
    cli_main = load_cli_main()
    # config error: nonexistent settings file, no --db override
    rc = cli_main(["--settings", "config/does_not_exist.yaml"])
    assert rc == 3, "config problems must exit 3 (wrapper contract)"


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests: list[tuple[str, Callable[..., object]]] = [
        ("init_creates_all_three_tables", test_init_creates_all_three_tables),
        ("init_is_idempotent", test_init_is_idempotent),
        ("open_db_sets_wal_and_busy_timeout",
         test_open_db_sets_wal_and_busy_timeout),
        ("record_and_read_roundtrip", test_record_and_read_roundtrip),
        ("record_rejects_non_utc_timestamp",
         test_record_rejects_non_utc_timestamp),
        ("record_rejects_missing_and_unknown_keys",
         test_record_rejects_missing_and_unknown_keys),
        ("record_rejects_duplicate_send_id",
         test_record_rejects_duplicate_send_id),
        ("uninitialized_db_fails_loudly_with_instruction",
         test_uninitialized_db_fails_loudly_with_instruction),
        ("resolve_explicit_path_beats_env_var",
         test_resolve_explicit_path_beats_env_var),
        ("resolve_env_var_when_no_argument",
         test_resolve_env_var_when_no_argument),
        ("default_path_is_history_sibling_of_landing",
         test_default_path_is_history_sibling_of_landing),
        ("validate_rejects_db_inside_date_dir",
         test_validate_rejects_db_inside_date_dir),
        ("validate_allows_history_sibling", test_validate_allows_history_sibling),
        ("validate_allows_outside_landing", test_validate_allows_outside_landing),
        ("concurrent_writers_all_land", test_concurrent_writers_all_land),
        ("init_cli_creates_schema", test_init_cli_creates_schema),
        ("init_cli_exit_codes", test_init_cli_exit_codes),
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
    print(f"RESULT: ALL GREEN ({total}/{total}) - stage 2 history_db verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
