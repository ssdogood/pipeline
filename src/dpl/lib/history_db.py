# dpl/lib/history_db.py
"""
SQLite history database - the ONLY database-touching module (ARCH-D1).

Doctrine (approved 2026-11-10): the string 'sqlite3' may appear in exactly
ONE source file in the entire pipeline - this one. Every other consumer
obtains connections, DDL, and error types from here, and never imports
sqlite3 itself. Callers elsewhere get exception types via the exported
aliases:

    from dpl.lib.history_db import DbError, IntegrityError

Ownership split:
  - THIS module: connections (WAL + busy_timeout), DDL constants, the
    email_sends write gate, the db-path rules, and the v2 legacy
    migration (PT-001, approved 2026-11-17).
  - wrapper/update_history.py: manifest-ingestion SQL and its CLI -
    but its connections come from open_db() here.

Table creation is NOT done on first open. Schema lifecycle is explicit:
run the install-time script once per machine/deploy:

    python scripts/init_history_db.py [--db PATH]

which applies the idempotent DDL, performs the one-time legacy rename
(pre-v2 test databases: runs -> runs_legacy, table_runs ->
table_runs_legacy, old rows left untouched), and switches the database
to WAL journal mode. Attempting to write to an uninitialized database
fails loudly with that instruction.

Schema v2.1 (PT-014 Commit 2, 2026-11-19):
  - table_runs  : + pause_start, pause_end, pause_reason, days_elapsed
                  (all nullable; NULL for every non-paused row and for
                  re-ingested pre-PT-014 manifests - files stay the
                  record of record either way).
  - folder_runs : + tables_paused (mirrors the envelope count).
  Applied by init_db() as additive ALTER TABLE, in place and lossless -
  unlike the v1 rename, old rows simply keep their NULLs.

Schema v2 (PT-001):
  - runs          : UNCHANGED shape - the wrapper's run-level row
                    (running stub at start, final status/exit_code).
  - folder_runs   : NEW - one row per (run_id, folder), i.e. exactly
                    one manifest file. Summary counts mirror the
                    envelope's top-level tables_* counts; status
                    mirrors the envelope, so a paused folder
                    (PT-014 stub manifest) records 'paused'.
  - table_runs    : + attempts INTEGER (nullable - pre-0.3.0
                    manifests have none). status is free text, so a
                    table-level 'paused' (PT-014) needs no change here.
  - email_sends   : unchanged.

Concurrency: designed assuming concurrent writers (decision log OQ-2).
open_db() sets WAL journal mode and a busy_timeout on EVERY connection,
so interleaved writers queue briefly instead of failing.

Canonical time (FR-20): created_at must be ISO-8601 UTC with 'Z',
enforced at this module's write gate via dpl.lib.timefmt.require_utc_z.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from dpl.lib.common import load_settings
from dpl.lib.logger import get_run_logger
from dpl.lib.timefmt import require_utc_z

log = get_run_logger("dpl.history")

# ---------------------------------------------------------------------------
# Public error aliases - so no other file ever needs 'import sqlite3'
# ---------------------------------------------------------------------------

#: base class of all sqlite failures (use in except clauses)
DbError = sqlite3.Error
#: uniqueness/constraint violations (e.g. duplicate send_id)
IntegrityError = sqlite3.IntegrityError

_BUSY_TIMEOUT_MS = 5000

# Environment overrides for path resolution (documented in module header
# and the user manual): explicit db path beats env var beats settings.
ENV_DB = "DPL_HISTORY_DB"
ENV_SETTINGS = "DPL_SETTINGS"
DEFAULT_SETTINGS_FILE = "config/settings.laptop.yaml"

# ---------------------------------------------------------------------------
# DDL v2 - the single source of truth for the schema.
# Idempotent: CREATE IF NOT EXISTS, safe on fresh and deployed DBs alike.
# The one-time LEGACY RENAME happens in init_db(), NOT here.
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,                -- max across the run's folder manifests
    status      TEXT NOT NULL,       -- success | partial_failure | failed | running
    exit_code   INTEGER              -- wrapper-level
);
CREATE TABLE IF NOT EXISTS folder_runs (
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    folder           TEXT NOT NULL,
    load_date        TEXT,
    status           TEXT NOT NULL,   -- mirrors envelope status (incl. 'paused' from PT-014 stubs)
    tables_total     INTEGER,         -- mirrors envelope top-level counts; exact COUNT fallback
    tables_extracted INTEGER,
    tables_skipped   INTEGER,
    tables_failed    INTEGER,
    tables_paused    INTEGER,        -- v2.1 (PT-014): mirrors envelope count
    exit_code        INTEGER,        -- from envelope wrapper.exit_code (pre-0.3.0: NULL)
    finished_at      TEXT,
    PRIMARY KEY (run_id, folder)
);
CREATE TABLE IF NOT EXISTS table_runs (
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    folder       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    connection   TEXT,
    schema       TEXT,
    status       TEXT NOT NULL,
    rows_read    INTEGER,
    rows_written INTEGER,
    duration_sec REAL,
    output_path  TEXT,
    error        TEXT,
    attempts     INTEGER,
    pause_start  TEXT,               -- v2.1 (PT-014): manifest pause fields
    pause_end    TEXT,               -- (NULL when the table was not paused)
    pause_reason TEXT,
    days_elapsed INTEGER,
    PRIMARY KEY (run_id, folder, table_name)
);
CREATE TABLE IF NOT EXISTS email_sends (
    send_id         TEXT PRIMARY KEY,
    caller          TEXT NOT NULL,
    created_at      TEXT NOT NULL,  -- ISO-8601 UTC with Z (enforced at gate)
    mode            TEXT NOT NULL,  -- live | dry
    outcome         TEXT NOT NULL,  -- sent | failed | skipped_disabled
    error_class     TEXT NOT NULL,  -- none | transient_exhausted | ...
    attempts_used   INTEGER NOT NULL,
    recipient_count INTEGER NOT NULL,
    subject         TEXT,
    error_summary   TEXT,           -- masked; NULL when outcome != failed
    run_id          TEXT,
    finished_at     TEXT            -- NULL until terminal; ISO-8601 UTC with Z
);
"""

