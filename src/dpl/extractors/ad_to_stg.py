# dpl/extractors/ad_to_stg.py
"""Active Directory extractor - glue between ldap_connect, writer, and manifests.

Usage:
    python -m dpl.extractors.ad_to_stg                    # run all, skip if exists
    python -m dpl.extractors.ad_to_stg --force           # run all, ignore skips
    python -m dpl.extractors.ad_to_stg --tables subd-users
    python -m dpl.extractors.ad_to_stg --force --tables subd-users,subd-groups

Manifests: written by extract_all() to settings.paths.runs_root as <runs_root>/<date>/<folder>/_manifest_<run_id>.json (archive location).
Envelope carries run-level fields (run_id, profile, times, status,
pending_tables) so history can be rebuilt from files alone. Contract mirrors dpl.extractors.oracle_to_stg exactly.

Declared schema (M3/M5):
    The master CSV (config/extractors/schema_master_ad_to_stg.csv) is the
    SINGLE source of truth for column names, order and dtypes. It is loaded
    once per run, cross-checked against the config YAML, and drives both
    the LDAP attribute request list and the constructed DataFrame dtypes.

MV child tables:
    Columns flagged 'mv=child' in the master's policy_flags are requested
    from LDAP but NOT written to the parent parquet. Each becomes its own
    child table '<parent>_<attr>' with columns (objectguid, value), one
    row per multi-value element. Parent and children are fed from the SAME
    paged search - one LDAP pass, both always consistent.

AD-specific conventions (differ from Oracle deliberately):
    - values are CASE-PRESERVED (SMTP prefixes, DNs, display names);
    transforms are empty - the writer never uppercases AD data.
    - column names are lowercase; LDAP returns attribute keys in server
    casing (sAMAccountName, objectGUID), mapped case-insensitively.
    - LDAP omits empty attributes from entries entirely; absent
    attributes are filled with None so the declared schema holds.

Logging: dpl/lib/logger.py - "dpl.ad_to_stg". Every per-table line carries run_id= and table= context, so one table's story can be isolated with grep.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from dpl.lib import pass_mgr
from dpl.lib.common import (
    FAILURE_STATUSES,
    landing_dir,
    load_config,
    load_schema_master,
    load_settings,
    load_table_rules,
    make_run_id,
    runs_dir,
    table_collision_check,
    validate_config_folders,
    validate_master_vs_config,
    validate_rules_references,
    validate_secrets_layout,
    write_manifest,
)
from dpl.lib.ldap_connect import LDAP_CONFIG_SCHEMA, connect, iter_paged
from dpl.lib.logger import get_run_logger, setup_logging
from dpl.lib.pause import (
    PAUSED,
    describe,
    lifted_by_override,
    manifest_fields,
    state_for,
    validate_pause_declarations,
)
from dpl.lib.session_pool import run_folder_tables
from dpl.lib.write_parquet import ParquetWriter

log = get_run_logger("dpl.ad_to_stg")

CONFIG_FILE = "config/extractors/ad_to_stg.yaml"
SETTINGS_FILE = "config/settings.laptop.yaml"

MASTER_AUTO = "config/extractors/schema_master_ad_to_stg.csv"
RULES_CSV = "config/extractors/table_rules_ad_to_stg.csv"

# Which connection *type* this extractor handles - selection is by type, # never by connection name (same rule as oracle_to_stg).
SOURCE_TYPE = "ldap"

# Entries buffered in plain dicts before one DataFrame is built and # appended - keeps DuckDB insert count sane while staying memory-bounded # (buffers flush long before page accumulation matters).
ENTRY_FLUSH = 500

# Child-table frame schema: (join key, value) - always these two columns.
CHILD_SCHEMA: dict[str, type[pl.DataType]] = {"objectguid": pl.Utf8, "value": pl.Utf8}

_PL_DTYPES = {
    "utf8": pl.Utf8,
    "int64": pl.Int64,
    "float64": pl.Float64,
    "boolean": pl.Boolean,
    "date": pl.Date,
}


def _resolve_dtype(dt: str) -> type[pl.DataType]:
    """Master CSV dtype string -> Polars DataType (unknown -> Utf8)."""
    return _PL_DTYPES.get(dt.strip().lower(), pl.Utf8)


def _cell(v: Any) -> str | None:
    """Normalize one attribute value for a Utf8 column.

    None stays None; lists (unexpected on a parent single-value column)
    are pipe-joined rather than crashing; everything else str()'d.
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v if x is not None)
    return str(v)


