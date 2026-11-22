# dpl/lib/generate_schemas.py
"""Schema generator - introspect configured queries, emit the master CSV.

Generic since PT-034: this module serves ANY registered connector type.
Type knowledge enters through exactly two seams, both explicit:

  1. INTROSPECTORS (below) - the per-type table mapping a connection
     type to its connect() and its dtype mapper. Adding a type (mssql,
     postgres) = adding ONE entry here. A type absent from this table
     fails LOUDLY for its pipe while other types continue - it never
     silently skips.
  2. CONNECTORS (dpl.lib.connectors) - decides WHETHER this module runs
     for a pipe at all (generator field) via schema_dispatch. This
     module never re-checks the registry; it trusts its caller.

Operator intent: every connection in the pipe yaml must declare
`schema: auto|manual`. Validation is aggregated up-front (all offenders
named in one message, nothing runs until declared). Connections marked
`manual` are skipped here with a loud info line - a legitimate opt-out.

Usage (from project root):
    python -m dpl.lib.generate_schemas                    # dry-run, show diff
    python -m dpl.lib.generate_schemas --force            # write master.csv
    python -m dpl.lib.generate_schemas --out foo.csv      # custom output path
    python -m dpl.lib.generate_schemas --force --dry-run  # show diff only

Exit codes:
    0 - success (new or updated master.csv written, or --dry-run)
    1 - config/declaration failure, some tables failed introspection
        (partial CSV emitted), or --force denied
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import oracledb

from dpl.lib import pass_mgr, schema_dispatch
from dpl.lib.common import (
    SCHEMA_MASTER_COLUMNS,
    _read_registry_csv,
    load_config,
    load_settings,
    validate_secrets_layout,
)
from dpl.lib.connectors import CONNECTORS
from dpl.lib.environment import resolve_environment
from dpl.lib.logger import get_run_logger, setup_logging
from dpl.lib.ora_connect import connect as oracle_connect
from dpl.lib.pause import PAUSED, state_for
from dpl.lib.session_pool import run_folder_tables

__func_msg__ = {
    "name": "generate-schemas",
    "invocation": "dpl generate-schemas",
    "category": "schema",
    "purpose": ("Introspect source schemas → master CSV (any registered connector type)"),
}

log = get_run_logger("dpl.schema")


# PT-040 4b: legacy manual-CLI defaults - kept ONLY as historical
# docstrings for the manual workflow; the resolved defaults come from
# the ACTIVE ENV FOLDER at runtime (see main()).
CONFIG_FILE = "<env-folder>/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "<env-folder>/settings.yaml"
OUTPUT_FILE = "<env-folder>/extractors/schema_master_oracle_to_stg.csv"
BACKUP_KEEP = 30

# ----------------------------------------------------------- type mapping
def oracle_dtype_to_comma_free(cursor_description_row: Any,
                               raw_column_name: str) -> str:
    """Convert a cursor.description row to the comma-free dtype notation.

    description[1] is the type_code_int (old cx_Oracle style) or DbType
    object (modern oracledb). We handle both for maximum compatibility.

    Returns: "Int64", "Utf8", "Datetime", "Decimal(12.2)", "Float32", etc.
    """
    has_dbtype = hasattr(oracledb, "DbType")

    name = raw_column_name.lower()
    type_info = cursor_description_row[1]

    # Modern oracledb.DbType objects
    if has_dbtype and isinstance(type_info, oracledb.DbType):
        type_name = str(type_info).upper()

        if "NUMBER" in type_name.upper():
            precision = cursor_description_row[4]
            scale = cursor_description_row[5]
            if precision is None or scale is None:
                return "Float64"
            if precision == 126 and scale == -127:
                return "Float64"
            if scale < 0:
                return "Float64"
            if scale == 0:
                return "Int64" if precision <= 18 else "Decimal(38.0)"
            return f"Decimal({precision}.{scale})"
        elif "FLOAT" in type_name.upper():
            return "Float64"
        elif "BINARY_FLOAT" in type_name.upper():
            return "Float32"
        elif "BINARY_DOUBLE" in type_name.upper():
            return "Float64"
        elif "DATE" in type_name.upper() or "TIME" in type_name.upper():
            return "Datetime"
        elif "VARCHAR" in type_name.upper() or "CHAR" in type_name.upper() or "CLOB" in type_name.upper() or "NCLOB" in type_name.upper() or "LONG" in type_name.upper() or "XMLTYPE" in type_name.upper():
            return "Utf8"
        elif "BLOB" in type_name.upper():
            return "Binary"
        else:
            log.warning(
                "Unknown Oracle type '%s' for column '%s' - defaulting to Utf8. "
                "Please add a policy flag if special handling is needed.",
                type_name, name
            )
            return "Utf8"

    # Legacy integer type_codes (cx_Oracle style)
    type_code = int(type_info) if type_info else 0

    # Oracle type_code constants (approximate; driver-dependent)
    # Most drivers map NUMBER as 2, VARCHAR as 1, DATE as 12
    if type_code in (1, 9, 13):  # VARCHAR2, NVARCHAR2, CHAR
        return "Utf8"
    elif type_code == 2:  # NUMBER
        precision = cursor_description_row[4] or 0
        scale = cursor_description_row[5] or 0
        if precision > 0 and scale == 0:
            return "Int64"
        elif precision > 0:
            return f"Decimal({precision}.{scale})"
        else:
            return "Decimal"
    elif type_code == 4:  # FLOAT
        return "Float64"
    elif type_code == 8:  # DATE/TIMESTAMP
        return "Datetime"
    elif type_code == 100:  # BLOB
        return "Binary"
    else:
        log.warning(
            "Unrecognized type_code %d for column '%s' - defaulting to Utf8.",
            type_code, name
        )
        return "Utf8"

INTROSPECTORS: dict[str, dict[str, Callable]] = {
    "oracle": {
        "connect": oracle_connect,
        "dtype_mapper": oracle_dtype_to_comma_free,
    },
}

# ---------------------------------------------------------------- generator
def _introspect_task(cfg: dict, table_cfg: dict, dtype_mapper: Callable):
    """Build the per-table introspection task for the session worker.

    The returned callable takes the LIVE connection from the worker and
    performs exactly ONE attempt. Returns the CSV row dicts on success;
    RAISES on any failure - the worker classifies (transport vs data)
    and retries per the connection's configured knobs.

    Args:
        cfg: parsed pipe yaml.
        table_cfg: one entry of the pipe yaml's `tables:` list.
        dtype_mapper: the type's cursor-description converter, from
            INTROSPECTORS. Per-type dispatch enters HERE - the query
            wrapping and cursor protocol below are type-neutral.
    """
    conn_name = table_cfg["connection"]
    folder = cfg["connections"][conn_name]["folder"]
    table_name = table_cfg["name"]
    query = table_cfg["query"]
    table_key = f"{folder}.{table_name}"
    introspection_query = f"SELECT * FROM ({query}) WHERE 1=0"

    def attempt(conn):
        start = time.time()
        cur = conn.cursor()
        try:
            cur.execute(introspection_query)
            columns = []
            for idx, desc_row in enumerate(cur.description or (), 1):
                col_name = desc_row[0].lower()
                columns.append({
                    "folder": folder,
                    "connection_name": conn_name,
                    "schema": table_cfg["schema"],
                    "table_name": table_name,
                    "column_seq": str(idx),
                    "column_name": col_name,
                    "dtype": dtype_mapper(desc_row, col_name),
                    "description": "",
                    "policy_flags": "",
                })
        finally:
            cur.close()
        log.info("introspected %d columns from %s (%.2fs)",
                 len(columns), table_key, time.time() - start)
        return columns

    return attempt


def read_existing_master(path: Path) -> dict[str, dict[str, str]]:
    """Read existing master CSV into {folder.table.column -> metadata}.

    Reuses common._read_registry_csv: '#' comment lines, blank lines,
    exact header, and ragged-row errors are handled identically to the
    extractor's read of the same file. Returns empty dict if the file
    doesn't exist.
    """
    if not path.exists():
        return {}

    rows = _read_registry_csv(path, SCHEMA_MASTER_COLUMNS, required=False)
    if rows is None:
        return {}

    result = {}
    for _lineno, cells in rows:
        (folder, conn, schema, table, seq, column, dtype,
         description, flags) = cells
        key = f"{folder}.{table}.{column}".lower()
        result[key] = {
            "dtype": dtype,
            "column_seq": seq,
            "description": description,
            "policy_flags": flags,
            "connection": conn,
            "schema": schema,
        }
    return result


def diff_master(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list[str]]:
    """Compare old/new master, categorize changes.

    Returns {"added": [...], "removed": [...], "changed": [...], "shifted": [...]}
    """
    old_keys = set(old.keys())
    new_keys = set(new.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = []
    shifted = []
    for key in old_keys & new_keys:
        if old[key]["dtype"] != new[key]["dtype"]:
            changed.append(key)
        else:
            old_seq, new_seq = old[key]["column_seq"], new[key]["column_seq"]
            if old_seq and new_seq and old_seq != new_seq:
                shifted.append(f"{key} (seq {old[key]['column_seq']}->{new[key]['column_seq']})")
            old_seq, new_seq = old[key]["connection"], new[key]["connection"]
            if old_seq and new_seq and old_seq != new_seq:
                shifted.append(f"{key} (connection {old[key]['connection']}->{new[key]['connection']})")
            old_seq, new_seq = old[key]["schema"], new[key]["schema"]
            if old_seq and new_seq and old_seq != new_seq:
                shifted.append(f"{key} (schema {old[key]['schema']}->{new[key]['schema']})")

    return {"added": added, "removed": removed, "changed": changed, "shifted": shifted}


def generate_schemas(config_path: str, output_path: str,
                     force: bool, dry_run: bool,
                     settings_path: str | None = None,
                     source_type: str | None = None, 
                     paused_override: bool = False) -> int:
    """Main generation routine for ONE pipe (single connector type).

    Args:
        source_type: the pipe's single connection type. Injected by
            run.py (from Pipe.source_type) and by tests - in that case
            NO file peek happens and the yaml loader enforces the
            schema tag. None (manual CLI path): the type is derived
            by a raw peek into the yaml, with the aggregated
            fail-loud tag check running BEFORE anything loads.

    Order of operations, each loud:
      1. type resolution - injected, or peeked (CLI) with aggregated
         tag validation first;
      2. introspector lookup - absent type fails loudly, exit 1;
      3. per-connection processing - manual connections skip loudly,
         auto connections introspect, table failures are loud and
         leave the run in exit-1 (partial) state.

    Returns: 0 success, 1 partial failure or declaration error.
    """
    # PT-040 4b: no explicit settings path -> the ACTIVE env folder's
    settings_path = settings_path or str(
        resolve_environment().folder / "settings.yaml")

    # ---- resolve the pipe's single connection type
    if source_type is None:
        # CLI path: no caller to declare the type - peek the yaml.
        # Aggregated tag check first: ALL offenders named in one message.
        import yaml  # local import: peek-only dependency
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except OSError as exc:
            log.critical("cannot read config %s: %s", config_path, exc)
            return 1
        conns = raw.get("connections") or {}
        schema_dispatch.require_schema_tags(conns)
        types = {str((c or {}).get("type", "")).lower() for c in conns.values()}
        if len(types) != 1:
            log.critical(
                "%s: expected exactly one connection type, found %s - one "
                "extractor yaml serves one connector type; split the file.",
                config_path, sorted(types))
            return 1
        source_type = types.pop()

    stype = source_type

    intro = INTROSPECTORS.get(stype)
    if intro is None:
        log.critical(
            "no introspector registered for connection type '%s' - "
            "register it in INTROSPECTORS (dpl/lib/generate_schemas.py) "
            "or declare schema: manual on its connections.", stype)
        return 1

    log.info("loading config from %s", config_path)
    cfg = load_config(config_path, CONNECTORS[stype]["schema"])
    if cfg is None:
        log.critical("Config load failed: %s", config_path)
        return 1

    # Validate config structure (folder_uniqueness already checked by schema)
    from dpl.lib.common import validate_config_folders
    validate_config_folders(cfg)

    # Vault-backed logins: settings name the pass_mgr environment; init
    # before connect() (introspection opens real connections).
    settings = load_settings(settings_path)
    if settings is None:
        log.critical("settings load failed: %s", settings_path)
        return 1
    validate_secrets_layout()
    # PT-040 4a: the vault config comes from the ACTIVE environment folder.
    pass_mgr.init(resolve_environment().folder / "pass_mgr.yaml")

    connect_fn = intro["connect"]
    dtype_mapper = intro["dtype_mapper"]

    # ---- gate 2: per-connection screening - manual skip AND foreign-type
    # exclusion. run.py already refuses mixed-type yamls at startup, so a
    # foreign type here means a bypassed caller - exclude loudly, never
    # introspect it. Defense-in-depth, per PT-001-style doctrine.
    tables = []
    excluded_foreign: list[str] = []
    paused_count = 0
    for t in cfg["tables"]:
        conn_cfg = cfg["connections"].get(t["connection"], {})
        if conn_cfg.get("schema") == "manual":
            log.info(
                "connection '%s': schema=manual - skipping schema "
                "generation for its tables (hand-curated master).",
                t["connection"])
            continue
        # PT-014: paused tables skip introspection SILENTLY on scheduled
        # runs (design 2026-11-18): the pause is already announced loudly
        # by the extractor's arithmetic lines and manifest/history records;
        # a third announcement would be noise. Manual --paused-table runs
        # pass paused_override=True, so the table gets FULL service here.
        if not paused_override:
            _st, _norm = state_for(t, cfg["connections"].get(t["connection"]))
            if _st == PAUSED:
                paused_count += 1
                continue
        if str(conn_cfg.get("type", "")).lower() != stype:
            excluded_foreign.append(f"{t['connection']}.{t['name']}")
            continue
        tables.append(t)

    if excluded_foreign:
        log.warning(
            "EXCLUDED foreign-type table(s) not matching this pipe's "
            "type '%s': %s - run.py should have refused this config; "
            "investigate how it reached the generator.",
            stype, ", ".join(sorted(excluded_foreign)))

    log.info("found %d tables of type '%s'", len(tables), stype)
    if not tables:
        # PT-014: a pipe where EVERY table is paused is a healthy,
        # expected state - keep the master unchanged, open no
        # connection, exit 0 quietly ("no complaints" ruling,
        # 2026-11-18). All-manual remains the old loud exit 1.
        if paused_count and paused_count == len(cfg["tables"]):
            log.info("all %d table(s) paused - master left unchanged, "
                     "no connection opened", paused_count)
            return 0
        log.critical(
            "no tables left to introspect - every connection declared "
            "schema: manual (or the yaml declares none). Nothing to do.")
        return 1

    # Group by connection, preserving config order within each group -
    # one session per connection, shared across that connection's tables.
    groups: dict[str, list[dict]] = {}
    for t in tables:
        groups.setdefault(t["connection"], []).append(t)
    log.info("%d table(s) across %d connection(s)",
             len(tables), len(groups))

    all_rows: list[dict[str, str]] = []
    failed_tables: list[str] = []

    for conn_name, conn_tables in groups.items():
        conn_cfg = cfg["connections"][conn_name]
        retries = conn_cfg.get("retries", 2)
        backoff = conn_cfg.get("retry_backoff_seconds", 5)

        def factory(_cn=conn_name):
            return connect_fn(cfg, _cn)

        tasks = [(t["name"], _introspect_task(cfg, t, dtype_mapper))
                 for t in conn_tables]

        outcomes = run_folder_tables(
            conn_name, tasks, factory,
            table_concurrency=1,        # metadata calls: parallelism buys nothing
            retries=retries,
            backoff_seconds=backoff)

        for t, oc in zip(conn_tables, outcomes):
            if oc["status"] == "ok":
                all_rows.extend(oc["result"])
            else:
                failed_tables.append(f"{conn_name}.{t['name']}")
                log.error("INTROSPECTION FAILED for %s.%s: %s",
                          conn_name, t["name"], oc["error"])

    if not all_rows:
        log.critical("No tables succeeded - nothing to write.")
        return 1

    # Build new master for diffing
    new_master = {}
    for r in all_rows:
        key = f"{r['folder']}.{r['table_name']}.{r['column_name']}".lower()
        new_master[key] = {
            "dtype": r["dtype"],
            "description": r["description"],
            "policy_flags": r["policy_flags"],
            "column_seq": r["column_seq"],
            "connection": r["connection_name"],
            "schema": r["schema"],
            }

    # Check existing file
    out_path = Path(output_path)
    existing = read_existing_master(out_path)
    diff = diff_master(existing, new_master)

    if diff["added"]:
        log.warning("NEW columns: %d", len(diff["added"]))
    if diff["removed"]:
        log.warning("REMOVED columns: %d", len(diff["removed"]))
    if diff["changed"]:
        log.warning("CHANGED dtypes: %d", len(diff["changed"]))
    if diff["shifted"]:
        log.warning("Columns shifted in sequence: %d", len(diff["shifted"]))
        for s in diff["shifted"][:5]:
            log.warning("   - %s", s)
        if len(diff["shifted"]) > 5:
            log.warning("   ... and %d more", len(diff["shifted"]) - 5)

    if existing and not diff["added"] and not diff["removed"] and not diff["changed"]:
        log.info("No changes - master CSV already current.")
        if dry_run:
            return 0
        # Without --force, refuse to overwrite identical file
        if not force:
            log.info("Existing master is current. Use --force to overwrite anyway, "
                     "or --dry-run to preview.")
            return 0

    # Check for destructive changes
    if diff["removed"] and not force and not dry_run:
        log.warning(
            "This generation would REMOVE %d columns. "
            "Use --force to proceed, or investigate if this is expected drift.",
            len(diff["removed"])
        )
        log.info("Columns to be removed:")
        for c in diff["removed"]:
            log.warning("  - %s", c)
        return 1

    # Dry-run mode
    if dry_run:
        log.info("--- DRY-RUN MODE - no file written ---")
        log.info("Rows to be written: %d", len(all_rows))
        return 0

    # Write CSV (require --force if overwriting)
    if existing and not force:
        log.error(
            "Output file exists. Use --force to overwrite. "
            "Or use --dry-run to preview changes."
        )
        return 1

    # Write
    log.info("Writing master CSV to %s", out_path)
    # Write: temp file first, then rotate backup, then atomic replace
    tmp_path = out_path.with_suffix(".csv.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        fh.write("# generated - do not edit by hand "
                 "(regenerate: python -m lib.generate_schemas --force)\n")
        writer = csv.DictWriter(
            fh,
            fieldnames=SCHEMA_MASTER_COLUMNS,
            extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    if existing and (diff["added"] or diff["removed"] or diff["changed"]):                               # content differs - that's why we're here
        backup = backup_and_rotate(out_path)
        log.info("previous master backed up to %s", backup)

    tmp_path.replace(out_path)                 # atomic same-FS move


    log.info("Schema master generated: %d rows, %d table(s)",
             len(all_rows), len({r["table_name"] for r in all_rows}))
    return 1 if failed_tables else 0

def _timestamp_name(base: str = "schema_master") -> str:
    return f"{base}_{time.strftime('%Y%m%d_%H%M%S')}.csv"

def backup_and_rotate(out_path: Path, keep: int = BACKUP_KEEP) -> Path | None:
    """Copy current master to backups/ with timestamp; prune old copies.

    Assumes out_path exists and differs from what will replace it - caller
    only invokes this on a real content change. Returns backup path or None.
    """
    if not out_path.exists():
        return None

    backup_dir = out_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / _timestamp_name(out_path.stem)
    shutil.copy2(out_path, backup)          # copy2 preserves mtime - sortable

    # prune: keep newest `keep` by name (timestamp prefix sorts lexicographically)
    backups = sorted(backup_dir.glob(f"{out_path.stem}_*.csv"))
    for old in backups[:-keep] if keep > 0 else backups:
        old.unlink()
    return backup

# ---------------------------------------------------------------------- main
def main() -> int:
    """CLI entry - kept Oracle-defaulted for convenient manual runs.

    run.py never uses this path: it calls generate_schemas() directly
    with the pipe's own config and master path. The --config/--out
    defaults merely preserve the historical manual workflow.
    """
    parser = argparse.ArgumentParser(
        description="Bootstrap schema master CSV from configured sources"
    )
    env = resolve_environment()
    default_config = str(env.folder / "extractors" / "oracle_to_stg.yaml")
    default_out = str(
        env.folder / "extractors" / "schema_master_oracle_to_stg.csv")
    parser.add_argument(
        "--config", default=default_config,
        help=f"path to config.yaml (default: {default_config})"
    )
    parser.add_argument(
        "--out", default=default_out,
        help=f"output path for master CSV (default: {default_out})"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite existing file without prompting"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show diff without writing"
    )
    parser.add_argument(
        "--settings",
        default=str(env.folder / "settings.yaml"),
        help=f"settings yaml (default: {env.folder / 'settings.yaml'})"
    )
    parser.add_argument(
        "--paused-table", action="store_true",
        help="PT-014: introspect paused tables too (manual pull of a "
             "paused table needs fresh schema truth)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="verbose logging"
    )

    args = parser.parse_args()

    setup_logging()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    log.info("starting schema generator (dry_run=%s, force=%s)",
             args.dry_run, args.force)

    return generate_schemas(args.config, args.out, args.force, args.dry_run,
                            settings_path=args.settings,
                            paused_override=args.paused_table)


if __name__ == "__main__":
    sys.exit(main())