#: exact key set accepted by record_email_send - no extras, no omissions
EMAIL_SEND_ROW_KEYS: tuple[str, ...] = (
    "send_id", "caller", "created_at", "mode", "outcome", "error_class",
    "attempts_used", "recipient_count", "subject", "error_summary", "run_id",
    "finished_at",
)

_LEGACY_TABLES = ("runs", "table_runs")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Connections (the ONLY sqlite3.connect calls in the pipeline)
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    """Open the history DB with safe-concurrency pragmas. Caller closes.

    Sets WAL journal mode (readers never block the writer; deploy-persistent
    once set) and busy_timeout on every connection (interleaved writers
    queue briefly instead of raising 'database is locked').

    Does NOT create tables. Schema is applied only by scripts/init_history_db.py
    - a write against a missing table fails with an instructive error.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _migrate_legacy(conn: sqlite3.Connection, existing: set[str]) -> bool:
    """One-time v1 -> v2 rename (PT-001 ruling 2026-11-17).

    Trigger: an existing 'table_runs' WITHOUT the 'attempts' column
    (the exact marker of a v1 schema). Action: rename both v1 tables to
    <name>_legacy and leave every old row untouched; the fresh DDL then
    creates the v2 tables and update_history re-ingests from manifest
    files (files remain the record of record).

    Refuses loudly (RuntimeError, nothing renamed) if any *_legacy name
    is already taken, so a half-migrated database can never silently
    lose data. Returns True if a rename happened.
    """
    if "table_runs" not in existing:
        return False
    if "attempts" in _table_columns(conn, "table_runs"):
        return False                      # already v2: idempotent re-run

    clashes = [f"{t}_legacy" for t in _LEGACY_TABLES
               if t in existing and f"{t}_legacy" in existing]
    if clashes:
        raise RuntimeError(
            f"history migration blocked: {clashes} already exist - "
            f"move or remove them before re-running init")
    for table in _LEGACY_TABLES:
        if table in existing:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
            log.info("history v1 '%s' renamed to '%s' (kept, not migrated)",
                     table, f"{table}_legacy")
    return True


def _migrate_v2_1(conn: sqlite3.Connection) -> bool:
    """Additive v2 -> v2.1 upgrade (PT-014 Commit 2, 2026-11-19).

    Runs in init_db() AFTER the DDL: CREATE IF NOT EXISTS never adds
    columns to an existing table, so a deployed v2 database gets its
    pause columns here. Lossless and in place - plain ALTER TABLE
    ... ADD COLUMN; existing rows keep their NULLs (the contrast with
    the v1 rename is deliberate: that migration preserved rows by
    renaming, this one preserves them by not touching them).

    Marker: 'table_runs' exists but lacks 'pause_reason'. Every added
    column is individually idempotent (checked, skipped when present),
    so a half-upgraded database re-runs to completion and a v2.1
    database is a clean no-op. Returns True when any column was added.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "table_runs" not in tables:
        return False          # fresh DB: the v2.1 DDL already has them
    if "pause_reason" in _table_columns(conn, "table_runs") \
            and "tables_paused" in _table_columns(conn, "folder_runs"):
        return False          # already v2.1: idempotent re-run

    additions: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        ("table_runs", (
            ("pause_start", "TEXT"),
            ("pause_end", "TEXT"),
            ("pause_reason", "TEXT"),
            ("days_elapsed", "INTEGER"),
        )),
        ("folder_runs", (
            ("tables_paused", "INTEGER"),
        )),
    )
    added = False
    for table, cols in additions:
        if table not in tables:
            continue          # nothing to alter (DDL creates v2.1 shape)
        present = _table_columns(conn, table)
        for col, decl in cols:
            if col not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                log.info("history v2.1: %s gained column %s", table, col)
                added = True
    return added