def _flag_is_mv_child(fl: Any) -> bool:
    """True when one column's parsed policy_flags marks it mv=child.

    The loader parses policy_flags into nested structures, so accept the
    shapes it plausibly produces rather than assuming one: {'mv':'child'},
    {'mv=child': True}, 'mv=child', or a list of any of those.
    """
    if not fl:
        return False
    if isinstance(fl, str):
        return "mv=child" in fl.lower().replace(" ", "")
    if isinstance(fl, dict):
        for k, v in fl.items():
            kl = str(k).lower().replace("_", " ").replace("=", " ")
            if "mv" in kl and "child" in kl:
                return True
            if kl.strip() == "mv" and str(v).lower() == "child":
                return True
        return False
    if isinstance(fl, (list, tuple, set)):
        return any(_flag_is_mv_child(x) for x in fl)
    return False


def _mv_columns_for(entry: dict) -> list[str]:
    """Column names flagged mv=child in a master entry's flags dict."""
    flags = entry.get("flags") or {}
    if not isinstance(flags, dict):
        return []
    return [col for col, fl in flags.items() if _flag_is_mv_child(fl)]

def _check_exists(settings: dict, cfg: dict, load_date: date,
                   conn_name: str, table_name: str,
                   child_names: list[str]) -> Path | None:
    """Return the destination dir if parent AND every child is published.

    Children ride with the parent: a partial parent-without-children state
    does NOT count as done, so a retry repairs the missing half.
    """
    folder = cfg["connections"][conn_name]["folder"]
    dest_dir = landing_dir(settings["paths"]["landing_root"], load_date, folder)
    for name in [table_name] + child_names:
        table_dir = dest_dir / name.lower()
        if not any(table_dir.glob(f"{name.lower()}-*.parquet")):
            return None
    return dest_dir


