# dpl/lib/generate_schemas.py
"""Bootstrap schema generator - introspect configured queries, emit master CSV.

Usage (from project root):
    python -m dpl.lib.generate_schemas                    # dry-run, show diff
    python -m dpl.lib.generate_schemas --force            # write master.csv
    python -m dpl.lib.generate_schemas --out foo.csv      # custom output path
    python -m dpl.lib.generate_schemas --force --dry-run  # show diff only

Exit codes:
    0 - success (new or updated master.csv written, or --dry-run)
    1 - some tables failed introspection (partial CSV emitted), or --force denied
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from pathlib import Path

from dpl.lib import pass_mgr
from dpl.lib.common import (
    SCHEMA_MASTER_COLUMNS,
    _read_registry_csv,
    load_config,
    load_settings,
    validate_secrets_layout,
)
from dpl.lib.logger import get_run_logger, setup_logging
from dpl.lib.ora_connect import ORACLE_CONFIG_SCHEMA, connect

log = get_run_logger("dpl.schema")


CONFIG_FILE = "config/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "config/settings.laptop.yaml"
OUTPUT_FILE = "config/extractors/schema_master_oracle_to_stg.csv"
SOURCE_TYPE = "oracle"
BACKUP_KEEP = 30

# ----------------------------------------------------------- type mapping
def oracle_dtype_to_comma_free(cursor_description_row: tuple,
                               raw_column_name: str) -> str:
    """Convert a cursor.description row to the comma-free dtype notation.

    description[1] is the type_code_int (old cx_Oracle style) or DbType
    object (modern oracledb). We handle both for maximum compatibility.

    Returns: "Int64", "Utf8", "Datetime", "Decimal(12.2)", "Float32", etc.
    """
    try:
        import oracledb
        has_dbtype = hasattr(oracledb, "DbType")
    except ImportError:
        has_dbtype = False

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


# ---------------------------------------------------------------- generator
def introspect_table(cfg: dict, table_cfg: dict, connection_name: str) -> list[dict[str, str]] | None:
    """Introspect one table/query, return list of row dicts for CSV output.

    Returns None if introspection fails (caller records and continues).
    Each dict has keys matching SCHEMA_MASTER_COLUMNS.
    """
    folder = cfg["connections"][connection_name]["folder"]
    table_name = table_cfg["name"]
    query = table_cfg["query"]
    table_key = f"{folder}.{table_name}"

    # Build the introspection query - wrap in SELECT * FROM (...) WHERE 1=0
    # to get metadata without fetching data
    introspection_query = f"SELECT * FROM ({query}) WHERE 1=0"

    start = time.time()
    try:
        with connect(cfg, connection_name) as conn:
            cur = conn.cursor()
            try:
                cur.execute(introspection_query)
                columns = []
                for idx, desc_row in enumerate(cur.description, 1):
                    col_name = desc_row[0].lower()
                    dtype = oracle_dtype_to_comma_free(desc_row, col_name)
                    columns.append({
                        "folder": folder,
                        "connection_name": connection_name,
                        "schema": table_cfg["schema"],
                        "table_name": table_name,
                        "column_seq": str(idx),
                        "column_name": col_name,
                        "dtype": dtype,
                        "description": "",
                        "policy_flags": "",
                    })

            finally:
                cur.close()
            
            duration = time.time() - start
            log.info(
                "introspected %d columns from %s (%.2fs)",
                len(columns), table_key, duration
            )
            return columns

    except Exception as exc:
        log.error(
            "INTROSPECTION FAILED for %s.%s (%s): %s",
            connection_name, table_name, table_key, exc
        )
        return None


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

    return {"added": added, "removed": removed, "changed": changed, "shifted":shifted}


def generate_schemas(config_path: str, output_path: str,
                     force: bool, dry_run: bool,
                     settings_path: str = SETTINGS_FILE) -> int:
    """Main generation routine.

    Returns: 0 success, 1 partial failure (some tables skipped).
    """
    log.info("loading config from %s", config_path)
    cfg = load_config(config_path, ORACLE_CONFIG_SCHEMA)
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
    pm_env = (settings.get("pass_mgr") or {}).get("environment")
    if not pm_env:
        log.critical(
            "settings file %s must declare the vault environment, e.g.:\n"
            "  pass_mgr:\n    environment: laptop", settings_path)
        return 1
    validate_secrets_layout()
    pass_mgr.init(pm_env)

    # Gather all tables of our SOURCE_TYPE
    tables = [
        t for t in cfg["tables"]
        if cfg["connections"].get(t["connection"], {}).get("type") == SOURCE_TYPE
    ]

    log.info("found %d tables of type '%s'", len(tables), SOURCE_TYPE)

    # Collect schema rows
    all_rows: list[dict[str, str]] = []
    failed_tables: list[str] = []

    for table_cfg in tables:
        conn_name = table_cfg["connection"]
        result = introspect_table(cfg, table_cfg, conn_name)
        if result is None:
            failed_tables.append(f"{conn_name}.{table_cfg['name']}")
        else:
            all_rows.extend(result)

    if failed_tables:
        log.warning("%d table(s) failed introspection - they are omitted:",
                    len(failed_tables))
        for ft in failed_tables:
            log.warning("  - %s", ft)

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
             len(all_rows), len(set(r["table_name"] for r in all_rows)))
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
    parser = argparse.ArgumentParser(
        description="Bootstrap schema master CSV from configured sources"
    )
    parser.add_argument(
        "--config", default=CONFIG_FILE,
        help=f"path to config.yaml (default: {CONFIG_FILE})"
    )
    parser.add_argument(
        "--out", default=OUTPUT_FILE,
        help=f"output path for master CSV (default: {OUTPUT_FILE})"
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
        "--settings", default=SETTINGS_FILE,
        help=f"path to settings yaml (default: {SETTINGS_FILE})"
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
                            settings_path=args.settings)


if __name__ == "__main__":
    sys.exit(main())
