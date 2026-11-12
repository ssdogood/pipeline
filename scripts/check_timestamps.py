#!/usr/bin/env python
"""Timestamp-conformance verifier for the dpl history DB.

Doctrine (rev2): every DB timestamp field is ISO-8601 UTC with a
literal Z suffix and no fractional seconds - identical to
lib/timefmt.py and the UtcFormatter log plane.

Scans the known timestamp columns and reports every value that does
not match the canonical pattern. READ-ONLY: this tool never writes,
migrates, or repairs. If it ever finds a genuine legacy row, the
repair path gets DESIGNED then (trigger doctrine - no speculative
rewrite machinery). As of 2026-11-12 the production DB is fully
canonical; this script's job is to keep it provably so, on any
machine, any backup, forever.

Column inventory (extend when new tables gain timestamp fields):
  runs.started_at, runs.finished_at,
  email_sends.created_at, email_sends.finished_at
NULL is legal (e.g. finished_at until terminal); empty string is a
fault. Missing tables are reported as skipped, not errors (fresh or
partial DBs are legitimate states).

Usage:
  python scripts/check_timestamps.py --db path/to/history.db

Exit codes (email_acceptance.py convention):
  0  all values conform (missing tables permitted)
  2  one or more non-conforming values found
  3  database unreadable
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dpl.lib.history_db import open_db

CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

TARGETS: tuple[tuple[str, str], ...] = (
    ("runs", "started_at"),
    ("runs", "finished_at"),
    ("email_sends", "created_at"),
    ("email_sends", "finished_at"),
)


def check_conformance(
    conn: sqlite3.Connection,
    targets: tuple[tuple[str, str], ...] = TARGETS,
) -> tuple[list[str], list[tuple[str, str, str, str]]]:
    """Scan targets; return (skipped_tables, faults).

    Fault tuples: (table, column, key, value) where key is the best
    identifying value the table offers (run_id, else send_id, else
    rowid) so an operator can jump straight to the offending row.
    """
    present = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    skipped: list[str] = []
    faults: list[tuple[str, str, str, str]] = []
    for table, column in targets:
        if table not in present:
            skipped.append(f"{table}.{column} (table absent)")
            continue
        cols = {
            r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            skipped.append(f"{table}.{column} (column absent)")
            continue
        key = ("run_id" if "run_id" in cols else
               "send_id" if "send_id" in cols else "rowid")
        for row in conn.execute(
                f"SELECT {key}, {column} FROM {table}"):
            k, val = row
            if val is None:
                continue                      # NULL is legal by schema
            if not CANONICAL.match(str(val)):
                faults.append((table, column, str(k), str(val)))
    return skipped, faults


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/check_timestamps.py",
        description="Verify dpl history DB timestamps are canonical "
                    "UTC-Z (doctrine rev2). Read-only.")
    parser.add_argument("--db", required=True,
                        help="path to the SQLite history DB")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"TIMESTAMP CHECK: DB not found: {path}", file=sys.stderr)
        return 3
    try:
        conn = open_db(path)
    except sqlite3.Error as exc:
        print(f"TIMESTAMP CHECK: DB unreadable: {exc}", file=sys.stderr)
        return 3

    try:
        skipped, faults = check_conformance(conn)
    finally:
        conn.close()

    for s in skipped:
        print(f"skipped: {s}")
    if not faults:
        print(f"TIMESTAMP CHECK PASS: all values canonical UTC-Z "
              f"({len(skipped)} column(s) skipped)")
        return 0

    print(f"TIMESTAMP CHECK FAIL: {len(faults)} non-conforming value(s):")
    for table, column, key, value in faults:
        print(f"  {table}.{column} [{key}]: {value!r}")
    print("No repair is performed - report these rows; the rewrite "
          "path gets designed if/when reality needs it.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
