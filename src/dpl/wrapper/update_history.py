# wrapper/update_history.py
"""SQLite history for run manifests - the ONLY database-touching module.

Two call paths:
  1. Standalone recovery:  python -m wrapper.update_history [--db ...]
  2. Wrapper final step:   update_history(settings, db_path) (slice 4)

Model (design note rev2 §6):
  - files are the record of record; this DB is the queryable index
  - ingest is idempotent: (run_id, folder, table_name) is the identity;
    INSERT OR REPLACE means re-ingest is always safe
  - the scan window equals manifest retention (default 90 days) - files
    outside the window don't exist, so the filesystem bounds the scan

DB is created with DDL on first open (staging/_history/ auto-mkdir).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from dpl.lib.common import get_run_logger, load_settings

log = get_run_logger("dpl.history")

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
"""


def default_db_path(settings: dict) -> Path:
    """Provisional default: <landing_root>/_history/dpl_history.db.

    Same sibling-of-date-dirs pattern as runs_root. Slice 4 lets
    wrapper.yaml override via db_path_for().
    """
    return Path(settings["paths"]["landing_root"]) / "_history" / "dpl_history.db"


def validate_db_path(db_path: Path, settings: dict) -> None:
    """Reject a DB living inside a landing DATA directory.

    Rule: history must not sit in the data path. The
    check: the resolved db_path must not lie under a date-named
    directory (<landing_root>/<YYYY-MM-DD>/...). landing_root itself
    and non-date siblings (_history/, _runs/) are allowed.
    """
    db_path = db_path.resolve()
    landing = Path(settings["paths"]["landing_root"]).resolve()
    try:
        rel = db_path.relative_to(landing)
    except ValueError:
        return                      # outside landing entirely: fine
    if rel.parts and rel.parts[0][:4].isdigit():
        raise ValueError(
            f"history db path must not live inside a landing data "
            f"directory: {db_path} (under {landing / rel.parts[0]})")


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) and apply DDL. Caller owns the conn."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    return conn


def discover_manifests(runs_root: str | Path, days: int = 90
                       ) -> list[tuple[str, str, Path]]:
    """Walk <runs_root>/<YYYY-MM-DD>/<folder>/_manifest_*.json.

    Returns [(run_id, folder, path)] - only date-named dirs inside the
    window. Identity comes from the manifest's run_id (not the path
    date - a run crossing midnight keeps its original run_id).
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    found: list[tuple[str, str, Path]] = []
    root = Path(runs_root)
    if not root.exists():
        return found
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and d.name[:4].isdigit()):
            continue                       # _runs/, _history/-style dirs exempt
        if d.name < cutoff:
            continue
        for mfile in sorted(d.glob("*" + "/_manifest_*.json")):
            envelope = json.loads(mfile.read_text(encoding="utf-8"))
            run_id = envelope.get("run_id")
            if run_id:                     # skip malformed/no-run_id files
                found.append((run_id, envelope["folder"], mfile))
    return found


def ingest_manifest(conn: sqlite3.Connection, mpath: Path) -> None:
    """Upsert one envelope: 1 runs row + N table_runs rows.

    INSERT OR REPLACE on runs keeps finished_at monotonically current
    across the run's folders (later manifests win); a wrapper-written
    'running' row gets overwritten by the manifest's final state.
    """
    env = json.loads(mpath.read_text(encoding="utf-8"))
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, profile, started_at, finished_at, status, exit_code)
           VALUES (?, ?, ?, ?, ?, NULL)
           ON CONFLICT(run_id) DO UPDATE SET
             finished_at=MAX(runs.finished_at, excluded.finished_at),
             status=CASE WHEN runs.status IN ('running','sqlite_error')
                         THEN excluded.status ELSE runs.status END""",
        (env["run_id"], env["profile"], env["started_at"],
         env["finished_at"], env["status"]))
    for t in env.get("tables", []):
        conn.execute(
            """INSERT OR REPLACE INTO table_runs VALUES
               (?,?,?,?,?,?,?,?,?,?,?)""",
            (env["run_id"], env["folder"], t["name"].lower(),
             t.get("connection"), t.get("schema"), t["status"],
             t.get("rows_read"), t.get("rows_written"),
             t.get("duration_sec"), t.get("output_path"),
             t.get("error")))


def existing_identities(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """(run_id, folder, table_name) pairs already persisted."""
    rows = conn.execute(
        "SELECT run_id, folder, table_name FROM table_runs").fetchall()
    return set(rows)


def update_history(settings: dict, db_path: Path, days: int = 90,
                   dry_run: bool = False) -> tuple[int, int]:
    """Entry point for both CLI and (slice 4) wrapper.

    Differential backfill: ingest every manifest in the window whose
    identity is missing from table_runs. Granularity is per manifest
    FILE ((run_id, folder)), so a partial ingest that died mid-run is
    repaired on the next pass.
    """
    validate_db_path(db_path, settings)
    runs_root = settings["paths"]["runs_root"]
    discovered = discover_manifests(runs_root, days)

    conn = open_db(db_path)
    try:
        known = existing_identities(conn)
        todo, skipped = [], 0
        for run_id, folder, mpath in discovered:
            # peek identity triples without full ingest: cheap pre-pass
            env = json.loads(mpath.read_text(encoding="utf-8"))
            triples = {(run_id, folder, t["name"].lower())
                      for t in env.get("tables", [])}
            if triples <= known:
                skipped += 1
                continue
            todo.append(mpath)

        if dry_run:
            for mpath in todo:
                print(f"[DRY-RUN] would ingest {mpath}")
            print(f"[DRY-RUN] {len(todo)} to ingest, {skipped} up to date")
            return len(todo), skipped

        for mpath in todo:
            ingest_manifest(conn, mpath)
            log.info("ingested %s", mpath)
        conn.commit()
        return len(todo), skipped
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrapper.update_history",
        description="Ingest manifest envelopes into the SQLite history DB")
    parser.add_argument("--db", help="DB path (default: derived from settings)")
    parser.add_argument("--settings", default="config/settings.laptop.yaml")
    parser.add_argument("--days", type=int, default=90,
                        help="backfill window == manifest retention")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    if settings is None:
        return 3                                        # config error

    db_path = Path(args.db) if args.db else default_db_path(settings)
    try:
        ingested, skipped = update_history(settings, db_path,
                                           days=args.days,
                                           dry_run=args.dry_run)
        print(f"history: {ingested} ingested, {skipped} up to date "
              f"-> {db_path}")
        return 0
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        log.error("history update failed: %s", exc)
        print(f"history update failed: {exc}", file=sys.stderr)
        return 2                                        # SQLite/system failure
    # exit codes align with the wrapper contract: 2 = history failure


if __name__ == "__main__":
    raise SystemExit(main())
