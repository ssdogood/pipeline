# dpl/lib/extraction_config.py
"""Extraction concurrency knobs: validation, serial groups, run plan.

Phase 1 of the parallel-extraction design (design v3, LOCKED). This module
owns everything configuration-shaped about parallel extraction:

  - global knobs         settings: extraction.max_parallel_connections,
                                   extraction.keepalive_minutes
  - per-connection knobs extractor yaml, per connection:
                         table_concurrency, retries,
                         retry_backoff_seconds, serial_group

The connector config schemas (ORA/LDAP) accept unknown keys by design
(Optional(str): object), so these knobs pass the loaders silently - THIS
module is the sole enforcement point. Every violation raises ValueError
naming the pipe/connection, the key, and the accepted range. Nothing here
executes anything: no connections, no threads, no I/O.

Serial groups: connections sharing a serial_group name must NEVER extract
in parallel (two schemas on one physical box). build_plan() folds them
into chains; folder_runner.py (Phase 3) consumes the plan. Group names are
a GLOBAL namespace across all pipe yamls - a coupling between an Oracle
folder and an MSSQL folder on the same box is expressed across files.

Safe-upgrade rule: a settings file WITHOUT an extraction: block yields
max_parallel_connections=1 - byte-for-byte today's sequential behavior - plus
a loud warning to set the block explicitly. The agreed operating defaults
(max_parallel_connections 4) live in the shipped settings file, never hidden
in code. The connector config schemas accept unknown keys by design,
so `extraction_config` is the sole enforcement point (concurrency 1,
retries 2, backoff 5, group unset); PRESENT-but-invalid values always
fail loudly.
"""

from __future__ import annotations

import datetime as dt
import math
import re

from dpl.lib.logger import get_run_logger

__func_msg__ = {
    "name": "extraction config",
    "invocation": "",
    "category": "extraction",
    "purpose": (
        "Concurrency knobs for parallel extraction - validation, serial groups, run plan:\n"
        "   1. global knobs (max_parallel_connections, keepalive_minutes),\n"
        "   2. per-connection knobs (table_concurrency, retries, backoff, serial_group),\n"
        "   3. builds the validated run plan consumed by the folder scheduler"
    ),
}

log = get_run_logger("dpl.extraction_config")

# Code-level defaults are the SAFE ones; the shipped settings file
# expresses the agreed operating point explicitly (see settings snippet).
GLOBAL_DEFAULTS = {"max_parallel_connections": 1, "keepalive_minutes": 10}
CONN_DEFAULTS = {
    "table_concurrency": 1,
    "retries": 2,
    "retry_backoff_seconds": 5,
}
SERIAL_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _int_ge(value: object, key: str, ctx: str, minimum: int) -> int:
    """Strict positive-int check. Booleans are NOT ints here (True == 1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{ctx}: '{key}' must be an integer >= {minimum}, got {value!r}"
        )
    if value < minimum:
        raise ValueError(f"{ctx}: '{key}' must be >= {minimum}, got {value!r}")
    return value


# ----------------------------------------------------------------- global ---
def validate_global_extraction(settings: dict) -> dict:
    """Validate the settings 'extraction:' block; return normalized knobs.

    Missing block -> defaults (sequential mode) + loud warning. Present
    keys must be valid ints or the run aborts (wrapper maps this to
    exit 3, config error).
    """
    block = settings.get("extraction")
    if not isinstance(block, dict):
        log.warning(
            "settings has no 'extraction:' block - running SEQUENTIAL "
            "(max_parallel_connections=%d). To enable connection "
            "parallelism add:",
            GLOBAL_DEFAULTS["max_parallel_connections"],
        )
        log.warning("  extraction:\n    max_parallel_connections: 4")
        return dict(GLOBAL_DEFAULTS)

    ctx = "settings: extraction"
    # PT-041: retired key LOUDLY refused - unknown keys are tolerated
    # by schema design, so a silent rename would silently fall back to
    # the default (1) on every deployed settings file. The refusal
    # names the successor so the operator's fix is one edit.
    if "max_parallel_folders" in block:
        raise ValueError(
            f"{ctx}: 'max_parallel_folders' is retired - rename it to "
            f"'max_parallel_connections' (same meaning; folders are "
            f"1:1 with connections)"
        )
    return {
        "max_parallel_connections": _int_ge(
            block.get(
                "max_parallel_connections", GLOBAL_DEFAULTS["max_parallel_connections"]
            ),
            "max_parallel_connections",
            ctx,
            1,
        ),
        "keepalive_minutes": _int_ge(
            block.get("keepalive_minutes", GLOBAL_DEFAULTS["keepalive_minutes"]),
            "keepalive_minutes",
            ctx,
            1,
        ),
    }


# ------------------------------------------------------------ connections ---
def _table_concurrency(value: object, ctx: str) -> int:
    """table_concurrency must be an int 1 or 2 - a HARD session ceiling.

    Worst-case sessions = max_parallel_connections x table_concurrency,
    capped at 4 x 2 = 8 (the ceiling approved for infrastructure).
    Values above 2 are rejected here, not merely discouraged: the
    ceiling lives in validation, so no config can ever exceed it.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise ValueError(
            f"{ctx}: 'table_concurrency' must be 1 or 2 "
            f"(hard session ceiling: max_parallel_connections x 2), got {value!r}"
        )
    return value


