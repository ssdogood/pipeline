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

from dpl.lib import asof, pass_mgr, schedule
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
from dpl.lib.environment import resolve_environment
from dpl.lib.logger import get_run_logger, setup_logging
from dpl.lib.ora_connect import (
    ORACLE_CONFIG_SCHEMA,
    DTypeLike,
    connect,
    iter_batches,
    resolve_dtype,
)
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

__func_msg__ = {
    "name": "oracle extractor",
    "invocation": "(internal module, not a command)",
    "category": "extraction",
    "purpose": ("Extract Oracle tables → staging layer"),
}

log = get_run_logger("dpl.oracle_to_stg")

# PT-040 4f: manual-CLI hint paths - real values live in the ACTIVE
# env folder (<env>/extractors/, <env>/settings.yaml), resolved from
# config/environment.yaml (the one and only declaration).
CONFIG_FILE = "<env-folder>/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "<env-folder>/settings.yaml"

# Registry files for THIS extractor, one set per extractor:
# the schema master is generator-owned (dpl.lib.generate_schemas),
# the table rules CSV is hand-maintained. They are NOT merged with
# any other extractor's registries.
# PT-040 4f: registry paths resolved from the ACTIVE env folder at
# call time (the constants remain as documentation hints only).
MASTER_AUTO = "<env-folder>/extractors/schema_master_oracle_to_stg.csv"
RULES_CSV = "<env-folder>/extractors/table_rules_oracle_to_stg.csv"


def _master_auto() -> str:
    return str(
        resolve_environment().folder / "extractors" / "schema_master_oracle_to_stg.csv"
    )


def _rules_csv() -> str:
    return str(
        resolve_environment().folder / "extractors" / "table_rules_oracle_to_stg.csv"
    )


# Which connection *type* this extractor handles. Selection is by type,
# never by connection name - names are user-chosen and arbitrary.
SOURCE_TYPE = "oracle"


def _check_exists(
    settings: dict, cfg: dict, load_date: date, conn_name: str, table_name: str
) -> Path | None:
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
    anchor_date: date | None = None,
) -> dict[str, Any]:
    """Assemble a manifest-ready stats entry for one table.

    PT-015 (B5): with the folder's anchor_date, the entry carries the
    table's as-of story: declared type + the coverage statement from
    asof.coverage_statement (master: snapshot-as-of anchor;
    transaction: anchor-(W-1)..anchor). Recorded for EVERY status -
    including not_scheduled/paused/skipped - so the manifest states
    what the batch WOULD have covered. Fields are additive: absent
    only when the caller passes no anchor (none exists today).
    """
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
    if anchor_date is not None:
        table_type = asof.validate_table_type(
            table_cfg.get("type"),
            f"[{table_cfg['connection']}] table {table_cfg['name']}",
        )
        stats["type"] = table_type
        window = 1
        if table_type == "transaction":
            window = asof.validate_window_days(
                table_cfg.get("window_days"),
                f"[{table_cfg['connection']}] table {table_cfg['name']}",
            )
        cov = asof.coverage_statement(table_type, anchor_date, window)
        cov_from = cov["from"]
        cov_to = cov["to"]
        stats["coverage_kind"] = cov["kind"]
        stats["coverage_from"] = (
            cov_from.isoformat() if isinstance(cov_from, date) else None
        )
        stats["coverage_to"] = cov_to.isoformat() if isinstance(cov_to, date) else None
    return stats


def _declared_schema_for(
    schema_master: dict | None, folder: str, table_name: str
) -> dict[str, DTypeLike] | None:
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


