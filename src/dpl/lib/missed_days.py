# dpl/lib/missed_days.py
"""PT-032 B7: missed-scheduled-day detection - pure core, thin history read.

For every connection WITH a schedule block, compares the days that
WERE due since its last successful extraction against the days that
actually ran, and reports the difference:

  - a due day with no successful run recorded on/covering it is MISSED;
  - a miss is never repaired by --ignore-schedule (the override does
    the extraction; the detection still reports honestly).

The sweep core is dates-and-dicts pure (no I/O, no logging, no email
calls). Two thin edges live here too: a READ-ONLY history query that
never raises (missing DB/table = first-run grace, empty baseline),
and the one-email payload aggregator. The wrapper owns firing the
email (pause-stale-digest pattern: one event, one caller, never-raise).

Lookback cap: MAX_LOOKBACK_DAYS (design constant). A first-ever run,
or a gap older than the cap, is reported as truncated-from rather
than scanning forever. Misses within the window are enumerated; the
cap exists so a long-dead connection cannot flood a manifest.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from dpl.lib.schedule import is_due_connection

__func_msg__ = {
    "name": "missed days",
    "invocation": "",
    "category": "operations",
    "purpose": (
        "Missed-scheduled-day detection:\n"
        "   1. for each scheduled connection, walks the due days since\n"
        "   2. its last successful extraction (from the history DB), and\n"
        "   3. reports every due day that ran nowhere, capped by lookback"
    ),
}

#: design constant - maximum days walked back from today (design 3.4)
MAX_LOOKBACK_DAYS = 30


def last_success_by_folder(rows: list[tuple[str, str | None]]) -> dict[str, date]:
    """Fold (folder, load_date) rows into {folder: latest date}.

    Rows with NULL load_date are ignored (a row without a date says
    nothing about when data last landed). Duplicate folders keep the
    maximum date - the fold is idempotent for any row order.
    """
    out: dict[str, date] = {}
    for folder, d in rows:
        if d is None:
            continue
        dt = date.fromisoformat(d)
        if folder not in out or dt > out[folder]:
            out[folder] = dt
    return out


def is_due_connection_schedule(schedule_block: object, day: date) -> bool:
    """Due-ness of one date against one schedule block (pure shim).

    Wraps the raw block the same way the extractors hand a connection
    config to is_due_connection; separate public name so callers are
    explicit about operating on a bare block.
    """
    if not isinstance(schedule_block, dict):
        return True  # absent block = due every day
    sched: dict[str, object] = dict(cast(dict[str, object], schedule_block))
    return is_due_connection({"schedule": sched}, day)


def missed_days_for_connection(
    schedule_block: object,
    last_success: date | None,
    today: date,
) -> dict[str, object] | None:
    """Detect missed due-days for ONE connection since its last success.

    Returns None when nothing is missed; otherwise an entry shaped
    for the manifest and template:
      {"missed": [iso dates], "from": iso date, "truncated": bool}

    - last_success None (never ran / unknown) -> NOT a miss report:
      detection has no baseline, so it reports nothing (the same
      first-run grace as the pause digest); the manifest keys stay
      at their empty defaults.
    - the sweep starts the day AFTER last_success and ends TODAY
      (exclusive): the last success covers its own date, and today
      belongs to the run at hand.
    - due days with a PAUSE covering them are the operator's own act;
      the manifest status vocabulary already records pauses per run,
      and pause-covered days are not swept for here (design keeps
      detection and pause composition orthogonal).
    """
    if last_success is None:
        return None
    earliest = max(
        last_success + timedelta(days=1),
        today - timedelta(days=MAX_LOOKBACK_DAYS),
    )
    missed: list[str] = []
    day = earliest
    while day < today:
        if is_due_connection_schedule(schedule_block, day):
            missed.append(day.isoformat())
        day += timedelta(days=1)
    if not missed:
        return None
    truncated = earliest > last_success + timedelta(days=1)
    return {
        "missed": missed,
        "from": earliest.isoformat(),
        "truncated": truncated,
    }


def fetch_last_success(db_path: Path) -> dict[str, date]:
    """Thin READ-ONLY history query: {folder: last SUCCESSFUL load_date}.

    One bounded scan (folder_runs is one row per manifest per day, so
    the retention window caps it). NEVER raises: a missing DB, a
    missing table (fresh machine, init not yet run) or a locked file
    means detection has no baseline - the first-run grace applies and
    the run proceeds. Extraction must never depend on history being
    present; only the miss REPORT does.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        raw = conn.execute(
            "SELECT folder, load_date FROM folder_runs WHERE status = 'success'"
        ).fetchall()
    except sqlite3.Error:
        return {}  # no DB / no table yet: first-run grace
    finally:
        conn.close()
    rows: list[tuple[str, str | None]] = []
    for r in raw:
        loaded = r[1] if isinstance(r[1], str) else None
        rows.append((str(r[0]), loaded))
    return last_success_by_folder(rows)


def missed_day_payload(
    envelopes: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    """Aggregate every envelope's miss into ONE email payload - or None.

    None means "nothing missed anywhere": the wrapper sends no mail
    (pause-digest doctrine - no empty-mail noise). One line per
    folder, pre-rendered; run_id is the wrapper's to add. Override
    runs report here exactly like scheduled ones - the override
    extracts, it never silences (design 3.4).
    """
    count = 0
    lines: list[str] = []
    for folder in sorted(envelopes):
        env = envelopes[folder]
        missed_raw: object = env.get("missed_days")
        if not isinstance(missed_raw, list) or not missed_raw:
            continue
        missed: list[object] = cast(list[object], missed_raw)
        count += len(missed)
        note = ""
        if env.get("missed_days_truncated"):
            note = f" (report truncated from {env.get('missed_days_from')})"
        lines.append(
            f"{folder}: {len(missed)} missed scheduled day(s): "
            f"{', '.join(str(m) for m in missed)}{note}"
        )
    if not lines:
        return None
    return {
        "missed_count": count,
        "missed_folders": str(len(lines)),
        "missed_lines": "\n".join(lines),
    }


def missed_days_json(env: dict[str, object]) -> str | None:
    """History-ingest helper: envelope keys -> one JSON column value."""
    missed = env.get("missed_days")
    if not isinstance(missed, list) or not missed:
        return None
    return json.dumps(
        {
            "days": missed,
            "from": env.get("missed_days_from"),
            "truncated": bool(env.get("missed_days_truncated")),
        }
    )