def validate_connection(pipe_name: str, conn_name: str, conn_cfg: dict) -> dict:
    """Validate one connection's knobs; return its normalized summary.

    Raises ValueError naming pipe, connection, key and range. Unknown
    extra keys in conn_cfg are ignored here (the connector schemas own
    their keys; this module only reads the parallel-extraction knobs).
    """
    ctx = f"pipe '{pipe_name}' connection '{conn_name}'"
    out: dict = {
        "folder": conn_cfg["folder"],
        "type": conn_cfg["type"],
        "table_concurrency": _table_concurrency(
            conn_cfg.get("table_concurrency", CONN_DEFAULTS["table_concurrency"]), ctx
        ),
        "retries": _int_ge(
            conn_cfg.get("retries", CONN_DEFAULTS["retries"]), "retries", ctx, 0
        ),
        "retry_backoff_seconds": _int_ge(
            conn_cfg.get(
                "retry_backoff_seconds", CONN_DEFAULTS["retry_backoff_seconds"]
            ),
            "retry_backoff_seconds",
            ctx,
            1,
        ),
    }

    if "serial_group" in conn_cfg:
        group = conn_cfg["serial_group"]
        if not isinstance(group, str) or not SERIAL_GROUP_RE.fullmatch(group):
            raise ValueError(
                f"{ctx}: 'serial_group' must match "
                f"^[a-z0-9][a-z0-9_-]*$ (lowercase letters, digits, "
                f"'-', '_', not starting with '-'/'_'), got {group!r}"
            )
        out["serial_group"] = group
    # PT-032 (B8): forward the schedule block (validated upstream by the
    # connector schemas at Tier-1) so the dry-run banner can render the
    # due/not-due set. Absent = due every day (no change to plan math).
    if "schedule" in conn_cfg:
        out["schedule"] = conn_cfg["schedule"]
    return out


def validate_pipe_connections(pipe_name: str, cfg: dict) -> dict[str, dict]:
    """Validate every connection of one pipe; return {name: summary}."""
    return {
        name: validate_connection(pipe_name, name, conn_cfg)
        for name, conn_cfg in cfg["connections"].items()
    }


# ------------------------------------------------------------------- plan ---
def build_plan(
    max_parallel_connections: int, connections: dict[str, dict], plan_date: dt.date
) -> dict:
    """Fold validated connections into the schedulable plan.

    - Duplicate folders across pipes raise: two connections writing one
      folder would interleave parquet publishes. (Per-pipe validation
      already enforces uniqueness inside a pipe; this is the cross-pipe
      check - the namespace only the wrapper sees.)
    - Connections sharing serial_group are reported as ONE chain task.
    - worst_case_sessions = max_parallel_connections x max(table_concurrency):
      the number we can state to infrastructure as the hard ceiling.
    """
    folders: list[str] = []
    for name, c in connections.items():
        if c["folder"] in folders:
            clash = next(
                n
                for n, cc in connections.items()
                if cc["folder"] == c["folder"] and n != name
            )
            raise ValueError(
                f"duplicate folder '{c['folder']}' across connections "
                f"'{clash}' and '{name}' - two connections must never "
                f"write one folder"
            )
        folders.append(c["folder"])

    groups: dict[str, list[str]] = {}
    for name, c in connections.items():
        g = c.get("serial_group")
        if g:
            groups.setdefault(g, []).append(name)

    ungrouped = [n for n, c in connections.items() if not c.get("serial_group")]
    max_conc = max((c["table_concurrency"] for c in connections.values()), default=1)
    return {
        "max_parallel_connections": max_parallel_connections,
        "connections": connections,
        "serial_groups": groups,
        "ungrouped": ungrouped,
        "tasks": len(ungrouped) + len(groups),
        "worst_case_sessions": max_parallel_connections * max_conc,
        "plan_date": plan_date,
    }


