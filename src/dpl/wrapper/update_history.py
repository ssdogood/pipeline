# dpl/wrapper/update_history.py
"""
SQLite history for run manifests - manifest-ingestion specialist.

Two call paths:
  1. Standalone recovery:  python -m dpl.wrapper.update_history [--db ...]
  2. Wrapper final step:   update_history(settings, db_path) (slice 4)

Model (design note rev2 §6):
  - files are the record of record; this DB is the queryable index
  - ingest is idempotent: (run_id, folder, table_name) is the identity;
    INSERT OR REPLACE means re-ingest is always safe
  - the scan window equals manifest retention (default 90 days)

SCHEMA v2 INGEST (PT-001):
  - runs        : one row per run_id (wrapper stub + merged final state)
  - folder_runs : one row PER MANIFEST FILE (run_id, folder) - status,
                  counts, wrapper exit_code. A PT-014 paused folder's
                  stub manifest lands here as status='paused'.
  - table_runs  : + attempts (NULL for pre-0.3.0 manifests)

Count mirroring (ruling 2026-11-17, key placement verified from the
real v0.3.0 manifest): the counts live at the ENVELOPE TOP LEVEL
(tables_total, tables_extracted, tables_skipped, tables_failed and,
since v2.1/PT-014, tables_paused) - NOT in a nested summary block.
Mirror them when ALL PRESENT are expected - see _summary_counts;
otherwise compute exact counts from tables[]. All-or-nothing, so
partial numbers can never land. (v2.1) A tables_paused count is
mirrored only when the DB carries the column AND the envelope has
all five keys; older four-key envelopes fall back to computed
counts, with 'paused' its own bucket at last.

OWNERSHIP SPLIT (stage 3 refactor, 2026-11-10): dpl.lib.history_db is
the ONLY module touching sqlite3. This module KEEPS its ingest SQL and
CLI; connections and pragmas come from open_db() there. Schema is
installed by scripts/init_history_db.py (v2: includes one-time legacy
rename).
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
from dpl.lib.missed_days import missed_days_json

__func_msg__ = {
    "name": "update-history",
    "invocation": "dpl update-history",
    "category": "operations",
    "purpose": (
        "Manifest-ingestion specialist for the run-history database:\n"
        "   1. standalone recovery path (dpl update-history),\n"
        "   2. wrapper final step ingesting every run's manifests,\n"
        "   3. idempotent: (run_id, folder, table_name) is the identity"
    ),
}

log = get_run_logger("dpl.history")

#: table_runs column order used by the INSERT below - single source here
#: (v2.1/PT-014: the four pause fields ride along; NULL whenever the
#: table was not paused or the manifest predates PT-014)
TABLE_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "folder",
    "table_name",
    "connection",
    "schema",
    "status",
    "rows_read",
    "rows_written",
    "duration_sec",
    "output_path",
    "error",
    "attempts",
    "pause_start",
    "pause_end",
    "pause_reason",
    "days_elapsed",
    # v2.2 (PT-032/015): the table's as-of story; NULL whenever the
    # manifest predates B5 (additive - old manifests never fail)
    "table_type",
    "coverage_kind",
    "coverage_from",
    "coverage_to",
)

#: the four original envelope-level count keys mirrored into folder_runs
#: - all-or-nothing exactly as ruled 2026-11-17 (encoded in
#: tests/unit/test_history_v2.py; v0.3.0-era four-key manifests must
#: keep mirroring throughout the 90-day retention window).
_FOUR_COUNT_KEYS: tuple[str, ...] = (
    "tables_total",
    "tables_extracted",
    "tables_skipped",
    "tables_failed",
)
#: v2.1/PT-014 addition: mirrored when the envelope carries it, else
#: computed - an old envelope yields a true 0, never NULL.
_PAUSED_COUNT_KEY = "tables_paused"


def discover_manifests(
    runs_root: str | Path, days: int = 90
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
            continue
        if d.name < cutoff:
            continue
        for mfile in sorted(d.glob("*" + "/_manifest_*.json")):
            envelope = json.loads(mfile.read_text(encoding="utf-8"))
            run_id = envelope.get("run_id")
            if run_id:
                found.append((run_id, envelope["folder"], mfile))
    return found


def _summary_counts(env: dict) -> dict[str, int]:
    """Mirror envelope-level counts; compute what the envelope lacks.

    The original four keys stay all-or-nothing (ruling 2026-11-17,
    encoded in test_history_v2.py): mirror when ALL FOUR are present,
    else compute from tables[]. tables_paused (v2.1) is independent:
    mirrored when the envelope carries it, computed otherwise - an
    old four-key envelope records a true computed 0, never NULL.
    """
    tables = env.get("tables", [])
    computed_paused = sum(1 for t in tables if t.get("status") == "paused")
    # PT-032 (B3): same mirror-compute doctrine as paused - the count
    # rides the envelope when present, else computed from the tables
    # (a legacy envelope yields a true 0, never NULL).
    computed_not_sched = sum(1 for t in tables if t.get("status") == "not_scheduled")

    picked = {k: env[k] for k in _FOUR_COUNT_KEYS if isinstance(env.get(k), int)}
    if len(picked) == len(_FOUR_COUNT_KEYS):
        paused = env.get(_PAUSED_COUNT_KEY)
        return {
            **picked,
            _PAUSED_COUNT_KEY: paused if isinstance(paused, int) else computed_paused,
            "tables_not_scheduled": (
                ns
                if isinstance(ns := env.get("tables_not_scheduled"), int)
                else computed_not_sched
            ),
        }

    return {
        "tables_total": len(tables),
        "tables_extracted": sum(1 for t in tables if t.get("status") == "extracted"),
        "tables_skipped": sum(1 for t in tables if t.get("status") == "skipped"),
        "tables_failed": sum(1 for t in tables if t.get("status") == "failed"),
        "tables_paused": computed_paused,
        "tables_not_scheduled": computed_not_sched,
    }


def ingest_manifest(conn, mpath: Path) -> None:
    """Upsert one envelope: 1 runs row + 1 folder_runs row + N table_runs.

    INSERT OR REPLACE keeps everything idempotent: the same manifest
    file always lands as the same rows. `conn` is untyped on purpose -
    every connection arrives from dpl.lib.history_db.open_db().
    """
    env = json.loads(mpath.read_text(encoding="utf-8"))

    # --- runs: run-level merged state (wrapper stub <-> final state) ---
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, profile, started_at, finished_at, status, exit_code)
           VALUES (?, ?, ?, ?, ?, NULL)
           ON CONFLICT(run_id) DO UPDATE SET
             finished_at=MAX(runs.finished_at, excluded.finished_at),
             status=CASE WHEN runs.status IN ('running','sqlite_error')
                         THEN excluded.status ELSE runs.status END""",
        (
            env["run_id"],
            env["profile"],
            env["started_at"],
            env["finished_at"],
            env["status"],
        ),
    )

    # --- folder_runs: THE manifest-file row (PT-001) ---
    wrapper = env.get("wrapper")
    wrapper_exit = wrapper.get("exit_code") if isinstance(wrapper, dict) else None
    counts = _summary_counts(env)
    conn.execute(
        """INSERT OR REPLACE INTO folder_runs
        (run_id, folder, load_date, status,
        tables_total, tables_extracted, tables_skipped, tables_failed,
        tables_paused, tables_not_scheduled, as_of, anchor_date,
        missed_days, exit_code, finished_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            env["run_id"],
            env["folder"],
            env.get("load_date"),
            env.get("status", "unknown"),
            counts["tables_total"],
            counts["tables_extracted"],
            counts["tables_skipped"],
            counts["tables_failed"],
            counts["tables_paused"],
            counts["tables_not_scheduled"],
            env.get("as_of"),
            env.get("anchor_date"),
            missed_days_json(env),
            wrapper_exit,
            env.get("finished_at"),
        ),
    )

    # --- table_runs: explicit columns (no positional-VALUES trap) ---
    placeholders = ", ".join("?" * len(TABLE_RUN_COLUMNS))
    columns = ", ".join(TABLE_RUN_COLUMNS)
    for t in env.get("tables", []):
        conn.execute(
            f"INSERT OR REPLACE INTO table_runs ({columns}) VALUES ({placeholders})",
            (
                env["run_id"],
                env["folder"],
                t["name"].lower(),
                t.get("connection"),
                t.get("schema"),
                t["status"],
                t.get("rows_read"),
                t.get("rows_written"),
                t.get("duration_sec"),
                t.get("output_path"),
                t.get("error"),
                t.get("attempts"),
                t.get("pause_start"),
                t.get("pause_end"),
                t.get("pause_reason"),
                t.get("days_elapsed"),
                t.get("type"),
                t.get("coverage_kind"),
                t.get("coverage_from"),
                t.get("coverage_to"),
            ),
        )


def existing_identities(conn) -> set[tuple[str, str, str]]:
    """(run_id, folder, table_name) pairs already persisted."""
    rows = conn.execute("SELECT run_id, folder, table_name FROM table_runs").fetchall()
    return set(rows)


def update_history(
    settings: dict, db_path: Path, days: int = 90, dry_run: bool = False
) -> tuple[int, int]:
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
        try:
            known = existing_identities(conn)
        except DbError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    f"history schema is not installed in {db_path}; run "
                    f"'python scripts/init_history_db.py' once, then "
                    f"retry"
                ) from exc
            raise
        todo, skipped = [], 0
        for run_id, folder, mpath in discovered:
            env = json.loads(mpath.read_text(encoding="utf-8"))
            triples = {
                (run_id, folder, t["name"].lower()) for t in env.get("tables", [])
            }
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
        description="Ingest manifest envelopes into the SQLite history DB",
    )
    parser.add_argument("--db", help="DB path (default: derived from settings)")
    parser.add_argument(
        "--settings",
        default=None,
        help="path to settings yaml (default: <environment folder>/settings.yaml)",
    )
    parser.add_argument(
        "--days", type=int, default=90, help="backfill window == manifest retention"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from dpl.lib.environment import resolve_environment

    settings_path = args.settings or str(resolve_environment().folder / "settings.yaml")
    settings = load_settings(settings_path)
    if settings is None:
        return 3  # config error

    db_path = Path(args.db) if args.db else default_db_path(settings)
    try:
        ingested, skipped = update_history(
            settings, db_path, days=args.days, dry_run=args.dry_run
        )
        print(f"history: {ingested} ingested, {skipped} up to date -> {db_path}")
        return 0
    except (ValueError, DbError, RuntimeError, OSError) as exc:
        log.error("history update failed: %s", exc)
        print(f"history update failed: {exc}", file=sys.stderr)
        return 2

    # exit codes align with the wrapper contract: 2 = history failure


if __name__ == "__main__":
    raise SystemExit(main())
