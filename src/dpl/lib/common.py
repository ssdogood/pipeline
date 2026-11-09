# dpl/lib/common.py
"""
Shared utilities: config loading, vault-backed credentials, manifests, paths.
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from schema import And, Optional, Schema, SchemaError

from dpl.lib.logger import get_run_logger
from dpl.lib.paths import get_project_root

log = get_run_logger()

# ------------------------------------------------------------------ paths
PROJECT_ROOT = get_project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
SECRET_DIR = PROJECT_ROOT / "secrets"

VALID_COMPRESSIONS = frozenset(["zstd", "snappy"])

RETIRED_TABLE_KEYS = ("dedup_columns",)

# Terminal statuses that count as failure for manifest/exit-code purposes.
FAILURE_STATUSES = frozenset({"failed"})

# ------------------------------------------------------------------- schema helpers

def _valid_folder_segment(folder: str) -> str:
    """Validate that `folder` is a safe single path segment."""
    if not isinstance(folder, str):
        raise SchemaError("folder must be a string")
    if not folder or folder.strip() == "":
        raise SchemaError("folder cannot be empty")
    if "/" in folder or "\\" in folder:
        raise SchemaError("folder cannot contain path separators (/ or \\)")
    if ".." in folder or folder.startswith(".") or folder.endswith("."):
        raise SchemaError("folder cannot contain '..' or start/end with '.'")
    if folder.lower().startswith(("http://", "https://", "ftp://")):
        raise SchemaError("folder cannot be a URL")
    return folder

# ------------------------------------------------------------------- schemas
SETTINGS_SCHEMA = Schema({
    Optional("polars"): {
        Optional("batch_rows", default=50000): And(int, lambda n: n > 0),
        Optional("strict_cast", default=True): bool,
        Optional(str): object
    },
    # Which pass_mgr.<environment>.yaml the vault layer reads at runtime.
    # Optional in the schema (minimal test fixtures stay loadable) but the
    # extractors REQUIRE it - missing key = clean refusal at startup.
    Optional("pass_mgr"): {
        "environment": And(str, len),
        Optional(str): object
    },
    "paths": {                          # mandatory per settings.yaml docs
        "landing_root": And(str, len),
        "scratch_root": And(str, len),
        # runs_root: manifest archive root. Optional; the derived default
        # (<landing_root>/_runs) is applied by normalize_settings() AFTER
        # validation - it needs landing_root, which the schema doesn't
        # see at validate time. A misspelled sibling key is NOT this key:
        # it falls into Optional(str) and is ignored (known limitation,
        # see settings.yaml DESIGN RULES note 3).
        Optional("runs_root"): And(str, len),
        Optional("history_db"): And(str, len),
        Optional(str): object
    },
    "duckdb": {                         # mandatory: memory_limit is required
        "memory_limit": And(str, lambda s: len(s.strip()) > 0),  # e.g. "10GB"
        Optional("threads"): And(int, lambda n: n > 0),
        Optional(str): object
    },
    Optional("writer"): {              # section optional: every child has a default
        Optional("compression", default="zstd"): And(
            str, lambda s: s.lower() in VALID_COMPRESSIONS
        ),
        Optional("compression_level"): And(
            int, lambda n: 1 <= n <= 22,  # DuckDB 1.4.5 documented range
            error="compression_level must be 1–22 for zstd (DuckDB 1.4.5 default: 3)"
        ),
        Optional("max_file_mb", default=512): And(int, lambda n: n > 0),
        Optional("keep_scratch_on_success", default=False): bool,
        Optional(str): object
    }
})

def normalize_settings(settings: dict) -> dict | None:
    """Post-validation fix-ups. Currently: derive runs_root default.

    runs_root omitted -> <landing_root>/_runs (sibling of the landing
    date-dirs, inside the staging root - the prune-safety contract in
    _prune_date_dirs depends on this location staying non-date-named).
    """
    if settings is None:
        return None
    paths = settings.get("paths", {})
    if not paths.get("runs_root"):
        paths["runs_root"] = str(Path(paths["landing_root"]) / "_runs")
    return settings


def runs_dir(runs_root: str | Path, load_date: date, folder: str) -> Path:
    """Manifest archive path: <runs_root>/<date>/<folder> (mirrors landing)."""
    return Path(runs_root) / load_date.isoformat() / folder

# ------------------------------------------------------------------- dedup
def parse_dedup_spec(spec: Any) -> dict:
    """Parse a dedup_columns value into a structured mode description.

    Supported forms (from config.yaml):
        None / omitted            -> {"mode": "none"}
        "none" / "NONE" (scalar)  -> {"mode": "none"}
        "all"  / "ALL"  (scalar)  -> {"mode": "all"}
        [a, b]                    -> {"mode": "include", "columns": [a, b]}
        [-a, -b]                  -> {"mode": "exclude", "columns": [a, b]}

    Rules:
      - Column names are NOT validated here (they can only be checked against
        the actual result set at write time - that's the writer's job).
      - Exclude mode is all-or-nothing: EVERY entry must start with '-'.
        Mixing positive and negative entries raises ValueError.
      - Scalar keywords are case-insensitive and normalize to lowercase.
      - A scalar that isn't a recognized keyword raises ValueError (catches
        typos like "al", "everything", "*").

    Returns a dict with:
        mode: "none" | "all" | "include" | "exclude"
        columns: normalized (lowercased, '-' stripped) list - empty for
                 "none"/"all"
    """
    if spec is None:
        return {"mode": "none", "columns": []}

    # --- scalar keyword ---
    if isinstance(spec, str):
        keyword = spec.strip().lower()
        if keyword == "none":
            return {"mode": "none", "columns": []}
        if keyword == "all":
            return {"mode": "all", "columns": []}
        raise ValueError(
            f"dedup_columns: unrecognized value '{spec}'. Use 'none', 'all', "
            f"a list of column names, or a list of '-column' exclusions."
        )

    # --- list form ---
    if isinstance(spec, list):
        if not spec:
            return {"mode": "none", "columns": []}

        entries = [str(c).strip() for c in spec]
        if any(not c for c in entries):
            raise ValueError("dedup_columns: empty entry in column list")

        negatives = [c for c in entries if c.startswith("-")]
        positives = [c for c in entries if not c.startswith("-")]

        if negatives and positives:
            raise ValueError(
                "dedup_columns: cannot mix positive and negative entries. "
                f"Got {positives} and {negatives}. Use all plain columns "
                "or all '-column' exclusions."
            )

        if negatives:
            columns = [c[1:].strip() for c in negatives]
            if any(not c for c in columns):
                raise ValueError(
                    "dedup_columns: exclusion entry with no column name "
                    "(bare '-')"
                )
            return {"mode": "exclude", "columns": [c.lower() for c in columns]}

        return {"mode": "include", "columns": [c.lower() for c in positives]}

    raise ValueError(
        f"dedup_columns must be a keyword ('none'/'all') or a list of "
        f"column names, got {type(spec).__name__}"
    )

# ------------------------------------------------------------------- sort
def parse_sort_spec(cell: str | None) -> list[tuple[str, str]]:
    """Parse a sort_order cell into [(column, 'asc'|'desc'), ...].

    Grammar (comma-free, '|' separated):
        updated_at           -> [("updated_at", "asc")]
        updated_at-          -> [("updated_at", "desc")]   trailing '-'
        updated_at+          -> [("updated_at", "asc")]    '+' = optional sugar
        updated_at-|id       -> [("updated_at", "desc"), ("id", "asc")]
        ''                   -> []                        no sort

    Rules:
      - Position = precedence (first token = primary sort key)
      - Columns are lowercased; trailing sign stripped
      - Malformed tokens raise ValueError: '--', '+-', bare '-', empty
      - Note: '-' on a sort token is TRAILING (desc); a LEADING '-' on a
        dedup token means exclusion - never mix the conventions.
    """
    if not cell or not cell.strip():
        return []

    result: list[tuple[str, str]] = []
    for token in cell.split(CELL_SEPARATOR):
        tok = token.strip()
        if not tok:
            raise ValueError(
                f"sort_order: empty token in '{cell}' "
                f"(consecutive '|' or stray whitespace)")

        direction = "asc"
        if tok.endswith("-"):
            direction = "desc"
            tok = tok[:-1].strip()
        elif tok.endswith("+"):
            tok = tok[:-1].strip()

        if not tok:
            raise ValueError(
                f"sort_order: bare direction sign in '{token}' "
                f"(no column name)")
        if tok.endswith("-") or tok.endswith("+"):
            # catches 'col--' and 'col-+' after the first strip
            raise ValueError(
                f"sort_order: malformed token '{token}' "
                f"(multiple trailing signs)")

        result.append((tok.lower(), direction))
    return result

# ------------------------------------------------- CSV registries (M1)
SCHEMA_MASTER_COLUMNS = ["folder", "connection_name", "schema", 
                         "table_name", "column_seq", "column_name",
                         "dtype", "description", "policy_flags"]
TABLE_RULES_COLUMNS = ["folder", "table_name", "dedup_columns", "sort_order"]
CELL_SEPARATOR = "|"          # multi-value cells: never comma (see spec D3.2)

PENDING_FILE = CONFIG_DIR / "_pending.yaml"

def _read_registry_csv(path: str | Path, expected_header: list[str],
                       required: bool) -> list[tuple[int, list[str]]] | None:
    """Read a registry CSV into (lineno, cells) tuples.

    - '#' as the FIRST non-blank character of a line marks a comment (D3.2)
    - blank lines ignored
    - header must match expected_header exactly (case-insensitive)
    - wrong-width rows raise with line number - no silent truncation
    """
    p = Path(path)
    if not p.exists():
        if required:
            raise FileNotFoundError(
                f"Registry file not found: {p} "
                f"(expected header: {expected_header})")
        return None

    rows: list[tuple[int, list[str]]] = []
    header_seen = False
    with p.open(newline="", encoding="utf-8") as fh:
        for lineno, raw in enumerate(csv.reader(fh), 1):
            cells = [c.strip() for c in raw]
            if not any(cells) or cells[0].startswith("#"):
                continue
            if not header_seen:
                if [c.lower() for c in cells] != expected_header:
                    raise ValueError(
                        f"{p}:{lineno}: header must be {expected_header}, "
                        f"got {cells}")
                header_seen = True
                continue
            if len(cells) != len(expected_header):
                raise ValueError(
                    f"{p}:{lineno}: expected {len(expected_header)} cells, "
                    f"got {len(cells)}: {cells} "
                    f"(hint: does a value contain a comma? cell values must be comma-free - use '.' inside dtype notation, "
                    f"e.g. Decimal(12.2) not Decimal(12,2), and '|' between multi-values)")
            rows.append((lineno, cells))
    if not header_seen:
        raise ValueError(f"{p}: no header row found")
    return rows

def _parse_flags(cell: str) -> dict:
    """Parse 'k=v|k2|k3=v3' policy_flags into a dict."""
    if not cell:
        return {}
    flags: dict = {}
    for part in cell.split(CELL_SEPARATOR):
        if "=" in part:
            k, v = part.split("=", 1)
            flags[k.strip().lower()] = v.strip()
        elif part:
            flags[part.lower()] = True
    return flags

def _check_master_row(path, lineno, cells):
    (folder, conn, schema, table, seq, column, dtype, *_rest) = cells
    if not (folder and conn and schema and table and column and dtype):
        raise ValueError(
            f"{path}:{lineno}: table-level identity cells cannot be empty")
    try:
        int(seq)
    except (TypeError, ValueError):
        raise ValueError(f"{path}:{lineno}: column_seq must be an integer")

def _insert_master_row(master, path, lineno, folder, conn, schema, table,
                       seq, column, dtype, description, flags):
    key = f"{folder}.{table}".lower()
    entry = master.setdefault(key, {
        "columns": {}, "descriptions": {}, "flags": {},
        "connection": conn, "schema": schema})
    col = column.lower()
    if col in entry["columns"]:
        raise ValueError(
            f"{path}:{lineno}: duplicate column '{col}' for '{key}'")
    entry["columns"][col] = dtype.lower()
    entry["descriptions"][col] = description or None
    entry["flags"][col] = _parse_flags(flags)

def load_schema_master(auto_path: str | Path,
                        ad_path: str | Path | None = None) -> dict:
    """Load and merge auto (generated) + ad (hand-maintained) master CSVs.

    auto_path is required. ad_path optional (absent -> no AD tables).
    STRICT RULE: a table key may exist in ONE file only. Duplication
    raises - it means two owners for one table.

    Returns: { "<folder>.<table>" -> {"columns": {...},
               "descriptions": {...}, "flags": {...},
               "connection": str, "schema": str} }
    """
    master = {}
    # -- auto file --
    rows = _read_registry_csv(auto_path, SCHEMA_MASTER_COLUMNS, required=True)
    assert rows is not None

    for lineno, cells in rows:
        (folder, conn, schema, table, seq, column, dtype, description, flags) = cells
        _check_master_row(auto_path, lineno, cells)
        _insert_master_row(master, auto_path, lineno, folder, conn, schema,
                           table, seq, column, dtype, description, flags)

    # -- ad file: merge with disjointness check --
    if ad_path is not None:
        ad_rows = _read_registry_csv(ad_path, SCHEMA_MASTER_COLUMNS,
                                     required=False)
        if ad_rows is not None:
            for lineno, cells in ad_rows:
                (folder, conn, schema, table, seq, column,
                 dtype, description, flags) = cells
                _check_master_row(ad_path, lineno, cells)
                key = f"{folder}.{table}".lower()
                if key in master:
                    raise ValueError(
                        f"table '{key}' exists in BOTH {auto_path} and "
                        f"{ad_path} - remove it from one of them")
                _insert_master_row(master, ad_path, lineno, folder, conn,
                                   schema, table, seq, column, dtype,
                                   description, flags)
    return master

def load_table_rules(path: str | Path) -> dict | None:
    """Load the curated table rules CSV. OPTIONAL (D3).

    Missing file -> None (caller degrades visibly: no dedup/sort).
    Malformed -> raises. Dedup cells reuse parse_dedup_spec grammar;
    multi-value cells split on '|'.

    Returns: { "<folder>.<table>" (lowercased) ->
                 {"dedup": parsed_spec | None,
                  "sort_cols": [col, ...],
                  "raw_dedup": str} }
    """
    rows = _read_registry_csv(path, TABLE_RULES_COLUMNS, required=False)
    if rows is None:
        return None

    rules: dict = {}
    for lineno, (folder, table, dedup_cell, sort_cell) in rows:
        key = f"{folder}.{table}".lower()
        if key in rules:
            raise ValueError(
                f"{path}:{lineno}: duplicate rules entry for '{key}'")

        dedup_parsed = None
        if dedup_cell:
            spec = dedup_cell  # default: single token ('all'/'none')
            if CELL_SEPARATOR in dedup_cell:
                spec = [c for c in dedup_cell.split(CELL_SEPARATOR)]
            elif dedup_cell.strip().lower() in ("all", "none"):
                spec = dedup_cell.strip()
            else:
                spec = [dedup_cell.strip()]
            dedup_parsed = parse_dedup_spec(spec)  # raises on typos

        sort_spec = parse_sort_spec(sort_cell)

        rules[key] = {"dedup": dedup_parsed,
                      "sort_spec": sort_spec,
                      "sort_cols": [c for c, _d in sort_spec],
                      "raw_dedup": dedup_cell}

    return rules

def _reject_retired_keys(cfg: dict) -> None:
    """Config keys that moved elsewhere (e.g. dedup -> table_rules_oracle_to_stg.csv)
    must fail loudly if they reappear - silent ignore is drift."""
    offenders = [
        (i, k) for i, t in enumerate(cfg.get("tables", []))
        for k in RETIRED_TABLE_KEYS if k in t
    ]
    if offenders:
        detail = ", ".join(f"tables[{i}].{k}" for i, k in offenders)
        raise ValueError(
            f"retired config key(s) present: {detail}. "
            f"These now live in config/extractors/table_rules_oracle_to_stg.csv - remove them "
            f"from config yaml.")

def validate_rules_references(schema_master: dict, rules: dict | None) -> None:
    """Every rules column (dedup keys, sort columns) must exist in the master.

    Strict fail (D4) - typo'd rules stop the run BEFORE any connection opens.
    A rules entry for a table absent from the master also raises: that pair
    is unresolvable, not merely stale.
    """
    if not rules:
        return
    problems = []
    for key, rule in rules.items():
        entry = schema_master.get(key)
        if entry is None:
            problems.append(f"table_rules entry '{key}' not in schema master")
            continue
        available = set(entry["columns"])
        mode = rule["dedup"]["mode"] if rule["dedup"] else "none"
        if mode in ("include", "exclude"):
            missing = [c for c in rule["dedup"]["columns"]
                       if c not in available]
            if missing:
                problems.append(
                    f"{key}: dedup column(s) {missing} not in master")
        for c, _direction in rule["sort_spec"]:
            if c not in available:
                problems.append(f"{key}: sort column '{c}' not in master")

    if problems:
        raise ValueError(
            "table_rules references unknown columns:\n  - "
            + "\n  - ".join(problems))

def validate_master_vs_config(cfg: dict, schema_master: dict,
                              source_type: str) -> None:
    """Master-CSV table-level fields must match config.yaml. Strict fail.

    Only checks tables whose connection type is `source_type` - the master
    CSV is per-extractor (each extractor validates its own subset).
    A config-side rename (connection/schema) without master regeneration
    is drift the pipeline must refuse to paper over.
    """
    problems = []
    for t in cfg["tables"]:
        conn = cfg["connections"].get(t["connection"], {})
        if conn.get("type") != source_type:
            continue
        key = f"{conn['folder']}.{t['name']}".lower()
        entry = schema_master.get(key)
        if entry is None:
            problems.append(f"configured table '{key}' absent from master CSV "
                           f"- run: python -m lib.generate_schemas --force")
            continue
        if entry["connection"].lower() != t["connection"].lower():
            problems.append(f"{key}: connection is '{t['connection']}' in "
                            f"config but '{entry['connection']}' in master")
        if entry["schema"].lower() != t["schema"].lower():
            problems.append(f"{key}: schema is '{t['schema']}' in config "
                            f"but '{entry['schema']}' in master")
    if problems:
        raise ValueError("master CSV inconsistent with config.yaml:\n  - "
                         + "\n  - ".join(problems))

# ---------------------------------------------------- pending ledger (D5)
def read_pending(path: str | Path | None = None) -> dict:
    """Read config/_pending.yaml (or an explicit path). Empty if absent."""
    p = Path(path) if path else PENDING_FILE
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}

def update_pending(findings: list[dict], path: str | Path | None = None) -> dict:
    """Record undocumented-column findings; bump counters.

    findings: [{"folder": ..., "table": ..., "column": ...}, ...]
    Entries persist until reconcile_pending clears them (i.e. until the
    column appears in a regenerated master CSV).
    """
    pending = read_pending(path)
    today = date.today().isoformat()
    for f in findings:
        key = f"{f['folder']}.{f['table']}.{f['column']}".lower()
        e = pending.get(key, {"first_seen": today, "occurrences": 0})
        e["last_seen"] = today
        e["occurrences"] += 1
        pending[key] = e
    Path(path).write_text(yaml.safe_dump(pending, sort_keys=True)) if path \
        else PENDING_FILE.write_text(yaml.safe_dump(pending, sort_keys=True))
    return pending

def reconcile_pending(schema_master: dict,
                       path: str | Path | None = None) -> list[str]:
    """Drop ledger entries whose column now exists in the master CSV.

    Returns the resolved keys - the 'you finished something' report.
    Call after every master regeneration.
    """
    pending = read_pending(path)
    resolved = []
    for key in list(pending):
        table_key, _, column = key.rpartition(".")
        entry = schema_master.get(table_key)
        if entry and column in entry["columns"]:
            del pending[key]
            resolved.append(key)
    if resolved or pending:
        p = Path(path) if path else PENDING_FILE
        p.write_text(yaml.safe_dump(pending, sort_keys=True))
    return resolved


# -------------------------------------------------------------- validators
def validate_config_folders(cfg: dict) -> None:
    """Validate folders are unique across connections and safe path segments.

    Raises ValueError on first violation so the pipeline doesn't proceed with
    broken config. Mutates cfg['folder_validated'] = True on success.
    """
    # A failed load upstream delivers cfg=None here; refuse cleanly
    # instead of crashing with a TypeError (wrapper incident 2026-11-05).
    if not isinstance(cfg, dict) or not isinstance(cfg.get("connections"), dict):
        raise ValueError(
            "config is missing or invalid (load_config failed upstream) - "
            "fix the config error reported in the log before running")

    folders_seen: dict[str, str] = {}  # folder -> connection_name

    for conn_name, conn_cfg in cfg["connections"].items():
        folder = conn_cfg["folder"]

        # Safe-path-segment check
        _valid_folder_segment(folder)

        # Uniqueness check
        if folder in folders_seen:
            raise ValueError(
                f"Duplicate folder '{folder}' declared for connections "
                f"'{folders_seen[folder]}' and '{conn_name}'. "
                "Each connection must have a unique folder."
            )
        folders_seen[folder] = conn_name

    cfg["folder_validated"] = True

# secrets/ may hold ONLY the vault files and the machine-managed dirs.
# Anything else is treated as a suspected plaintext password file and
# stops the run before any connection is opened (2026-11-05 policy).
ALLOWED_SECRET_FILES = frozenset({"secrets.fernet.bin", "secrets.sops.yaml"})
ALLOWED_SECRET_DIRS = frozenset({"backups", "logs"})

def validate_secrets_layout() -> None:
    """Refuse to run when secrets/ contains unexpected entries.

    Passwords live ONLY in the vaults. Hidden files (macOS .DS_Store,
    the tool's transient .tmp files) are ignored; everything else that
    is not the two vault files or backups//logs/ raises, naming the
    offenders. Raises ValueError; called by the extractors at startup.
    """
    if not SECRET_DIR.is_dir():
        raise ValueError(f"secrets directory not found: {SECRET_DIR}")
    offenders = []
    for entry in sorted(SECRET_DIR.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if entry.name not in ALLOWED_SECRET_DIRS:
                offenders.append(entry.name + "/")
        elif entry.name not in ALLOWED_SECRET_FILES:
            offenders.append(entry.name)
    if offenders:
        raise ValueError(
            f"secrets/ contains unexpected entries: {offenders}. Only the "
            f"vault files (secrets.fernet.bin, secrets.sops.yaml) and the "
            f"machine-managed dirs (backups/, logs/) may exist there. "
            f"Passwords now live in the vault - add or rotate them with: "
            f"./vault.sh scan <env>. Delete legacy plaintext password "
            f"files by hand once their secrets are vaulted."
        )

def table_collision_check(cfg: dict) -> None:
    """Detect tables with same (folder, table_name) → silent overwrite risk.

    Raises ValueError on collision. Caller (extractor) invokes this once at
    startup before opening any connections.
    """
    seen: dict[str, str] = {}  # "(folder,table)" -> "connection.schema.name"

    for table_cfg in cfg["tables"]:
        conn_name = table_cfg["connection"]
        try:
            folder = cfg["connections"][conn_name]["folder"]
        except KeyError:
            raise ValueError(f"Connection '{conn_name}' in table '{table_cfg['name']}' not found")

        table_key = f"{folder}:{table_cfg['name'].lower()}"
        if table_key in seen:
            raise ValueError(
                f"Table collision: '{seen[table_key]}' and "
                f"'{conn_name}.{table_cfg['schema']}.{table_cfg['name']}' both "
                f"map to folder '{folder}'/table '{table_cfg['name']}'. "
                "Rename one table or use a different folder for its connection."
            )
        seen[table_key] = f"{conn_name}.{table_cfg['schema']}.{table_cfg['name']}"

# ------------------------------------------------------------------- loaders
def load_config(path: str, schema: Schema) -> dict | None:
    """Load and validate a YAML file. Defaults to the per-source CONFIG_SCHEMA."""
    try:
        with open(path, "r") as fh:
            cfg = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        log.error("syntax error in %s: invalid YAML -> %s", path, exc)
        return None
    except FileNotFoundError:
        log.error("configuration file not found: %s", path)
        return None

    try:
        schema.validate(cfg)
    except SchemaError as e:
        log.error("validation error in %s: %s", path, e)
        return None

    _reject_retired_keys(cfg)     # NEW - raises ValueError
    return cfg


def load_settings(path: str) -> dict | None:
    """Load the global writer/runtime settings from the given YAML file.

    Mandatory path argument - no guessing or defaults. Applies the
    runs_root derived default after schema validation.
    """
    settings = load_config(path, SETTINGS_SCHEMA)
    return normalize_settings(settings) if settings else settings

# --------------------------------------------------------------- landing i/o
def landing_dir(landing_root: str | Path, load_date: date, source: str) -> Path:
    """Build landing path: <root>/<date>/<source>."""
    return Path(landing_root) / load_date.isoformat() / source

def write_manifest(directory: Path, name: str, payload: dict) -> None:
    """Write a JSON manifest into the directory. Creates dir if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"_manifest_{name}.json").write_text(json.dumps(payload, indent=2))