def format_schedule_visibility(conns: dict[str, dict], plan_date: dt.date) -> list[str]:
    """Due/not-due lines for the plan date - one line per connection.

    Same is_due the extractors use at run time (schedule.is_due via
    the connection-summary shim), so the banner can never disagree
    with the run's decider. As-of (T-0/T-1/T-2) is NOT part of this
    block: schedule says WHEN, as_of says HOW FAR BACK - and dry-run
    shows due-set, not coverage math.
    """
    import datetime as dt_
    import textwrap

    from dpl.lib import schedule as schedule_mod

    del dt_, textwrap  # placeholders dropped; only schedule_mod needed

    due: list[str] = []
    not_due: list[str] = []
    for name, c in conns.items():
        if "schedule" in c:
            if schedule_mod.is_due_connection(c, plan_date):
                due.append(name)
            else:
                not_due.append(name)
    lines = [
        "",
        f"schedule (for {plan_date.isoformat()}):",
        f"  due      : {', '.join(due) if due else '(none)'}",
        f"  not due  : {', '.join(not_due) if not_due else '(none)'}",
        ("  (connections without a schedule are due every day - not listed above)"),
    ]
    return lines


def format_plan(plan: dict) -> str:
    """Render the plan as the operator-facing dry-run block (stdout).

    Column widths adapt to the longest actual value (floored at the
    column label) so long connection/folder names can never break
    alignment; the banner width derives from the table itself.
    """
    conns = plan["connections"]

    w_conn = max([len("connection")] + [len(n) for n in conns]) + 2
    w_type = max([len("type")] + [len(c["type"]) for c in conns.values()]) + 2
    w_fold = max([len("folder")] + [len(c["folder"]) for c in conns.values()]) + 2
    w_num = len("backoff") + 2  # right-aligned numeric columns

    header = (
        f"{'connection':<{w_conn}}{'type':<{w_type}}{'folder':<{w_fold}}"
        f"{'conc':>{w_num}}{'retries':>{w_num}}{'backoff':>{w_num}}"
        "  serial_group"
    )
    width = (math.floor(len(header) / 2) * 2) + 2

    lines = [
        "=" * width,
        " " * (math.floor(width / 2) - 8) + "EXTRACTION PLAN",
        "=" * width,
        (
            f"max_parallel_connections : {plan['max_parallel_connections']}"
            "  (settings: extraction.max_parallel_connections)"
        ),
        (
            "keepalive_minutes    : see settings (driver keepalive: Oracle uses"
            " expire_time; AD ruling: reconnect on drop - no keepalive)"
        ),
        "",
        header,
    ]
    for name, c in conns.items():
        lines.append(
            f"{name:<{w_conn}}{c['type']:<{w_type}}{c['folder']:<{w_fold}}"
            f"{c['table_concurrency']:>{w_num}}{c['retries']:>{w_num}}"
            f"{c['retry_backoff_seconds']:>{w_num}}"
            f"  {c.get('serial_group', '-')}"
        )
    if plan["serial_groups"]:
        lines.append("")
        lines.append("serial groups (members NEVER run in parallel):")
        for g, members in plan["serial_groups"].items():
            lines.append(f"  {g}: {', '.join(members)}  -> one chain (config order)")
    max_conc = max((c["table_concurrency"] for c in conns.values()), default=1)
    lines += [
        "",
        (
            f"schedulable tasks   : {plan['tasks']} "
            f"({len(plan['ungrouped'])} free folder(s) + "
            f"{len(plan['serial_groups'])} chain(s))"
        ),
        (
            f"worst-case sessions : {plan['worst_case_sessions']} "
            f"(= max_parallel_connections {plan['max_parallel_connections']} x "
            f"max table_concurrency {max_conc})"
        ),
    ]
    # PT-032 (B8, design 4): schedule visibility - which connections are
    # due today, which are not. Rendered only when at least one summary
    # carries a schedule block, so unscheduled legacy configs see no new
    # banner. The date is the run date, stated (schedule questions are
    # date questions); weekend/month-end edge words come from the same
    # schedule.is_due the extractors use at run time.
    if any("schedule" in c for c in conns.values()):
        lines += format_schedule_visibility(conns, plan["plan_date"])
    lines += ["=" * width]
    return "\n".join(lines)