def _rules_for(rules: dict | None, folder: str, table_name: str) -> dict:
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
    pos: int | None = None,
    total: int | None = None,
    paused_override: bool = False,
    anchor_date: date | None = None,
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
    query = table_cfg["query"]
    conn_name = table_cfg["connection"]
    folder = cfg["connections"][conn_name]["folder"]
    batch_rows = settings.get("polars", {}).get("batch_rows", 50_000)
    strict_cast = settings.get("polars", {}).get("strict_cast", True)
    tlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id, table=table_name)

    # PT-014: resolve pause state for this table. All intelligence
    # lives in dpl.lib.pause: folder-level block dominates table-level;
    # --paused-table lifts a table-level pause for THIS run only and
    # can never lift a folder-level one.
    paused_state, pause_norm, _was_lifted = lifted_by_override(
        table_cfg, cfg["connections"].get(conn_name), paused_override
    )
    pause_line = describe(paused_state, pause_norm) if pause_norm is not None else ""
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
            # old extract_all printed "[stg N/136] extracting..." before
            # each table; the counter is the table's fixed config
            # position, stable across retries and interleaved lanes.
            tlog.info("[%s %d/%d] extracting...", folder, pos, total)

        # PT-014: pause check runs FIRST - before the skip-check and
        # before the force branch can touch it. Needs no connection.
        # Pause outranks profile-force; only --paused-table lifts it,
        # and only for this manual run (the wrapper's flag rides the
        # same 'paused_override' path, absent for every cron nightly).
        if paused_state == PAUSED:
            tlog.info("%s", pause_line)
            paused_stats = _build_stats(
                table_cfg, load_date, "paused", anchor_date=anchor_date
            )
            paused_stats.update(pause_fields)
            return paused_stats

        if not force:
            existing_dir = _check_exists(
                settings, cfg, load_date, conn_name, table_name
            )
            if existing_dir:
                tlog.info("skipping (%s exists)", existing_dir)
                return _build_stats(
                    table_cfg, load_date, "skipped", anchor_date=anchor_date
                )

        # declared schema: the master CSV is the authority (M3/M5).
        # A missing-table ValueError is data-class: classification
        # refuses to retry it, the worker records the failure - same
        # net behavior as the old no-retry early return.
        declared = _declared_schema_for(schema_master, folder, table_name)

        rule = _rules_for(rules, folder, table_name)
        dedup_parsed = rule.get("dedup")
        sort_spec = rule.get("sort_spec") or []

        # PT-046 C3: bound BEFORE the block so static checkers never
        # see a possibly-unbound name (close() always assigns; the
        # None guard below remains the honest regression tripwire).
        wstats: dict | None = None
        with ParquetWriter(
            settings,
            source=folder,
            table=table_name.lower(),
            load_date=load_date,
            transforms=cfg.get("transforms", {}),
            dedup_columns=dedup_parsed,
            sort_spec=sort_spec,
            run_id=run_id,
        ) as w:
            for df in iter_batches(
                conn,
                query,
                batch_size=batch_rows,
                declared=declared,
                strict=strict_cast,
            ):
                w.append(df)
            wstats = w.close()
        if wstats is None:
            # regression guard: close() always returns a stats dict
            raise RuntimeError(
                f"internal error: writer returned no stats for "
                f"{folder}.{table_name} - close() was not called?"
            )
        tlog.info(
            "completed (%d rows, %.1fs%s)",
            wstats.get("rows_read", 0),
            time.time() - start,
            ", sorted" if wstats.get("sort_columns") else "",
        )
        return _build_stats(
            table_cfg,
            load_date,
            "extracted",
            rows_read=wstats.get("rows_read", 0),
            rows_written=wstats.get("rows_written", 0),
            duration_sec=time.time() - start,
            output_path=wstats.get("output_dir"),
            anchor_date=anchor_date,
        )

    return attempt


