# dpl/lib/pause.py
"""PT-014: pause-window evaluation - the single source of pause truth.

Contract (design locked 2026-11-18):
  Declaration (Shape A): presence of a `pause:` block = declared;
  absence = active. The block carries three MANDATORY sub-keys:

      start : YYYY-MM-DD (today or future allowed - stageable)
      end   : YYYY-MM-DD or blank (blank = indefinite)
      reason: non-blank free text

  Two levels: a `tables:` entry and a connection (folder). When both
  exist on the same table, the FOLDER block governs and the table
  block is moot - no conflict error, staging is allowed (but garbage
  in a moot block still fails validation: typos are always loud).

States (live window, evaluated on the run's local date):
      today < start          -> ARMED   (active tonight; arms itself
                                          on the start date)
      start <= today <= end  -> PAUSED  (indefinite when end is blank)
      end < today            -> EXPIRED (active again; tag inert and
                                          nagged until removed)

Loudness doctrine (deliberate - cite this before "fixing"):
  Schema generation skips paused tables SILENTLY on scheduled runs -
  the pause is already announced loudly here (arithmetic lines) and
  in the manifest/history ('paused' status + reason/date fields);
  a third announcement would be noise. Manual override runs
  (--paused-table) get full normal service everywhere.

This module is PURE: dates and dicts in, state and strings out. No
I/O, no logging, no config loading - fully unit-testable (the
PT-034 schema_dispatch pattern).
"""

from __future__ import annotations

from datetime import date, datetime

__func_msg__ = {
    "name": "pause",
    "invocation": "",
    "category": "operations",
    "purpose": (
        "Single source of pause truth:\n"
        "   1. table-level and folder-level pause windows (declared in pipe config),\n"
        "   2. expiry evaluation - expired pauses lift automatically,\n"
        "   3. folder block governs when table and folder overlap"
    ),
}

DATE_FMT = "%Y-%m-%d"

# States
ACTIVE = "active"      # no pause declared (or nothing governs this table)
ARMED = "armed"        # declared, start date in the future
PAUSED = "paused"      # inside the live window
EXPIRED = "expired"    # window has passed; the tag is inert

REQUIRED_KEYS = ("start", "end", "reason")


