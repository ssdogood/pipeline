# dpl/wrapper/update_history.py
"""SQLite history for run manifests - manifest-ingestion specialist.

Two call paths:
  1. Standalone recovery:  python -m dpl.wrapper.update_history [--db ...]
  2. Wrapper final step:   update_history(settings, db_path) (slice 4)

Model (design note rev2 §6):
  - files are the record of record; this DB is the queryable index
  - ingest is idempotent: (run_id, folder, table_name) is the identity;
    INSERT OR REPLACE means re-ingest is always safe
  - the scan window equals manifest retention (default 90 days) - files
    outside the window don't exist, so the filesystem bounds the scan

OWNERSHIP SPLIT (stage 3 refactor, 2026-11-10):
  Since stage 2, dpl.lib.history_db is the ONLY module in the pipeline
  that touches sqlite3. This module KEEPS its manifest-ingestion SQL,
  its CLI, and its exit codes, but obtains everything else from there:

      connections, WAL/busy_timeout pragmas, db-path rules
          -> dpl.lib.history_db (open_db, default_db_path, validate_db_path)

  SCHEMA CHANGE OF BEHAVIOR: this module no longer creates tables on
  first open. The schema is installed explicitly, once per machine:

      python scripts/init_history_db.py [--db PATH]

  (idempotent; also upgrades a deployed DB in place). Ingesting into an
  uninitialized database fails loudly with that instruction.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from dpl.lib.common import get_run_logger, load_settings
from dpl.lib.history_db import (
    DbError,
    default_db_path,
    open_db,
    validate_db_path,
)

log = get_run_logger("dpl.history")


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


def ingest_manifest(conn, mpath: Path) -> None:
    """Upsert one envelope: 1 runs row + N table_runs rows.

    INSERT OR REPLACE on runs keeps finished_at monotonically current
    across the run's folders (later manifests win); a wrapper-written
    'running' row gets overwritten by the manifest's final state.

    NOTE: `conn` is untyped on purpose - this module must not name
    sqlite3 types; every connection it uses arrives from
    dpl.lib.history_db.open_db().
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


def existing_identities(conn) -> set[tuple[str, str, str]]:
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

    conn = open_db(db_path)      # from history_db; schema NOT created here
    try:
        try:
            known = existing_identities(conn)
        except DbError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    f"history schema is not installed in {db_path}; run "
                    f"'python scripts/init_history_db.py' once, then "
                    f"retry") from exc
            raise
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
        prog="python -m dpl.wrapper.update_history",
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
    except (ValueError, DbError, RuntimeError, OSError) as exc:
        log.error("history update failed: %s", exc)
        print(f"history update failed: {exc}", file=sys.stderr)
        return 2                                        # SQLite/system failure
    # exit codes align with the wrapper contract: 2 = history failure


if __name__ == "__main__":
    raise SystemExit(main())