def init_db(db_path: Path) -> dict[str, Any]:
    """Apply the idempotent DDL, run the one-time legacy rename, set WAL.

    Install-time step. Safe on a fresh machine, on a v1 test database
    (renames the old tables aside), and on an already-v2 database (no-op).
    Returns {"tables": [...], "journal_mode": "wal", "migrated": bool}.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        existing = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        migrated = _migrate_legacy(conn, existing)
        conn.executescript(DDL)
        # v2.1 upgrade AFTER the DDL (see _migrate_v2_1 docstring)
        upgraded = _migrate_v2_1(conn)
        conn.commit()
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name")
        ]
        return {"tables": tables, "journal_mode": journal,
                "migrated": migrated, "upgraded_v2_1": upgraded}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB path rules (moved verbatim from wrapper/update_history.py)
# ---------------------------------------------------------------------------

def default_db_path(settings: dict) -> Path:
    """Default: <landing_root>/_history/dpl_history.db."""
    return Path(settings["paths"]["landing_root"]) / "_history" / "dpl_history.db"


def validate_db_path(db_path: Path, settings: dict) -> None:
    """Reject a DB living inside a landing DATA directory."""
    db_path = Path(db_path).resolve()
    landing = Path(settings["paths"]["landing_root"]).resolve()
    try:
        rel = db_path.relative_to(landing)
    except ValueError:
        return                      # outside landing entirely: fine
    if rel.parts and rel.parts[0][:4].isdigit():
        raise ValueError(
            f"history db path must not live inside a landing data "
            f"directory: {db_path} (under {landing / rel.parts[0]})")


def resolve_db_path(db_path: Path | str | None = None,
                    *, settings_file: str | None = None) -> Path:
    """Resolve which DB file to use. Precedence (loud about every step):
    1. explicit db_path argument (tests, wrapper overrides)
    2. $DPL_HISTORY_DB environment variable
    3. settings-derived path, validated.
    """
    if db_path is not None:
        return Path(db_path)
    env_db = os.environ.get(ENV_DB)
    if env_db:
        return Path(env_db)
    settings_file = settings_file or os.environ.get(
        ENV_SETTINGS, DEFAULT_SETTINGS_FILE)
    settings = load_settings(settings_file)
    if settings is None:
        raise ValueError(
            f"cannot resolve history db path: settings file "
            f"{settings_file!r} failed to load; pass db_path=, set "
            f"${ENV_DB}, or fix the settings file")
    path = default_db_path(settings)
    validate_db_path(path, settings)
    return path


# ---------------------------------------------------------------------------
# The email history write gate (FR-18)
# ---------------------------------------------------------------------------

def record_email_send(row: dict, *,
                      db_path: Path | str | None = None) -> None:
    """Persist ONE resolved email send as a single email_sends row.

    Contract: row keys must be EXACTLY EMAIL_SEND_ROW_KEYS; created_at
    (and finished_at, when present) must be canonical UTC-'Z' - enforced
    BEFORE any file is touched; one row per resolved send, plain INSERT -
    a duplicate send_id raises IntegrityError rather than overwriting.
    The EMAIL SERVICE must treat every failure here as non-fatal to the
    send result (FR-17) - that policy lives in the email service, not here.
    """
    missing = [k for k in EMAIL_SEND_ROW_KEYS if k not in row]
    unknown = [k for k in row if k not in EMAIL_SEND_ROW_KEYS]
    if missing or unknown:
        raise ValueError(
            f"email_sends row must contain exactly {EMAIL_SEND_ROW_KEYS}; "
            f"missing: {missing}, unknown: {unknown}")
    require_utc_z(row["created_at"])
    if row["finished_at"] is not None:
        require_utc_z(row["finished_at"])

    path = Path(db_path) if db_path else resolve_db_path()
    conn = open_db(path)
    try:
        conn.execute(
            f"INSERT INTO email_sends ({', '.join(EMAIL_SEND_ROW_KEYS)}) "
            f"VALUES ({', '.join('?' * len(EMAIL_SEND_ROW_KEYS))})",
            tuple(row[k] for k in EMAIL_SEND_ROW_KEYS))
        conn.commit()
        log.debug("email_sends row written: send_id=%s caller=%s "
                  "outcome=%s", row["send_id"], row["caller"],
                  row["outcome"])
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise RuntimeError(
                f"history schema is not installed in {path}; run "
                f"'python scripts/init_history_db.py' once, then retry"
            ) from exc
        raise
    finally:
        conn.close()


__all__ = [
    "DDL",
    "DbError",
    "EMAIL_SEND_ROW_KEYS",
    "IntegrityError",
    "init_db",
    "open_db",
    "default_db_path",
    "validate_db_path",
    "resolve_db_path",
    "record_email_send",
]