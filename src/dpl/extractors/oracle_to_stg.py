# dpl/extractors/oracle_to_stg.py
"""Oracle ETL extractor - glue between ora_connect, writer, and manifests.

Usage:
    python -m dpl.extractors.oracle_to_stg                    # run all, skip if exists
    python -m dpl.extractors.oracle_to_stg --force           # run all, ignore skips
    python -m dpl.extractors.oracle_to_stg --tables CUSTOMER_DETAILS
    python -m dpl.extractors.oracle_to_stg --force --tables CUSTOMER_DETAILS,ACCOUNT_DETAILS

Manifests: written by extract_all() to settings.paths.runs_root as
<runs_root>/<date>/<folder>/_manifest_<run_id>.json (archive location;
the old landing-dir manifest is retired). Envelope carries run-level
fields (run_id, profile, times, status, pending_tables) so history can
be rebuilt from files alone.

Declared schema (M3/M5):
    The master CSV (config/extractors/schema_master_oracle_to_stg.csv) is 
    the SINGLE source of truth for column names, order and dtypes. It is 
    loaded once per run, cross-checkedagainst config.yaml, and handed to 
    iter_batches() so every batch DataFrame is CONSTRUCTED with the declared 
    dtypes instead of inferring them from the data (which mis-types all-null 
    columns and blows up later in the writer).

Logging: dpl/lib/logger.py - "dpl.oracle_to_stg" under the central dpl tree.
Every log line in the per-table scope carries run_id= and table= context
(injected via get_run_logger), so one table's story across retries and
interleaved output can be isolated with grep.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

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
from dpl.lib.logger import get_run_logger, setup_logging
from dpl.lib.ora_connect import (
    ORACLE_CONFIG_SCHEMA,
    DTypeLike,
    connect,
    iter_batches,
    resolve_dtype,
)
from dpl.lib.session_pool import run_folder_tables
from dpl.lib.write_parquet import ParquetWriter

log = get_run_logger("dpl.oracle_to_stg")

CONFIG_FILE = "config/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "config/settings.laptop.yaml"

# Registry files - auto is generator-owned, ad is hand-maintained.
# They are merged at load time; a table key may appear in only one of them.
MASTER_AUTO = "config/extractors/schema_master_oracle_to_stg.csv"
RULES_CSV = "config/extractors/table_rules_oracle_to_stg.csv"


# Which connection *type* this extractor handles. Selection is by type,
# never by connection name - names are user-chosen and arbitrary.
SOURCE_TYPE = "oracle"

def _check_exists(settings: dict, cfg: dict, load_date: date,
                  conn_name: str, table_name: str) -> Path | None:
    """Return the destination dir if any published part exists; else None.

    Disk-as-truth: a table counts as done when at least one part file
    matches the writer's published pattern {table}-NNNN.parquet in the
    connection's configured folder. Paths come from settings.yaml (never
    from cfg); the folder comes from the connection's config entry.
    """
    folder = cfg["connections"][conn_name]["folder"]
    dest_dir = landing_dir(settings["paths"]["landing_root"], load_date, folder)
    table_dir = dest_dir / table_name.lower()
    if any(table_dir.glob(f"{table_name.lower()}-*.parquet")):
        return dest_dir
    return None


def _build_stats(
    table_cfg: dict,
    load_date: date,
    status: str,
    rows_read: int = 0,
    rows_written: int = 0,
    duration_sec: float = 0.0,
    error: str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Assemble a manifest-ready stats entry for one table."""
    return {
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


def _declared_schema_for(schema_master: dict | None, folder: str,
                         table_name: str) -> dict[str, DTypeLike] | None:
    """Resolve the master-CSV dtype strings for one table into Polars types.

    Returns None when no master was supplied (legacy/inference path).
    Raises ValueError when the table is absent from the master or a dtype
    string is unrecognized - the caller records it as a table failure.
    """
    if schema_master is None:
        return None

    key = f"{folder}.{table_name}".lower()
    entry = schema_master.get(key)
    if entry is None:
        raise ValueError(
            f"table '{key}' not in schema master - run: "
            f"python -m lib.generate_schemas --force"
        )
    # dict preserves the CSV row order == source column order == cursor order
    return {col: resolve_dtype(dt) for col, dt in entry["columns"].items()}

def _rules_for(rules: dict | None, folder: str,
               table_name: str) -> dict:
    """Fetch the rules entry for one table; empty entry when absent.

    A table with no row in table_rules.csv simply runs with no dedup and
    no sort (silent degradation, by design - validated up front by
    validate_rules_references so a typo'd table name never reaches here).
    """
    if rules is None:
        return {"raw_dedup": None, "sort_spec": []}
    return rules.get(
        f"{folder}.{table_name}".lower(),
        {"raw_dedup": None, "sort_spec": []},
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
):
    """Build the per-table task for the folder worker (B1).

    The returned callable takes the LIVE folder-session connection and
    performs exactly ONE attempt: fresh writer, fresh generator (the
    write_parquet retry contract). On any failure it RAISES - the
    worker's health gate classifies (session vs data), resets the
    session only when transport broke, and retries per the connection's
    configured knobs. Skip-check runs first and needs no connection.
    """
    table_name = table_cfg["name"]
    query = table_cfg["query"]
    conn_name = table_cfg["connection"]
    folder = cfg["connections"][conn_name]["folder"]
    batch_rows = settings.get("polars", {}).get("batch_rows", 50_000)
    strict_cast = settings.get("polars", {}).get("strict_cast", True)
    tlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id, table=table_name)

    def attempt(conn):
        """One attempt on the open session. Returns stats; raises on failure."""
        start = time.time()

        if not force:
            existing_dir = _check_exists(settings, cfg, load_date,
                                         conn_name, table_name)
            if existing_dir:
                tlog.info("skipping (%s exists)", existing_dir)
                return _build_stats(table_cfg, load_date, "skipped")

        # declared schema: the master CSV is the authority (M3/M5).
        # A missing-table ValueError is data-class: classification
        # refuses to retry it, the worker records the failure - same
        # net behavior as the old no-retry early return.
        declared = _declared_schema_for(schema_master, folder, table_name)

        rule = _rules_for(rules, folder, table_name)
        dedup_parsed = rule.get("dedup")
        sort_spec = rule.get("sort_spec") or []

        with ParquetWriter(
            settings,
            source=folder,
            table=table_name.lower(),
            load_date=load_date,
            transforms=cfg.get("transforms", {}),
            dedup_columns=dedup_parsed,
            sort_spec=sort_spec,
        ) as w:
            for df in iter_batches(conn, query, batch_size=batch_rows,
                                   declared=declared, strict=strict_cast):
                w.append(df)
            wstats = w.close()
        if wstats is None:
            # regression guard: close() always returns a stats dict
            raise RuntimeError(
                f"internal error: writer returned no stats for "
                f"{folder}.{table_name} - close() was not called?")
        tlog.info("completed (%d rows, %.1fs%s)", wstats.get("rows_read", 0),
                  time.time() - start,
                  ", sorted" if wstats.get("sort_columns") else "")
        return _build_stats(
            table_cfg, load_date, "extracted",
            rows_read=wstats.get("rows_read", 0),
            rows_written=wstats.get("rows_written", 0),
            duration_sec=time.time() - start,
            output_path=wstats.get("output_dir"),
        )

    return attempt