def _build_stats(
    table_cfg: dict,
    load_date: date,
    status: str,
    rows_read: int = 0,
    rows_written: int = 0,
    duration_sec: float = 0.0,
    error: str | None = None,
    output_path: str | None = None,
    children: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble a manifest-ready stats entry for one table."""
    stats: dict[str, Any] = {
        "connection": table_cfg["connection"],
        "schema": table_cfg["schema"],
        "name": table_cfg["name"],
        "load_date": load_date.isoformat(),
        "status": status,
        "rows_read": rows_read,
        "rows_written": rows_written,
        "duration_sec": round(duration_sec, 2),
        "output_path": output_path,
        "error": error,
    }
    if children:
        stats["child_tables"] = children
        stats["child_rows_written"] = sum(children.values())
    return stats


def _declared_schema_for(schema_master: dict, folder: str,
                          table_name: str) -> tuple[dict[str, type[pl.DataType]], list[str]]:
    """Resolve the master's dtypes + MV columns for one table.

    Returns (declared_dtypes, mv_child_columns) - the latter drawn from
    policy_flags, NOT dtype.
    Raises ValueError when the table is absent from the master.
    """
    key = f"{folder}.{table_name}".lower()
    entry = schema_master.get(key)
    if entry is None:
        raise ValueError(
            f"table '{key}' not in schema master - edit "
            f"{MASTER_AUTO} and add the table"
        )
    declared = {col: _resolve_dtype(dt) for col, dt in entry["columns"].items()}
    return declared, _mv_columns_for(entry)


def _rules_for(rules: dict | None, folder: str, table_name: str) -> dict:
    """Rules entry for one table; empty entry when absent (silent degrade)."""
    if rules is None:
        return {"dedup": None, "sort_spec": []}
    return rules.get(
        f"{folder}.{table_name}".lower(),
        {"dedup": None, "sort_spec": []},
    )


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _task_for(
    cfg: dict,
    settings: dict,
    table_cfg: dict,
    load_date: date,
    force: bool,
    schema_master: dict | None,
    rules: dict | None,
    run_id: str,
    pos: int | None = None,
    total: int | None = None,
    paused_override: bool = False,
):
    """Build the per-table task for the folder worker (B1).

    The returned callable takes the LIVE folder-session connection and
    performs exactly ONE attempt: fresh writer, fresh generator (the
    write_parquet retry contract). On any failure it RAISES - the
    worker's health gate classifies (session vs data), resets the
    session only when transport broke, and retries per the connection's
    configured knobs. Skip-check runs first and needs no connection.

    PT-014: paused_override is the --paused-table flag, threaded from
    the wrapper; False on every cron nightly. All pause intelligence
    lives in dpl.lib.pause (lifted_by_override) - this extractor only
    consumes its verdicts.
    """
    table_name = table_cfg["name"]
    search_base = table_cfg["search_base"]
    search_filter = table_cfg["filter"]
    conn_name = table_cfg["connection"]
    folder = cfg["connections"][conn_name]["folder"]
    page_size = cfg["connections"][conn_name].get("page_size", 500)

    tlog = get_run_logger("dpl.ad_to_stg", run_id=run_id, table=table_name)

    # PT-014: resolve pause state for this table. All intelligence
    # lives in dpl.lib.pause: folder-level block dominates table-level;
    # --paused-table lifts a table-level pause for THIS run only and
    # can never lift a folder-level one.
    paused_state, pause_norm, _was_lifted = lifted_by_override(
        table_cfg, cfg["connections"].get(conn_name), paused_override)
    pause_line = (describe(paused_state, pause_norm)
                  if pause_norm is not None else "")
    pause_fields = {}
    if paused_state == PAUSED:
        # pyrefly narrowing: PAUSED implies a governing block exists
        assert pause_norm is not None
        pause_fields = manifest_fields(pause_norm)

    def attempt(conn):
        """One attempt on the open session. Returns stats; raises on failure."""
        start = time.time()

        if pos is not None:
            # PT-029: per-table start line restored at its new home -
            # counter is the table's fixed config position; mirrors
            # oracle_to_stg's attempt() exactly.
            tlog.info("[%s %d/%d] extracting...", folder, pos, total)

        # PT-014: pause check runs FIRST - before the declared-schema
        # resolution, the skip-check, and the force branch. Needs no
        # connection. Pause outranks profile-force; only --paused-table
        # lifts it, and only for this manual run (the wrapper's flag
        # rides the same 'paused_override' path, absent for cron).
        if paused_state == PAUSED:
            tlog.info("%s", pause_line)
            paused_stats = _build_stats(table_cfg, load_date, "paused")
            paused_stats.update(pause_fields)
            return paused_stats

        # --- declared schema: the master CSV is the authority (M3/M5) ---
        # ValueError here is data-class: no retry, table fails - same
        # net behavior as the old no-retry early return.
        if schema_master is None:
            raise ValueError("schema master not supplied")
        declared, mv_columns = _declared_schema_for(schema_master,
                                                folder, table_name)

        parent_cols = [c for c in declared if c not in mv_columns]
        ldap_attributes = list(declared.keys())

        child_names = {mv: f"{table_name.lower()}_{mv}" for mv in mv_columns}

        if not force:
            existing_dir = _check_exists(settings, cfg, load_date,
                                        conn_name, table_name,
                                        list(child_names.values()))
            if existing_dir:
                tlog.info("skipping (%s exists)", existing_dir)
                return _build_stats(table_cfg, load_date, "skipped")

        rule = _rules_for(rules, folder, table_name)
        dedup_parsed = rule.get("dedup")
        sort_spec = rule.get("sort_spec") or []

        with ExitStack() as stack:
            pw = stack.enter_context(ParquetWriter(
                settings,
                source=folder,
                table=table_name.lower(),
                load_date=load_date,
                transforms={},          # AD: case-preserved, no cleanse
                dedup_columns=dedup_parsed,
                sort_spec=sort_spec,
                run_id=run_id,
            ))
            # child writers: own rules row, own dedup/sort
            cws: dict[str, Any] = {}
            for mv, cname in child_names.items():
                crule = _rules_for(rules, folder, cname)
                cws[mv] = stack.enter_context(ParquetWriter(
                    settings,
                    source=folder,
                    table=cname,
                    load_date=load_date,
                    transforms={},
                    dedup_columns=crule.get("dedup"),
                    sort_spec=crule.get("sort_spec") or [],
                    run_id=run_id,
                ))

            parent_buf: list[dict[str, Any]] = []
            child_bufs: dict[str, list[dict[str, Any]]] = \
                    {mv: [] for mv in mv_columns}

            def _flush() -> None:
                if parent_buf:
                    pw.append(pl.from_dicts(parent_buf,
                                            schema={c: declared[c]
                                                    for c in parent_cols}))
                    parent_buf.clear()
                for mv, buf in child_bufs.items():
                    if buf:
                        cws[mv].append(pl.from_dicts(buf,
                                        schema=CHILD_SCHEMA))
                        buf.clear()

            for entry in iter_paged(conn, search_base, search_filter,
                                    ldap_attributes,
                                    page_size=page_size):
                # case-insensitive: server casing -> master lowercase
                low = {k.lower(): v for k, v in (entry or {}).items()}
                guid = _cell(low.get("objectguid"))

                parent_buf.append(
                    {c: _cell(low.get(c)) for c in parent_cols}
                )
                for mv in mv_columns:
                    vals = low.get(mv)
                    if vals is None or guid is None:
                        continue
                    if not isinstance(vals, (list, tuple)):
                        vals = [vals]
                    for v in vals:
                        if v is not None:
                            child_bufs[mv].append(
                                {"objectguid": guid, "value": str(v)}
                            )
                if len(parent_buf) >= ENTRY_FLUSH:
                    _flush()

            _flush()

            parent_stats = pw.close()
            child_stats = {child_names[mv]:
                        cws[mv].close().get("rows_written", 0)
                        for mv in mv_columns}

            tlog.info("completed (%d rows, %d child rows, %.1fs)",
                    parent_stats.get("rows_written", 0),
                    sum(child_stats.values()) if child_stats else 0,
                    time.time() - start)
            return _build_stats(
                table_cfg, load_date, "extracted",
                rows_read=parent_stats.get("rows_read", 0),
                rows_written=parent_stats.get("rows_written", 0),
                duration_sec=time.time() - start,
                output_path=parent_stats.get("output_dir"),
                children=child_stats,
            )

    return attempt


def prepare_run(
    cfg: dict,
    settings: dict,
    only_tables: list[str] | None = None,
    run_id: str = "-",
    paused_override: bool = False,
) -> dict[str, Any]:
    """Run head carved out of extract_all (Phase 4 C1).

    Mirrors oracle_to_stg.prepare_run: guards, vault init, master and
    rules load/validation, table selection, folder grouping - the
    everything-before-any-bind step. The wrapper (C2) calls this
    once per pipe, then schedules folders via extract_folder.

    Returns the run context (started_at, by_folder, schema_master,
    rules) - same keys as the Oracle extractor, by contract.
    """
    rlog = get_run_logger("dpl.ad_to_stg", run_id=run_id)

    started_at = _utc_now_iso()

    # --- guards: fail loudly before opening anything ---
    validate_config_folders(cfg)
    table_collision_check(cfg)
    validate_secrets_layout()

    # Vault init: settings name the pass_mgr environment; idempotent, so
    # sharing a process with the Oracle extractor is fine.
    pm_env = (settings.get("pass_mgr") or {}).get("environment")
    if not pm_env:
        raise ValueError(
            "settings file must declare the vault environment, e.g.:\n"
            "  pass_mgr:\n    environment: laptop")
    pass_mgr.init(pm_env)

    # --- master CSV: load once, cross-check against config, then it rules ---
    schema_master = load_schema_master(MASTER_AUTO)
    validate_master_vs_config(cfg, schema_master, SOURCE_TYPE)
    rules = load_table_rules(RULES_CSV)
    validate_rules_references(schema_master, rules)

    rlog.info("schema master loaded: %d table(s)", len(schema_master))

    # PT-014: every declared pause block, both levels, loudly
    # validated here FIRST (named errors; the yaml-load schema already
    # refused structural garbage - this adds the owner's name).
    validate_pause_declarations(cfg)

    # PT-014: --tables naming paused tables without --paused-table
    # refuses THOSE tables loudly (the operator remedy is in the
    # message); unnamed tables proceed untouched. PT-036 fix
    # (2026-11-20): with --paused-table given the refusal must NOT
    # fire - the operator already chose the remedy the message
    # prescribes; the lift verdict itself (including folder-level
    # never-liftable) belongs to the attempt layer.
    if only_tables and not paused_override:
        refused = []
        for t in cfg["tables"]:
            if t["name"] not in only_tables:
                continue
            conn_cfg = cfg["connections"].get(t["connection"])
            state, _ = state_for(t, conn_cfg)
            if state == PAUSED:
                refused.append(t["name"])
        if refused:
            rlog.error(
                "paused table(s) %s requested without --paused-table - "
                "to extract a paused table, rerun with "
                "--paused-table (or remove its pause tag)",
                ", ".join(refused))
            only_tables = [n for n in only_tables
                           if n not in set(refused)]

    tables = [
        t for t in cfg["tables"]
        if cfg["connections"].get(t["connection"], {}).get("type") == SOURCE_TYPE
    ]

    if only_tables is not None and not only_tables:
        # PT-014 live-run finding (2026-11-18): refusal can empty the
        # selection. An empty selection means "extract NOTHING", never
        # "extract everything" - the type-filter list is the fallback
        # for NO selection, not for a collapsed one.
        tables = []
    elif only_tables:
        wanted = set(only_tables)
        tables = [t for t in tables if t["name"] in wanted]

    # group by folder, preserving config order within each group
    by_folder: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        folder = cfg["connections"][t["connection"]]["folder"]
        by_folder.setdefault(folder, []).append(t)

    return {
        "started_at": started_at,
        "by_folder": by_folder,
        "schema_master": schema_master,
        "rules": rules,
        "paused_override": paused_override,
    }


def extract_folder(
    ctx: dict[str, Any],
    cfg: dict,
    settings: dict,
    load_date: date,
    folder: str,
    force: bool = False,
    run_id: str = "-",
    profile: str = "manual",
) -> dict[str, Any]:
    """Extract ONE folder's tables; return its envelope (Phase 4 C1).

    The former folder-loop body, unchanged: one long-lived LDAP
    session (B1 worker), per-connection knobs, envelope with summary
    counts, manifest write. No keepalive (AD ruling: reconnect on
    failure is handled by the health gate, not pooling semantics).
    Table failures are recorded, never raised; a folder name not in
    ctx['by_folder'] raises KeyError (the caller records it).
    """
    rlog = get_run_logger("dpl.ad_to_stg", run_id=run_id)
    folder_tables = ctx["by_folder"][folder]
    runs_root = settings["paths"]["runs_root"]

    # folder:connection is 1:1, so all tables share ONE connection.
    conn_name = folder_tables[0]["connection"]
    conn_cfg = cfg["connections"][conn_name]
    table_conc = conn_cfg.get("table_concurrency", 1)
    retries = conn_cfg.get("retries", 2)
    backoff = conn_cfg.get("retry_backoff_seconds", 5)

    # AD ruling: plain connect, NO keepalive/expiry.
    def factory(_cn=conn_name):
        return connect(cfg, _cn)

    rlog.info("[%s] extracting %d table(s) (lanes=%d, retries=%d)",
            folder, len(folder_tables), table_conc, retries)

    total_tables = len(folder_tables)
    tasks = [
        (t["name"], _task_for(cfg, settings, t, load_date, force,
                              ctx["schema_master"], ctx["rules"], run_id,
                              pos=pos, total=total_tables,
                              paused_override=ctx.get("paused_override", False)))
        for pos, t in enumerate(folder_tables, start=1)
    ]

    outcomes = run_folder_tables(
        folder, tasks, factory,
        table_concurrency=table_conc, retries=retries,
        backoff_seconds=backoff)

    results: list[dict[str, Any]] = []
    for t, oc in zip(folder_tables, outcomes):
        if oc["status"] == "ok":
            stats = oc["result"]
        else:
            stats = _build_stats(t, load_date, "failed",
                                error=oc["error"])
        stats["attempts"] = oc["attempts"]
        results.append(stats)

        if stats["status"] in FAILURE_STATUSES:
            rlog.warning("[%s] %s failed after %d attempt(s) - continuing",
                        folder, t["name"], stats["attempts"])

    folder_failed = any(t["status"] in FAILURE_STATUSES for t in results)

    pending_tables = [t["name"] for t in results
                    if t["status"] in FAILURE_STATUSES]

    envelope = {
        # run-level fields: update_history rebuilds the DB from these
        "run_id": run_id,
        "profile": profile,
        "started_at": ctx["started_at"],
        "finished_at": _utc_now_iso(),
        "status": "partial_failure" if folder_failed else "success",
        "pending_tables": pending_tables,
        # folder-level fields (as before)
        "folder": folder,
        "load_date": load_date.isoformat(),
        "tables": results,
        # operator summary counts (Part B request, 2026-11-14)
        "tables_total": len(results),
        "tables_extracted": sum(
            1 for r in results if r["status"] == "extracted"),
        "tables_skipped": sum(
            1 for r in results if r["status"] == "skipped"),
        "tables_paused": sum(
            1 for r in results if r["status"] == "paused"),
        "tables_failed": sum(
            1 for r in results if r["status"] in FAILURE_STATUSES),
    }

    manifest_dir = runs_dir(runs_root, load_date, folder)
    write_manifest(manifest_dir, run_id, envelope)
    rlog.info("manifest written: %s/_manifest_%s.json",
            manifest_dir, run_id)

    return envelope


def extract_all(
    cfg: dict,
    settings: dict,
    load_date: date,
    force: bool = False,
    only_tables: list[str] | None = None,
    run_id: str = "-",
    profile: str = "manual",
    paused_override: bool = False,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Extract all AD tables; return (manifests_by_folder, any_failed).

    Thin shell over prepare_run + extract_folder (Phase 4 C1) - the
    manual CLI path. The wrapper's merged scheduler (C2) calls the
    pieces separately. Signature and behavior unchanged.
    """
    ctx = prepare_run(cfg, settings, only_tables=only_tables, run_id=run_id,
                      paused_override=paused_override)

    manifests: dict[str, dict[str, Any]] = {}
    for folder in ctx["by_folder"]:
        manifests[folder] = extract_folder(
            ctx, cfg, settings, load_date, folder, force=force,
            run_id=run_id, profile=profile)

    any_failed = any(m["status"] == "partial_failure"
                    for m in manifests.values())
    return manifests, any_failed


def main(run_id: str = "-") -> int:
    parser = argparse.ArgumentParser(description="Active Directory extractor")
    parser.add_argument("--force", action="store_true", help="re-extract even if files exist")
    parser.add_argument("--tables", help="comma-separated table names to extract")
    parser.add_argument("--paused-table", action="store_true",
                        help="with --tables naming paused table(s), "
                             "lift their TABLE-level pause for this run")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("-c", "--config", default=CONFIG_FILE, help="config/extractors/oracle_to_stg.yaml")
    parser.add_argument("--settings", default=SETTINGS_FILE, help="config/settings.laptop.yaml")

    args = parser.parse_args()

    setup_logging()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        cfg = load_config(CONFIG_FILE, LDAP_CONFIG_SCHEMA)
        if cfg is None:
            raise RuntimeError(f"Failed to load {CONFIG_FILE}")

        settings = load_settings(SETTINGS_FILE)
        if settings is None:
            raise RuntimeError(f"Failed to load {SETTINGS_FILE}")
    except Exception as exc:                               	# noqa: BLE001 - top-level: any startup failure maps to exit code 1 with a logged cause
        log.critical("config/settings load failed: %s", exc)
        return 1

    only_tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    paused_override = bool(args.paused_table)
    if paused_override and not only_tables:
        log.error("--paused-table modifies --tables selections only - "
                  "rerun as: --tables <names> --paused-table")
        return 3
    load_date = date.today()

    if run_id == "-":
        run_id = make_run_id(prefix="manual_")

    log.info("starting AD extraction (force=%s, tables=%s, run_id=%s)",
            args.force, only_tables, run_id)

    try:
        manifests, any_failed = extract_all(
            cfg, settings, load_date, force=args.force, only_tables=only_tables,
            run_id=run_id, profile="manual", paused_override=paused_override,
        )
    except (ValueError, KeyError, FileNotFoundError) as exc:
        log.critical("startup validation failed: %s", exc)
        return 1

    # summary
    all_tables = [t for m in manifests.values() for t in m["tables"]]
    extracted = sum(1 for t in all_tables if t["status"] == "extracted")
    skipped = sum(1 for t in all_tables if t["status"] == "skipped")
    paused = sum(1 for t in all_tables if t["status"] == "paused")
    failed = sum(1 for t in all_tables if t["status"] == "failed")

    log.info(
        "extraction complete: %d extracted, %d skipped, %d paused, %d failed",
        extracted, skipped, paused, failed,
    )

    if any_failed:
        log.warning("Some tables failed - see logs for details")
        return 1

    return 0


if __name__ == "__main__":
     sys.exit(main())