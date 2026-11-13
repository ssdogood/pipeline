# dpl/lib/extraction_config.py
"""Extraction concurrency knobs: validation, serial groups, run plan.

Phase 1 of the parallel-extraction design (design v3, LOCKED). This module
owns everything configuration-shaped about parallel extraction:

  - global knobs         settings: extraction.max_parallel_folders,
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
max_parallel_folders=1 - byte-for-byte today's sequential behavior - plus
a loud warning to set the block explicitly. The agreed operating defaults
(max_parallel_folders 4) live in the shipped settings file, never hidden
in code. Absent per-connection keys fall back to the agreed defaults
(concurrency 1, retries 2, backoff 5, group unset); PRESENT-but-invalid
values always fail loudly.
"""

from __future__ import annotations

import re

from dpl.lib.logger import get_run_logger

log = get_run_logger("dpl.extraction_config")

# Code-level defaults are the SAFE ones; the shipped settings file
# expresses the agreed operating point explicitly (see settings snippet).
GLOBAL_DEFAULTS = {"max_parallel_folders": 1, "keepalive_minutes": 10}
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
            f"{ctx}: '{key}' must be an integer >= {minimum}, got {value!r}")
    if value < minimum:
        raise ValueError(
            f"{ctx}: '{key}' must be >= {minimum}, got {value!r}")
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
            "(max_parallel_folders=%d). To enable folder parallelism add:",
            GLOBAL_DEFAULTS["max_parallel_folders"],
        )
        log.warning("  extraction:\n    max_parallel_folders: 4")
        return dict(GLOBAL_DEFAULTS)

    ctx = "settings: extraction"
    return {
        "max_parallel_folders": _int_ge(
            block.get("max_parallel_folders", GLOBAL_DEFAULTS["max_parallel_folders"]),
            "max_parallel_folders", ctx, 1),
        "keepalive_minutes": _int_ge(
            block.get("keepalive_minutes", GLOBAL_DEFAULTS["keepalive_minutes"]),
            "keepalive_minutes", ctx, 1),
    }


# ------------------------------------------------------------ connections ---
def validate_connection(pipe_name: str, conn_name: str,
                        conn_cfg: dict) -> dict:
    """Validate one connection's knobs; return its normalized summary.

    Raises ValueError naming pipe, connection, key and range. Unknown
    extra keys in conn_cfg are ignored here (the connector schemas own
    their keys; this module only reads the parallel-extraction knobs).
    """
    ctx = f"pipe '{pipe_name}' connection '{conn_name}'"
    out: dict = {
        "folder": conn_cfg["folder"],
        "type": conn_cfg["type"],
        "table_concurrency": _int_ge(
            conn_cfg.get("table_concurrency", CONN_DEFAULTS["table_concurrency"]),
            "table_concurrency", ctx, 1),
        "retries": _int_ge(
            conn_cfg.get("retries", CONN_DEFAULTS["retries"]),
            "retries", ctx, 0),
        "retry_backoff_seconds": _int_ge(
            conn_cfg.get("retry_backoff_seconds",
                         CONN_DEFAULTS["retry_backoff_seconds"]),
            "retry_backoff_seconds", ctx, 1),
    }

    if "serial_group" in conn_cfg:
        group = conn_cfg["serial_group"]
        if not isinstance(group, str) or not SERIAL_GROUP_RE.fullmatch(group):
            raise ValueError(
                f"{ctx}: 'serial_group' must match "
                f"^[a-z0-9][a-z0-9_-]*$ (lowercase letters, digits, "
                f"'-', '_', not starting with '-'/'_'), got {group!r}")
        out["serial_group"] = group
    return out


def validate_pipe_connections(pipe_name: str, cfg: dict) -> dict[str, dict]:
    """Validate every connection of one pipe; return {name: summary}."""
    return {
        name: validate_connection(pipe_name, name, conn_cfg)
        for name, conn_cfg in cfg["connections"].items()
    }


# ------------------------------------------------------------------- plan ---
def build_plan(max_parallel_folders: int,
               connections: dict[str, dict]) -> dict:
    """Fold validated connections into the schedulable plan.

    - Duplicate folders across pipes raise: two connections writing one
      folder would interleave parquet publishes. (Per-pipe validation
      already enforces uniqueness inside a pipe; this is the cross-pipe
      check - the namespace only the wrapper sees.)
    - Connections sharing serial_group are reported as ONE chain task.
    - worst_case_sessions = max_parallel_folders x max(table_concurrency):
      the number we can state to infrastructure as the hard ceiling.
    """
    folders: list[str] = []
    for name, c in connections.items():
        if c["folder"] in folders:
            clash = next(n for n, cc in connections.items()
                         if cc["folder"] == c["folder"] and n != name)
            raise ValueError(
                f"duplicate folder '{c['folder']}' across connections "
                f"'{clash}' and '{name}' - two connections must never "
                f"write one folder")
        folders.append(c["folder"])

    groups: dict[str, list[str]] = {}
    for name, c in connections.items():
        g = c.get("serial_group")
        if g:
            groups.setdefault(g, []).append(name)

    ungrouped = [n for n, c in connections.items() if not c.get("serial_group")]
    max_conc = max((c["table_concurrency"] for c in connections.values()),
                   default=1)
    return {
        "max_parallel_folders": max_parallel_folders,
        "connections": connections,
        "serial_groups": groups,
        "ungrouped": ungrouped,
        "tasks": len(ungrouped) + len(groups),
        "worst_case_sessions": max_parallel_folders * max_conc,
    }


def format_plan(plan: dict) -> str:
    """Render the plan as the operator-facing dry-run block (stdout)."""
    lines = [
        "==== EXTRACTION PLAN " + "=" * 42,
        (f"max_parallel_folders : {plan['max_parallel_folders']}"
        "        (settings: extraction.max_parallel_folders)"),
        ("keepalive_minutes    : see settings (Oracle expire_time;"
        " keep below firewall idle timeout)"),
        "",
        (f"{'connection':<16}{'type':<9}{'folder':<12}"
        f"{'conc':>5}{'retries':>9}{'backoff':>9}  serial_group"),
    ]
    for name, c in plan["connections"].items():
        lines.append(
            f"{name:<16}{c['type']:<9}{c['folder']:<12}"
            f"{c['table_concurrency']:>5}{c['retries']:>9}"
            f"{c['retry_backoff_seconds']:>9}  "
            f"{c.get('serial_group', '-')}")
    if plan["serial_groups"]:
        lines.append("")
        lines.append("serial groups (members NEVER run in parallel):")
        for g, members in plan["serial_groups"].items():
            lines.append(f"  {g}: {', '.join(members)}"
                         "  -> one chain (config order)")
    lines += [
        "",
        (f"schedulable tasks   : {plan['tasks']} "
        f"({len(plan['ungrouped'])} free folder(s) + "
        f"{len(plan['serial_groups'])} chain(s))"),
        (f"worst-case sessions : {plan['worst_case_sessions']} "
        f"(= max_parallel_folders {plan['max_parallel_folders']} x "
        f"max table_concurrency "
        f"{max((c['table_concurrency'] for c in plan['connections'].values()), default=1)})"),
        "=" * 62,
    ]
    return "\n".join(lines)