def extract_all(
    cfg: dict,
    settings: dict,
    load_date: date,
    force: bool = False,
    only_tables: list[str] | None = None,
    run_id: str = "-",
    profile: str = "manual",
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Extract all Oracle tables; return (manifests_by_folder, any_failed).

    - Startup guards run BEFORE any connection is opened: duplicate/unsafe
      folders, table collisions, malformed dedup specs, and master-vs-config
      drift all raise. These abort the whole run - fail fast, before any
      work is done (D4.1 tier 1).
    - The master CSV is loaded ONCE here and threaded down to every table.
    - Tables are selected by connection TYPE (SOURCE_TYPE), never by
      connection name, then grouped by their connection's folder. Folder
      and connection are 1:1 (enforced by validate_config_folders), so a
      folder group is exactly one connection's workload.
    - Long-lived session per folder; the session_pool health gate
      classifies failures (session vs data), retries per the
      connection's configured retries/backoff, and each retry is a
      fresh attempt (new writer, new generator) on a reset-if-needed
      session
    - CONTINUE ON FAILURE (D4.1 tiers 2 & 3): a table that fails is recorded
      as 'failed' and the next table proceeds normally. Each attempt opens
      its own connection, so one table's failure cannot poison its siblings.
      The run's exit code still reflects overall failure via any_failed.
    - Each folder gets its own manifest payload, keyed by folder name.
    - run_id (from the wrapper) is stamped into every table's log context.
    """
    rlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id)

    started_at = _utc_now_iso()
    runs_root = settings["paths"]["runs_root"]

    # --- guards: fail loudly before opening anything ---
    validate_config_folders(cfg)
    table_collision_check(cfg)

    # secrets/ holds vault files only - refuse before any connection.
    validate_secrets_layout()

    # Vault init: settings names the pass_mgr environment; init is
    # idempotent, so the wrapper calling oracle then ldap is fine.
    pm_env = (settings.get("pass_mgr") or {}).get("environment")
    if not pm_env:
        raise ValueError(
            "settings file must declare the vault environment, e.g.:\n"
            "  pass_mgr:\n"
            "    environment: laptop\n"
            "add it to config/settings.laptop.yaml")
    pass_mgr.init(pm_env)

    # --- master CSV: load once, cross-check against config, then it rules ---
    schema_master = load_schema_master(MASTER_AUTO)
    validate_master_vs_config(cfg, schema_master, SOURCE_TYPE)
    rules = load_table_rules(RULES_CSV)
    validate_rules_references(schema_master, rules)

    rlog.info("schema master loaded: %d table(s)", len(schema_master))

    tables = [
        t for t in cfg["tables"]
        if cfg["connections"].get(t["connection"], {}).get("type") == SOURCE_TYPE
    ]

    if only_tables:
        wanted = set(only_tables)
        tables = [t for t in tables if t["name"] in wanted]

    # group by folder, preserving config order within each group
    by_folder: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        folder = cfg["connections"][t["connection"]]["folder"]
        by_folder.setdefault(folder, []).append(t)

    manifests: dict[str, dict[str, Any]] = {}
    any_failed = False

    for folder, folder_tables in by_folder.items():
        # folder:connection is 1:1 (enforced by validate_config_folders),
        # so the folder's tables all share ONE connection - take it from
        # the first table and read its per-connection knobs.
        conn_name = folder_tables[0]["connection"]
        conn_cfg = cfg["connections"][conn_name]
        keepalive = (settings.get("extraction") or {}).get("keepalive_minutes")
        table_conc = conn_cfg.get("table_concurrency", 1)
        retries = conn_cfg.get("retries", 2)
        backoff = conn_cfg.get("retry_backoff_seconds", 5)

        def factory(_cn=conn_name, _ka=keepalive):
            return connect(cfg, _cn, expire_time=_ka)

        rlog.info("[%s] extracting %d table(s) (lanes=%d, retries=%d, "
                  "keepalive=%s min)", folder, len(folder_tables),
                  table_conc, retries, keepalive)

        tasks = [
            (t["name"], _task_for(cfg, settings, t, load_date, force,
                                  schema_master, rules, run_id))
            for t in folder_tables
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
                # duration on the failure path is 0.0: the worker owns
                # the clock now, and duration_sec is volatile-stripped by
                # the equivalence comparator anyway.
                stats = _build_stats(t, load_date, "failed",
                                     error=oc["error"])
            stats["attempts"] = oc["attempts"]
            results.append(stats)

            if stats["status"] in FAILURE_STATUSES:
                rlog.warning("[%s] %s failed after %d attempt(s) - continuing",
                             folder, t["name"], stats["attempts"])

        folder_failed = any(t["status"] in FAILURE_STATUSES for t in results)
        any_failed = any_failed or folder_failed

        pending_tables = [t["name"] for t in results
                          if t["status"] in FAILURE_STATUSES]

        envelope = {
            # run-level fields: update_history rebuilds the DB from these
            "run_id": run_id,
            "profile": profile,
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
            "status": "partial_failure" if folder_failed else "success",
            "pending_tables": pending_tables,
            # folder-level fields (as before)
            "folder": folder,
            "load_date": load_date.isoformat(),
            "tables": results,
            # operator summary counts (Part B request, 2026-11-14):
            # additive keys; history ingestion reads named fields only,
            # the comparator demands equality when both sides have them.
            "tables_total": len(results),
            "tables_extracted": sum(
                1 for r in results if r["status"] == "extracted"),
            "tables_skipped": sum(
                1 for r in results if r["status"] == "skipped"),
            "tables_failed": sum(
                1 for r in results if r["status"] in FAILURE_STATUSES),
        }
        manifests[folder] = envelope

        # THE manifest write - extractor-owned, into the archive.
        manifest_dir = runs_dir(runs_root, load_date, folder)
        write_manifest(manifest_dir, run_id, envelope)
        rlog.info("manifest written: %s/_manifest_%s.json",
                  manifest_dir, run_id)

    return manifests, any_failed


def main(run_id: str = "-") -> int:
    parser = argparse.ArgumentParser(description="Oracle ETL extractor")
    parser.add_argument("--force", action="store_true", help="re-extract even if files exist")
    parser.add_argument("--tables", help="comma-separated table names to extract")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("-c", "--config", default=CONFIG_FILE, help="config/extractors/oracle_to_stg.yaml")
    parser.add_argument("--settings", default=SETTINGS_FILE, help="config/settings.laptop.yaml")

    args = parser.parse_args()

    setup_logging()

    if args.verbose:
        log.setLevel(logging.DEBUG)     # bump only this component, not the tree

    try:
        cfg = load_config(CONFIG_FILE, ORACLE_CONFIG_SCHEMA)
        if cfg is None:
            raise RuntimeError(f"Failed to load {CONFIG_FILE}")

        settings = load_settings(SETTINGS_FILE)
        if settings is None:
            raise RuntimeError(f"Failed to load {SETTINGS_FILE}")           
    except Exception as exc:                                    # noqa: BLE001 - top-level: any startup failure maps to exit code 1 with a logged cause
        log.critical("config/settings load failed: %s", exc)
        return 1

    only_tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    load_date = date.today()

    # manual CLI run: derive a run_id (the wrapper passes ITS own in later,
    # via the run_id argument of main() -- default "-" kept for that hook)
    if run_id == "-":
        run_id = make_run_id(prefix="manual_")

    log.info("starting Oracle extraction (force=%s, tables=%s, strict_cast=%s, "
             "run_id=%s)",
             args.force, only_tables,
             settings.get("polars", {}).get("strict_cast", True), run_id)

    try:
        manifests, any_failed = extract_all(
            cfg, settings, load_date, force=args.force, only_tables=only_tables,
            run_id=run_id, profile="manual",
        )
    except (ValueError, KeyError, FileNotFoundError) as exc:
        log.critical("startup validation failed: %s", exc)
        return 1

    # summary
    all_tables = [t for m in manifests.values() for t in m["tables"]]
    extracted = sum(1 for t in all_tables if t["status"] == "extracted")
    skipped = sum(1 for t in all_tables if t["status"] == "skipped")
    failed = sum(1 for t in all_tables if t["status"] == "failed")

    log.info(
        "extraction complete: %d extracted, %d skipped, %d failed",
        extracted, skipped, failed,
    )

    if any_failed:
        log.warning("Some tables failed - see logs for details")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())