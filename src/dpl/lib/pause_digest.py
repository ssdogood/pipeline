# dpl/lib/pause_digest.py
"""PT-014 Commit 2: stale-pause digest scanning - pure, like pause.py.

Collects every declared pause in one pipe-config snapshot and marks
those sitting stale past the configured threshold:

  - "indefinite" (blank end): aged from its start date
  - "expired" (lingering tag): aged from its end date
ARMED pauses and live windows are never stale (pause.staleness ruling).

Threshold semantics: a pause is stale when its age (days) is
>= stale_days. The knob lives at settings pause.stale_days (default
14, validated in lib.common.SETTINGS_SCHEMA; absent block -> default).

Pure module: dates and dicts in, entries and strings out. No I/O, no
logging, no email calls - the wrapper owns firing the digest (via
email_service, FR-17 isolation); this module only decides WHETHER
there is anything to say, and pre-renders the exact lines.
"""
from __future__ import annotations

from datetime import date

from dpl.lib.pause import (
    staleness,
    validate_pause_block,
)

#: inclusive staleness threshold default - mirrors the settings schema
DEFAULT_STALE_DAYS = 14


def stale_pause_entries(pipe_cfgs: dict[str, dict],
                        stale_days: int,
                        today: date | None = None) -> list[dict]:
    """Scan every pipe config; return one entry per stale pause.

    Entry shape (all scalars - template-ready):
      {"pipe": str, "owner": str, "kind": str, "days": int,
       "reason": str, "line": str}

    `owner` names the declaring site the way pause validation does
    (connection 'name' / table 'name'); `line` is the pre-rendered
    digest line, ready to join with newlines into one template value.
    Config errors raise loudly (typos are always loud) - by digest
    time the run itself has already validated every block, so a raise
    here means the yaml changed mid-run, and that must be visible.
    """
    today = today or date.today()
    entries: list[dict] = []
    for pipe_name, cfg in pipe_cfgs.items():
        conns = cfg.get("connections") or {}
        for cname, conn_cfg in conns.items():
            if isinstance(conn_cfg, dict) \
                    and conn_cfg.get("pause") is not None:
                _check(conn_cfg["pause"], entries, pipe_name,
                       f"connection '{cname}'", stale_days, today)
        for t in cfg.get("tables") or []:
            if isinstance(t, dict) and t.get("pause") is not None:
                _check(t["pause"], entries, pipe_name,
                       f"table '{t['name']}'", stale_days, today)
    return entries


def _check(block: object, entries: list[dict], pipe: str,
           owner: str, stale_days: int, today: date) -> None:
    named = f"pipe '{pipe}', {owner}"
    norm = validate_pause_block(block, owner=named)
    kind, days = staleness(norm, today)
    if kind is None or days < stale_days:
        return
    age_from = norm["start"] if kind == "indefinite" else norm["end"]
    assert age_from is not None          # kind implies the anchor exists
    entries.append({
        "pipe": pipe,
        "owner": owner,
        "kind": kind,
        "days": days,
        "reason": norm["reason"],
        "line": (f"{pipe}, {owner}: {kind}, {days} day(s) "
                 f"(since {age_from.isoformat()}): {norm['reason']}"),
    })

def digest_payload(entries: list[dict], stale_days: int) -> dict | None:
    """Template-ready data for the pause_stale email - or None.

    None means "nothing stale": the wrapper sends nothing at all (no
    empty-mail noise, one debug line instead). run_id is added by the
    wrapper, which owns it.
    """
    if not entries:
        return None
    return {
        "stale_threshold": stale_days,
        "stale_count": len(entries),
        "stale_lines": "\n".join(e["line"] for e in entries),
    }