def clean_scratch(root: Path) -> None:
    """Remove all *.parquet files from scratch (transient files)."""
    root.mkdir(parents=True, exist_ok=True)
    for f in root.glob("*.parquet"):
        f.unlink()

def _prune_date_dirs(root: Path, days: int, what: str) -> None:
    """Remove YYYY-MM-DD-named subdirectories older than `days`.

    NON-date subdirectories are DELIBERATELY EXEMPT: runs/ and history/
    (manifest archive, SQLite) live under the staging root alongside the
    landing date-dirs. Their survival depends on failing the date-name
        check below - locked by tests/unit/test_common.py::test_prune_protects_non_date_dirs. 
    Never relax this to 'delete everything older than cutoff'.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name[:4].isdigit() and d.name < cutoff:
            log.info("pruning %s: %s", what, d)
            shutil.rmtree(d)


def prune_old_landing(root: Path, days: int) -> None:
    """Remove landing date-dirs older than `days` (YYYY-MM-DD named)."""
    root.mkdir(parents=True, exist_ok=True)
    _prune_date_dirs(root, days, "landing")


def prune_old_runs(root: Path, days: int) -> None:
    """Remove manifest-archive date-dirs older than `days`.

    Wrapper housekeeping calls this with wrapper.yaml's
    retention.manifest_days (default 90) - also the update_history
    backfill window: files outside it don't exist, so the filesystem
    bounds the scan.
    """
    root.mkdir(parents=True, exist_ok=True)
    _prune_date_dirs(root, days, "manifest archive")

def make_run_id(prefix: str = "run_",
                ts_format: str = "%Y%m%d_%H%M%S") -> str:
    """Timestamp-derived run identifier: filesystem-safe, sortable.

    Shared by the extractor's CLI (manual runs -> 'manual_...' prefix)
    and the wrapper (wrapper.yaml prefix/format). The same value
    prefixes log lines, manifest filenames, and SQLite rows.
    """
    import time
    return f"{prefix}{time.strftime(ts_format)}"