def prepare_run(
    cfg: dict,
    settings: dict,
    only_tables: list[str] | None = None,
    run_id: str = "-",
    paused_override: bool = False,
    only_connection: str | None = None,
    ignore_schedule: bool = False,
) -> dict[str, Any]:
    """Run head carved out of extract_all (Phase 4 C1).

    Guards, vault init, master/rules load and validation, table
    selection, folder grouping - everything that must happen BEFORE
    any connection opens. The wrapper (C2) calls this once per pipe,
    then schedules folders via extract_folder; extract_all calls both,
    serially, for the manual CLI path. Raises on startup problems
    exactly as the old inline head did.

    B4: `ignore_schedule` (from --ignore-schedule, Option B) bypasses
    the due-today decider for THIS run. Validation of the pairing
    (flag requires a selection) happens at the CLI/wrapper layer,
    which owns --tables/--connection; here it only travels.

    Returns the run context: started_at (ONE clock for the whole run),
    by_folder ({folder: [table_cfg, ...]} in config order),
    schema_master, rules.
    """
    rlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id)

    started_at = _utc_now_iso()

    # --- guards: fail loudly before opening anything ---
    validate_config_folders(cfg)
    table_collision_check(cfg)

    # secrets/ holds vault files only - refuse before any connection.
    validate_secrets_layout()

    # Vault init (PT-040 4a): the vault config comes from the ACTIVE
    # environment folder; init is idempotent, so the wrapper calling
    # oracle then ldap is fine.
    pass_mgr.init(resolve_environment().folder / "pass_mgr.yaml")

    # --- master CSV: load once, cross-check against config, then it rules ---
    schema_master = load_schema_master(_master_auto())
    validate_master_vs_config(cfg, schema_master, SOURCE_TYPE)
    rules = load_table_rules(_rules_csv())
    validate_rules_references(schema_master, rules)

    rlog.info("schema master loaded: %d table(s)", len(schema_master))

    # PT-014: every declared pause block, both levels, loudly
    # validated here (named errors; the yaml-load schema already
    # refused structural garbage - this adds the owner's name).
    validate_pause_declarations(cfg)

    # PT-032 (B1): schedule lives at CONNECTION level only. A
    # table-level schedule: is refused loudly (REQ-032-9) - feeds
    # with different cadences belong in separate connection
    # entries. Connection blocks themselves are validated when the
    # due-today decider runs (B3); here we guard the placement.
    for t in cfg["tables"]:
        if "schedule" in t:
            raise ValueError(
                f"table '{t['name']}' (connection "
                f"'{t['connection']}') declares a table-level "
                f"'schedule' - schedule is a connection-level key "
                f"(REQ-032-9); feeds with different cadences belong "
                f"in separate connection entries"
            )

    # PT-015 (B2): as_of + type/window_days validation. Universal
    # contract (operator ruling 2026-11-25: dev/test break-once is
    # DELIBERATE - production ships clean): every table declares
    # type; transaction tables declare window_days (= the width the
    # QUERY pulls); as_of is connection-level only (table-level is
    # refused). Absent as_of at connection level = T-0 + loud warning
    # per run (REQ-015-3) - the warning fires here, once per
    # connection per run, before extraction starts.
    for conn_name, conn_cfg in cfg["connections"].items():
        if conn_cfg.get("type") != SOURCE_TYPE:
            continue
        if "as_of" in conn_cfg:
            tctx = f"pipe oracle connection '{conn_name}'"
            asof.validate_as_of(conn_cfg["as_of"], tctx)
        else:
            rlog.warning(
                "connection '%s' declares no 'as_of' - assuming T-0 "
                "(live snapshot as of extraction); write 'as_of: T-0' "
                "in the yaml to silence this warning",
                conn_name,
            )
        # PT-032 (B3): every declared schedule block is grammar-
        # validated HERE, at startup, whether or not the connection is
        # due today - a malformed schedule must refuse the run loudly,
        # never silently disable a connection's cadence.
        if "schedule" in conn_cfg:
            schedule.validate_schedule_block(
                conn_cfg["schedule"], f"pipe oracle connection '{conn_name}'"
            )
    for t in cfg["tables"]:
        conn_name = t["connection"]
        conn_cfg = cfg["connections"].get(conn_name)
        if conn_cfg is None or conn_cfg.get("type") != SOURCE_TYPE:
            continue
        tctx = f"pipe oracle connection '{conn_name}' table '{t['name']}'"
        if "as_of" in t:
            raise ValueError(
                f"{tctx}: 'as_of' at table level is refused - as_of "
                f"is a CONNECTION-level key (REQ-015-10); a "
                f"connection's feeds share one lag (if a genuine "
                f"mixed-lag case appears, table-level as_of becomes "
                f"a DESIGNED extension)"
            )
        if "type" not in t:
            raise ValueError(
                f"{tctx}: table 'type' is REQUIRED (master | "
                f"transaction) - the taxonomy has no safe default; "
                f"the manifest coverage record depends on it"
            )
        table_type = asof.validate_table_type(t["type"], tctx)
        if table_type == "transaction":
            if "window_days" not in t:
                raise ValueError(
                    f"{tctx}: 'window_days' is REQUIRED for "
                    f"transaction tables - it declares the width "
                    f"the QUERY pulls and drives the manifest "
                    f"coverage statement (REQ-015-7)"
                )
            asof.validate_window_days(t["window_days"], tctx)
        elif "window_days" in t:
            raise ValueError(
                f"{tctx}: 'window_days' on a master table is "
                f"nonsensical - a snapshot has no pull window; "
                f"remove the key"
            )

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
                ", ".join(refused),
            )
            only_tables = [n for n in only_tables if n not in set(refused)]

    tables = [
        t
        for t in cfg["tables"]
        if cfg["connections"].get(t["connection"], {}).get("type") == SOURCE_TYPE
    ]

    # PT-039 (2026-11-20): --connection scoping, threaded from the
    # wrapper. Locked design: (a) INTERSECTION with --tables - a named
    # table NOT on the connection is refused loudly (typo) and
    # excluded; (b) a connection carrying a LIVE folder-level pause,
    # named without --paused-table, is refused loudly (same remedy
    # line as the --tables rule); (c) a refused connection means ZERO
    # jobs - the 2026-11-18 collapse rule, never the whole pipe. A
    # connection name this pipe simply doesn't have contributes
    # nothing quietly (the wrapper validated it against ALL pipes).
    if only_connection is not None:
        conn_cfg = cfg["connections"].get(only_connection)
        if conn_cfg is None:
            tables = []
        else:
            refused_conn = False
            if not paused_override:
                state, _ = state_for(None, conn_cfg)
                if state == PAUSED:
                    rlog.error(
                        "connection '%s' is paused and requested "
                        "without --paused-table - to extract it, rerun "
                        "with --paused-table (or remove its pause tag)",
                        only_connection,
                    )
                    refused_conn = True
            if refused_conn:
                tables = []
            else:
                if only_tables:
                    members = {
                        t["name"]
                        for t in cfg["tables"]
                        if t["connection"] == only_connection
                    }
                    outsiders = [n for n in only_tables if n not in members]
                    if outsiders:
                        rlog.error(
                            "table(s) %s are not on connection '%s' - "
                            "check the name(s) (typo?) or drop "
                            "--connection",
                            ", ".join(outsiders),
                            only_connection,
                        )
                        only_tables = [n for n in only_tables if n in members]
                tables = [t for t in tables if t["connection"] == only_connection]

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
        "ignore_schedule": ignore_schedule,
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

    The former folder-loop body, unchanged: one long-lived session
    (B1 worker), per-connection knobs, envelope with summary counts,
    manifest written into the archive. Table failures are recorded,
    never raised; siblings continue. A folder name not present in
    ctx['by_folder'] raises KeyError (the caller records it).
    """
    rlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id)
    folder_tables = ctx["by_folder"][folder]
    runs_root = settings["paths"]["runs_root"]

    # folder:connection is 1:1 (enforced by validate_config_folders),
    # so the folder's tables all share ONE connection - take it from
    # the first table and read its per-connection knobs.
    conn_name = folder_tables[0]["connection"]
    conn_cfg = cfg["connections"][conn_name]

    # PT-032 (B3): due-today decider. Today = the run's ONE calendar
    # date (load_date), fixed at run start. Absent schedule = due
    # every day (REQ-032-5). A non-due connection yields ZERO jobs -
    # the skip is RECORDED (per-table not_scheduled; paused wins the
    # status word per the composition ruling) and the manifest is
    # still written, same doctrine as all-paused exit-0.
    # PT-032 (B4): --ignore-schedule (Option B - always a deliberate,
    # named act) bypasses the decider for this run; forcing a
    # NON-DUE connection is stamped loudly in the log (and in the
    # envelope, below).
    conn_due = schedule.is_due_connection(conn_cfg, load_date)
    due = conn_due or bool(ctx.get("ignore_schedule"))
    # PT-015 (B5): anchor computed ONCE per folder - this is the
    # folder's one clock for coverage statements.
    as_of_declared = (
        conn_cfg["as_of"] if isinstance(conn_cfg.get("as_of"), str) else "T-0"
    )
    lag = asof.AS_OF_LAGS[as_of_declared]
    anchor_date = asof.anchor(load_date, lag)
    if not due:
        rlog.info(
            "[%s] not scheduled today (%s) - %d table(s) skipped "
            "with record, no session opened",
            folder,
            load_date.isoformat(),
            len(folder_tables),
        )
        results = []
        for t in folder_tables:
            state, _ = state_for(t, conn_cfg)
            status = "paused" if state == PAUSED else "not_scheduled"
            results.append(_build_stats(t, load_date, status, anchor_date=anchor_date))
        return _finish_envelope(
            ctx,
            folder,
            results,
            load_date,
            run_id,
            profile,
            runs_root,
            rlog,
            as_of_declared=as_of_declared,
            anchor_date=anchor_date,
        )
    overridden = bool(ctx.get("ignore_schedule")) and not conn_due
    if overridden:
        # B4: deliberate off-schedule pull - loud in the log AND the
        # manifest, so the records can never mistake it for a normal
        # scheduled extraction.
        rlog.warning(
            "[%s] NOT scheduled for %s - extracting anyway "
            "(--ignore-schedule; deliberate off-schedule pull)",
            folder,
            load_date.isoformat(),
        )

    keepalive = (settings.get("extraction") or {}).get("keepalive_minutes")
    table_conc = conn_cfg.get("table_concurrency", 1)
    retries = conn_cfg.get("retries", 2)
    backoff = conn_cfg.get("retry_backoff_seconds", 5)

    def factory(_cn=conn_name, _ka=keepalive):
        return connect(cfg, _cn, expire_time=_ka)

    rlog.info(
        "[%s] extracting %d table(s) (lanes=%d, retries=%d, keepalive=%s min)",
        folder,
        len(folder_tables),
        table_conc,
        retries,
        keepalive,
    )

    total_tables = len(folder_tables)
    tasks = [
        (
            t["name"],
            _task_for(
                cfg,
                settings,
                t,
                load_date,
                force,
                ctx["schema_master"],
                ctx["rules"],
                run_id,
                pos=pos,
                total=total_tables,
                paused_override=ctx.get("paused_override", False),
                anchor_date=anchor_date,
            ),
        )
        for pos, t in enumerate(folder_tables, start=1)
    ]

    outcomes = run_folder_tables(
        folder,
        tasks,
        factory,
        table_concurrency=table_conc,
        retries=retries,
        backoff_seconds=backoff,
    )

    results: list[dict[str, Any]] = []
    for t, oc in zip(folder_tables, outcomes):
        if oc["status"] == "ok":
            stats = oc["result"]
        else:
            # duration on the failure path is 0.0: the worker owns
            # the clock now, and duration_sec is volatile-stripped by
            # the equivalence comparator anyway.
            stats = _build_stats(
                t, load_date, "failed", error=oc["error"], anchor_date=anchor_date
            )
        stats["attempts"] = oc["attempts"]
        results.append(stats)

        if stats["status"] in FAILURE_STATUSES:
            rlog.warning(
                "[%s] %s failed after %d attempt(s) - continuing",
                folder,
                t["name"],
                stats["attempts"],
            )

    folder_failed = any(t["status"] in FAILURE_STATUSES for t in results)

    return _finish_envelope(
        ctx,
        folder,
        results,
        load_date,
        run_id,
        profile,
        runs_root,
        rlog,
        folder_failed,
        overridden,
        as_of_declared,
        anchor_date,
    )


def _finish_envelope(
    ctx: dict,
    folder: str,
    results: list[dict[str, Any]],
    load_date: date,
    run_id: str,
    profile: str,
    runs_root: str,
    rlog: Any,
    folder_failed: bool = False,
    schedule_overridden: bool = False,
    as_of_declared: str | None = None,
    anchor_date: date | None = None,
) -> dict[str, Any]:
    """Shared envelope tail for extract_folder (normal + not-due paths).

    Same assembly for every status vocabulary so history ingest and
    the comparator see ONE shape. B3 adds 'not_scheduled' to the
    operator counts (tables_not_scheduled mirrors tables_paused -
    the four-key mirror doctrine; counted when present).
    """
    pending_tables = [t["name"] for t in results if t["status"] in FAILURE_STATUSES]

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
        # PT-015 (B5): the folder's as-of story - the declared lag and
        # the computed anchor (run date - N). Both additive: absent in
        # manifests predating B5.
        "as_of": as_of_declared,
        "anchor_date": (anchor_date.isoformat() if anchor_date is not None else None),
        "tables": results,
        # operator summary counts (Part B request, 2026-11-14):
        # additive keys; history ingestion reads named fields only,
        # the comparator demands equality when both sides have them.
        "tables_total": len(results),
        "tables_extracted": sum(1 for r in results if r["status"] == "extracted"),
        "tables_skipped": sum(1 for r in results if r["status"] == "skipped"),
        "tables_paused": sum(1 for r in results if r["status"] == "paused"),
        "tables_failed": sum(1 for r in results if r["status"] in FAILURE_STATUSES),
        # PT-032 (B3): schedule-skip count - distinct vocabulary,
        # same additive-keys doctrine as the counts above.
        "tables_not_scheduled": sum(
            1 for r in results if r["status"] == "not_scheduled"
        ),
    }

    # PT-032 (B4): when --ignore-schedule forces THIS folder
    # off-schedule, the manifest carries schedule_overridden=True
    # (a due folder never carries the mark; additive-keys doctrine
    # keeps old manifests comparable).
    if schedule_overridden:
        envelope["schedule_overridden"] = True

    # THE manifest write - extractor-owned, into the archive.
    manifest_dir = runs_dir(runs_root, load_date, folder)
    write_manifest(manifest_dir, run_id, envelope)
    rlog.info("manifest written: %s/_manifest_%s.json", manifest_dir, run_id)

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
    ignore_schedule: bool = False,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Extract all Oracle tables; return (manifests_by_folder, any_failed).

    Thin shell over prepare_run + extract_folder (Phase 4 C1) - the
    manual CLI path. The wrapper's merged scheduler (C2) calls the
    two pieces separately. Signature and behavior unchanged: guards
    raise exactly as before; table failures are recorded, never
    raised; any_failed reflects any folder's partial_failure.
    """
    ctx = prepare_run(
        cfg,
        settings,
        only_tables=only_tables,
        run_id=run_id,
        paused_override=paused_override,
        ignore_schedule=ignore_schedule,
    )

    manifests: dict[str, dict[str, Any]] = {}
    for folder in ctx["by_folder"]:
        manifests[folder] = extract_folder(
            ctx,
            cfg,
            settings,
            load_date,
            folder,
            force=force,
            run_id=run_id,
            profile=profile,
        )

    any_failed = any(m["status"] == "partial_failure" for m in manifests.values())
    return manifests, any_failed


