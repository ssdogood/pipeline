# dpl/lib/history_db.py
"""
SQLite history database - the ONLY database-touching module (ARCH-D1).

Doctrine (approved 2026-11-10): the string 'sqlite3' may appear in exactly
ONE source file in the entire pipeline - this one. Every other consumer
obtains connections, DDL, and error types from here, and never imports
sqlite3 itself. Enforceable by a one-line grep (see green script, later
stage). Callers elsewhere get exception types via the exported aliases:

    from dpl.lib.history_db import DbError, IntegrityError

Ownership split:
  - THIS module: connections (WAL + busy_timeout), DDL constants, the
    email_sends write gate, and the db-path rules.
  - wrapper/update_history.py (stage 3 refactor): manifest-ingestion
    SQL and its CLI - but its connections come from open_db() here.

Table creation is NOT done on first open. Schema lifecycle is explicit:
run the install-time script once per machine/deploy:

    python scripts/init_history_db.py [--db PATH]

which applies the idempotent DDL (runs, table_runs, email_sends) and
switches the database to WAL journal mode. Attempting to write to an
uninitialized database fails loudly with that instruction.

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
# DDL - the single source of truth for the schema.
# runs / table_runs moved VERBATIM from wrapper/update_history.py (stage 3
# deletes them there). email_sends per architecture design section 6.1.
# Idempotent: CREATE IF NOT EXISTS, safe on fresh and deployed DBs alike.
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,               -- max across the run's folder manifests
    status      TEXT NOT NULL,      -- success | partial_failure | failed | running
    exit_code   INTEGER              -- wrapper-level; NULL until slice 4
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


def init_db(db_path: Path) -> dict[str, Any]:
    """Apply the idempotent DDL and switch the DB to WAL. Install-time step.

    Safe both on a fresh machine and on a deployed database (adds
    email_sends without touching existing rows). Returns a summary dict:
    {"tables": [...], "journal_mode": "wal"}.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)
        conn.commit()
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name")
        ]
        return {"tables": tables, "journal_mode": journal}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB path rules (moved verbatim from wrapper/update_history.py)
# ---------------------------------------------------------------------------

def default_db_path(settings: dict) -> Path:
    """Default: <landing_root>/_history/dpl_history.db.

    Same sibling-of-date-dirs pattern as runs_root. Slice 4 will let
    wrapper.yaml override via db_path_for().
    """
    return Path(settings["paths"]["landing_root"]) / "_history" / "dpl_history.db"


def validate_db_path(db_path: Path, settings: dict) -> None:
    """Reject a DB living inside a landing DATA directory.

    Rule: history must not sit in the data path. The check: the resolved
    db_path must not lie under a date-named directory
    (<landing_root>/<YYYY-MM-DD>/...). landing_root itself and non-date
    siblings (_history/, _runs/) are allowed.
    """
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
    3. settings-derived: <landing_root>/_history/dpl_history.db,
       where settings come from $DPL_SETTINGS or the conventional
       default settings file, and validate_db_path is enforced.

    Raises instructive ValueError when settings cannot be loaded, so the
    fix is obvious from the message alone.
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

    Contract:
      - row keys must be EXACTLY EMAIL_SEND_ROW_KEYS (extra or missing
        keys raise loudly - schema drift cannot slip in silently);
      - row['created_at'] must be canonical UTC-'Z' (FR-20) - enforced
        here via require_utc_z, BEFORE any file is touched;
      - one row per resolved send, plain INSERT: a duplicate send_id
        raises IntegrityError rather than overwriting history.

    Raises (caller-facing, all importable without sqlite3):
      ValueError        - bad row keys or non-canonical timestamp
      IntegrityError    - duplicate send_id
      RuntimeError      - db exists but schema was never installed
                          (message points at scripts/init_history_db.py)
      DbError / OSError - genuine storage failures

    The EMAIL SERVICE must treat every one of these as non-fatal to the
    send result (failure isolation, FR-17): an email that WAS sent stays
    'sent' even if this row write fails. That policy lives in the email
    service, not here - this module reports loudly and honestly.
    """
    missing = [k for k in EMAIL_SEND_ROW_KEYS if k not in row]
    unknown = [k for k in row if k not in EMAIL_SEND_ROW_KEYS]
    if missing or unknown:
        raise ValueError(
            f"email_sends row must contain exactly {EMAIL_SEND_ROW_KEYS}; "
            f"missing: {missing}, unknown: {unknown}")
    require_utc_z(row["created_at"])  
    if row["finished_at"] is not None:
        require_utc_z(row["finished_at"])    # loud gate BEFORE touching disk

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
