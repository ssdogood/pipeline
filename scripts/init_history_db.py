"""Install-time schema initialization for the dpl history database.

One-time-per-machine (and safe to re-run anytime) schema step, per the
approved architecture design (FR-18): table creation does NOT happen on
first open; it happens here, idempotently:

    python scripts/init_history_db.py [--db PATH] [--settings FILE]

  - Fresh machine: creates runs, table_runs, email_sends + WAL mode.
  - Deployed database: adds any missing tables (email_sends) without
    touching existing rows, and switches to WAL.

Exit codes align with the wrapper contract:
  0 = ok | 2 = db/system failure | 3 = config error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dpl.lib.history_db import DbError, init_db, resolve_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/init_history_db.py",
        description="Create/upgrade the dpl history DB schema "
                    "(idempotent; safe on fresh and deployed databases)")
    parser.add_argument(
        "--db", help="DB path (default: settings-derived; "
                     "$DPL_HISTORY_DB overrides)")
    parser.add_argument(
        "--settings", default=None,
        help="settings file for deriving the DB path "
             "(default: $DPL_SETTINGS or the conventional default)")
    args = parser.parse_args(argv)

    try:
        db_path = (Path(args.db) if args.db
                   else resolve_db_path(settings_file=args.settings))
        summary = init_db(db_path)
    except ValueError as exc:                       # settings/config problem
        print(f"history init: config error: {exc}", file=sys.stderr)
        return 3
    except (DbError, OSError) as exc:              # storage problem
        print(f"history init failed: {exc}", file=sys.stderr)
        return 2

    print(f"history db ready: {db_path}")
    print(f"  journal mode: {summary['journal_mode']}")
    print(f"  tables ensured: {', '.join(summary['tables'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