def main(run_id: str = "-") -> int:
    parser = argparse.ArgumentParser(description="Oracle ETL extractor")
    parser.add_argument(
        "--force", action="store_true", help="re-extract even if files exist"
    )
    parser.add_argument("--tables", help="comma-separated table names to extract")
    parser.add_argument(
        "--paused-table",
        action="store_true",
        help="with --tables naming paused table(s), "
        "lift their TABLE-level pause for this run",
    )
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help="with --tables naming the target, extract it "
        "TODAY even though its schedule says otherwise "
        "(deliberate off-schedule pull; stamped in log "
        "and manifest)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    from dpl.lib.environment import resolve_environment

    _env = resolve_environment()
    parser.add_argument(
        "-c",
        "--config",
        default=str(_env.folder / "extractors" / "oracle_to_stg.yaml"),
        help="pipe yaml (default: <env-folder>/extractors/oracle_to_stg.yaml)",
    )
    parser.add_argument(
        "--settings",
        default=str(_env.folder / "settings.yaml"),
        help="settings yaml (default: <env-folder>/settings.yaml)",
    )

    args = parser.parse_args()

    setup_logging()

    if args.verbose:
        log.setLevel(logging.DEBUG)  # bump only this component, not the tree

    try:
        cfg = load_config(args.config, ORACLE_CONFIG_SCHEMA)
        if cfg is None:
            raise RuntimeError(f"Failed to load {args.config}")

        settings = load_settings(args.settings)
        if settings is None:
            raise RuntimeError(f"Failed to load {args.settings}")
    except Exception as exc:  # noqa: BLE001 - top-level: any startup failure maps to exit code 1 with a logged cause
        log.critical("config/settings load failed: %s", exc)
        return 1

    only_tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    paused_override = bool(args.paused_table)
    if paused_override and not only_tables:
        log.error(
            "--paused-table modifies --tables selections only - "
            "rerun as: --tables <names> --paused-table"
        )
        return 3
    # PT-032 (B4): --ignore-schedule requires a NAMED target
    # (Option-B ruling); the extractor CLI has no --connection, so
    # here the target must come via --tables.
    ignore_schedule = bool(getattr(args, "ignore_schedule", False))
    _pair_err = schedule.validate_ignore_schedule_pairing(
        ignore_schedule, only_tables, None
    )
    if _pair_err is not None:
        log.error("%s", _pair_err)
        return 3
    load_date = date.today()

    # manual CLI run: derive a run_id (the wrapper passes ITS own in later,
    # via the run_id argument of main() -- default "-" kept for that hook)
    if run_id == "-":
        run_id = make_run_id(prefix="manual_")

    log.info(
        "starting Oracle extraction (force=%s, tables=%s, strict_cast=%s, run_id=%s)",
        args.force,
        only_tables,
        settings.get("polars", {}).get("strict_cast", True),
        run_id,
    )

    try:
        manifests, any_failed = extract_all(
            cfg,
            settings,
            load_date,
            force=args.force,
            only_tables=only_tables,
            run_id=run_id,
            profile="manual",
            paused_override=paused_override,
            ignore_schedule=ignore_schedule,
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
        extracted,
        skipped,
        paused,
        failed,
    )

    if any_failed:
        log.warning("Some tables failed - see logs for details")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