def _parse_day(value: object, owner: str, key: str) -> date:
    """One YYYY-MM-DD field -> date. Loud, naming the owner.

    PyYAML autotypes bare YYYY-MM-DD scalars to datetime.date; accept
    that honestly-written form too (both quoted and bare).
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value                                  # bare yaml scalar
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{owner}: pause.{key} must be a non-blank 'YYYY-MM-DD' "
            f"string, got {value!r}")
    try:
        return datetime.strptime(value.strip(), DATE_FMT).date()
    except ValueError:
        raise ValueError(
            f"{owner}: pause.{key} {value!r} is not a valid "
            f"'YYYY-MM-DD' date") from None


def validate_pause_block(block: object,
                         owner: str = "<unknown>") -> dict:
    """Loud validation of one pause block; returns the NORMALIZED form:
    {"start": date, "end": date | None, "reason": str}.
    Raises ValueError naming the owner on any violation.
    """
    if not isinstance(block, dict):
        raise ValueError(
            f"{owner}: pause must be a mapping with "
            f"'start', 'end', 'reason' sub-keys")
    missing = [k for k in REQUIRED_KEYS if k not in block]
    if missing:
        raise ValueError(
            f"{owner}: pause block missing required key(s): "
            f"{', '.join(missing)}")

    start = _parse_day(block["start"], owner, "start")

    end_raw = block["end"]
    # blank = indefinite: yaml `end:` (None), empty string, whitespace.
    # EVERYTHING else goes through _parse_day - which accepts both bare
    # yaml dates (autotyped) and quoted strings. The old isinstance-str
    # gate silently turned bare dates into "indefinite" - the PT-014
    # office-test finding (2026-11-19): a dated end must never lie.
    blank = end_raw is None or (isinstance(end_raw, str)
                                and not end_raw.strip())
    if blank:
        end = None
    else:
        end = _parse_day(end_raw, owner, "end")
        if end < start:
            raise ValueError(
                f"{owner}: pause.end {end_raw!r} is before pause.start "
                f"{block['start']!r} - fix the window")

    reason = block["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"{owner}: pause.reason must be non-blank free text")

    return {"start": start, "end": end, "reason": reason.strip()}


def pause_block_ok(block: object) -> bool:
    """Schema-library hook (quiet boolean). The yaml-load schemas call
    this to refuse structurally-broken pause blocks at load; the named
    loud re-validation happens in each extractor's prepare_run.
    """
    try:
        validate_pause_block(block)
    except ValueError:
        return False
    return True


def evaluate(norm: dict, today: date) -> str:
    """Window state for a NORMALIZED block on the given date."""
    if today < norm["start"]:
        return ARMED
    if norm["end"] is None or today <= norm["end"]:
        return PAUSED
    return EXPIRED


def effective_block(table_cfg: dict | None,
                    conn_cfg: dict | None) -> dict | None:
    """Which raw pause block governs this table - or None.
    Folder-level dominates; a table-level block inside a folder-level
    pause is moot.
    """
    if isinstance(conn_cfg, dict) \
            and isinstance(conn_cfg.get("pause"), dict):
        return conn_cfg["pause"]
    if isinstance(table_cfg, dict) \
            and isinstance(table_cfg.get("pause"), dict):
        return table_cfg["pause"]
    return None


def state_for(table_cfg: dict | None, conn_cfg: dict | None,
              today: date | None = None) -> tuple[str, dict | None]:
    """(state, normalized_block | None) for one table tonight.
    Raises if a governing block is malformed - prepare_run has already
    validated, so a raise here is a programming bug surfacing loudly.
    """
    today = today or date.today()
    raw = effective_block(table_cfg, conn_cfg)
    if raw is None:
        return ACTIVE, None
    norm = validate_pause_block(raw)
    return evaluate(norm, today), norm


def describe(state: str, norm: dict,
              today: date | None = None) -> str:
    """The per-run arithmetic log line for a declared pause."""
    today = today or date.today()
    start, end, reason = norm["start"], norm["end"], norm["reason"]

    if state == ARMED:
        days = (start - today).days
        return (f"pause pending: arms in {days} day(s) "
                f"({start.isoformat()}): {reason}")

    if state == PAUSED:
        elapsed = (today - start).days
        if end is None:
            return (f"paused - {elapsed} day(s) elapsed since "
                    f"{start.isoformat()}, no end date: {reason}")
        left = (end - today).days
        return (f"paused - {elapsed} day(s) elapsed since "
                f"{start.isoformat()}, {left} day(s) left until "
                f"{end.isoformat()}: {reason}")

    # EXPIRED - cleanup nag, logged every run until the tag is removed
    late = (today - end).days
    return (f"WARNING: pause expired {late} day(s) ago "
            f"({end.isoformat()}) - remove or extend the tag in "
            f"config: {reason}")


def lifted_by_override(table_cfg: dict | None, conn_cfg: dict | None,
                       override: bool) -> tuple[str, dict | None, bool]:
    """state_for + the --paused-table override applied.

    Returns (state, normalized_block, was_lifted). A lifted TABLE-level
    pause reverts to ACTIVE for this run only; a folder-level pause is
    NEVER liftable from the CLI (operator edits config or waits out
    the window).
    """
    today = date.today()
    raw = effective_block(table_cfg, conn_cfg)
    if raw is None:
        return ACTIVE, None, False
    norm = validate_pause_block(raw)
    state = evaluate(norm, today)
    if override and state == PAUSED \
            and not (isinstance(conn_cfg, dict)
                     and isinstance(conn_cfg.get("pause"), dict)):
        return ACTIVE, norm, True
    return state, norm, False


def manifest_fields(norm: dict, today: date | None = None) -> dict:
    """Per-table manifest/history fields for a paused run (PT-014
    ruling: distinct 'paused' status + reason/date recording).
    """
    today = today or date.today()
    return {
        "pause_start": norm["start"].isoformat(),
        "pause_end": norm["end"].isoformat() if norm["end"] else "",
        "pause_reason": norm["reason"],
        "days_elapsed": (today - norm["start"]).days,
    }


def staleness(norm: dict, today: date | None = None) -> tuple[str | None, int]:
    """Digest-email fuel: (kind, days) a pause has been sitting stale.
      indefinite -> aged from start; expired  -> aged from end.
    Not stale while ARMED (not yet begun) or inside a live window.
    """
    today = today or date.today()
    if evaluate(norm, today) == ARMED:
        return None, 0
    if norm["end"] is None:
        return "indefinite", (today - norm["start"]).days
    if today > norm["end"]:
        return "expired", (today - norm["end"]).days
    return None, 0


def validate_pause_declarations(cfg: dict) -> None:
    """Named, loud re-validation of EVERY declared block in one pipe
    config - extractors call this in prepare_run (defense-in-depth:
    the yaml-load schema already refused structural garbage; this adds
    the owner's name to the message and catches anything that slipped).
    Validating a moot table block is intentional: typos are always loud.
    """
    for cname, conn_cfg in (cfg.get("connections") or {}).items():
        if isinstance(conn_cfg, dict) and conn_cfg.get("pause") is not None:
            validate_pause_block(conn_cfg["pause"],
                                 owner=f"connection '{cname}'")
    for t in cfg.get("tables") or []:
        if isinstance(t, dict) and t.get("pause") is not None:
            validate_pause_block(t["pause"],
                                 owner=f"table '{t['name']}'")