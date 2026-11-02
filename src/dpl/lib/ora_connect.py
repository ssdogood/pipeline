# dpl/lib/ora_connect.py
"""
Oracle connection and query helpers (python-oracledb thin mode).

Config:     config/extractors/oracle_to_stg.yaml (connections, tables)
Secrets:    secrets/<file from config>
Settings:   config/settings.*.yaml (batch_rows etc.)
"""

from __future__ import annotations

from contextlib import contextmanager

import oracledb
import polars as pl
import re

from schema import Schema, And, Optional

from dpl.lib.common import load_config, load_settings, SECRET_DIR
from dpl.lib.logger import get_run_logger, setup_logging

log = get_run_logger("dpl.ora")


CONFIG_FILE = "config/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "config/settings.laptop.yaml"

DEFAULT_BATCH_SIZE = 50_000

ORACLE_CONFIG_SCHEMA = Schema({
    "connections": {
        str: {  # Dynamic key: Matches 'ora_db', 'postgres_prod', etc.
             "type": And(str, len, lambda t: t.lower() == "oracle"),
            "username": And(str, len),
            "secret": And(str, len),
            "host": And(str, len),
            "port": int,
            "service": And(str, len),
            "folder": And(str, len),  # required folder name per connection
            Optional(str): object  # Future-proof: allows adding new keys like 'timeout: 30' later
            # NOTE: "folder_validated" is NOT declared here — it is runtime
            # state set top-level by validate_config_folders(), never config.
        }
    },

    "transforms": {
        Optional(str): object  # Allows you to easily append more text transforms later
    },

    "tables": [
        {
            "connection": And(str, len),
            "schema": And(str, len),
            "name": And(str, len),
            "query": And(str, len),
            Optional(str): object  # Future-proof
        }
    ],
    Optional(str): object  # Future-proof
})

# connect
@contextmanager
def connect(cfg: dict, connection_name: str):
    """
    Yield an open Oracle connection; always closed on exit.
    with connect(cfg, "ora_db") as conn:
        ...
    """
    try:
        c = cfg["connections"][connection_name]
    except KeyError:
        raise KeyError(
            f"Connection '{connection_name}' not defined in config. "
            f"Available: {list(cfg['connections'])}"
        )

    if c["type"] != "oracle":
        raise ValueError(
            f"Connection '{connection_name}' has type '{c['type']}', expected 'oracle'. "
            f"Use the correct *_connect module for this connection."
        )
    
    secret_file = SECRET_DIR / c["secret"]
    if not secret_file.exists():
        raise FileNotFoundError(
            f"Secret file not found: {secret_file} "
            f"(configured for connection '{connection_name}')"
        )
    
    password = secret_file.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError(f"Secret file is empty: {secret_file}")
    
    dsn = oracledb.makedsn(c["host"], c["port"], service_name=c["service"])

    log.info("connecting to %s (%s:%s/%s)", connection_name, c["host"], c["port"], c["service"])

    conn = oracledb.connect(user=c["username"],password=password, dsn=dsn)
    try:
        yield conn
    finally:
        conn.close()
    
def _column_names(cursor, lowercase: bool = True) -> list[str]:
    """
    Oracle returns Uppercase identifiers; lowercase them for cross-DB consistency.
    """
    names = [d[0] for d in cursor.description]
    return [n.lower() for n in names] if lowercase else names

def _batch_rows(settings: dict) -> int:
    """
    Pull the configured batch size, falling back to DEFAULT_BATCH_ROWS.
    """
    if settings and settings.get("polars", {}).get("batch_rows"):
        return settings["polars"]["batch_rows"]
    return DEFAULT_BATCH_SIZE

def iter_batches(conn, query: str, batch_size: int = DEFAULT_BATCH_SIZE,
                 declared: dict[str, pl.DataType] = None,
                 strict: bool = True):
     """Run query, yield one DataFrame per batch constructed with DECLARED dtypes.

     `declared` comes from the schema master (schema_master_*.csv,
     resolved via resolve_dtype). Polars NEVER infers types from data —
     inference mis-types NULL-leading columns and crashes mid-batch.

     Column names AND order must match the cursor exactly; any drift
     between master CSV and the live query fails loudly. This check is
     unconditional — not governed by `strict`.

     `strict=True` (default; settings key polars.strict_cast): a value that
     does not fit its declared dtype raises. `strict=False` coerces
     silently — emergency valve only.
     """
     if declared is None:
         raise ValueError(
             "iter_batches: a declared schema is REQUIRED — Polars type "
             "inference from data is disabled by design. Regenerate with: "
             "python -m lib.generate_schemas --force")
     cur = conn.cursor()
     try:
         cur.arraysize = batch_size
         cur.prefetchrows = batch_size
         cur.execute(query)
         if cur.description is None:
             raise ValueError("Query returned no result set (not a SELECT?)")

         names = _column_names(cur)
         # Enforce: declared schema must match cursor order (always strict)
         if list(declared.keys()) != names:
             raise ValueError(
                 f"schema mismatch: master says {list(declared.keys())}, "
                 f"query returns {names}")
         schema = {n: declared[n] for n in names}

         while True:
             rows = cur.fetchmany(batch_size)
             if not rows:
                 break
             yield pl.DataFrame(rows, schema=schema, orient="row",
                                strict=strict)
     finally:
         cur.close()


def resolve_dtype(dtype_str: str) -> pl.DataType:
     """Comma-free master-CSV dtype string -> Polars type. Strict."""
     s = dtype_str.strip().lower()
     simple = {
         "utf8": pl.String, "int64": pl.Int64, "int32": pl.Int32,
         "float32": pl.Float32, "float64": pl.Float64,
         "binary": pl.Binary, "boolean": pl.Boolean,
         "date": pl.Date,
     }
     if s == "datetime":
         return pl.Datetime("us")
     m = re.fullmatch(r"datetime\(tz=([^)]+)\)", s)
     if m:
         return pl.Datetime("us", m.group(1))
     m = re.fullmatch(r"decimal\((\d+)\.(\d+)\)", s)
     if m:
         return pl.Decimal(int(m.group(1)), int(m.group(2)))
     if s == "decimal":
        return pl.Float64
     if s in simple:
         return simple[s]
     raise ValueError(f"unknown dtype in schema master: {dtype_str!r}")

def read_query(cfg: dict, connection_name: str, query: str,
              declared: dict[str, pl.DataType] = None,
              batch_size: int = DEFAULT_BATCH_SIZE,
              strict: bool = True) -> pl.DataFrame:
    """
    Full result as a single DataFrame. Convenient for small/medium tables.
    Requires the declared schema (same contract as iter_batches).
    """
    with connect(cfg, connection_name) as conn:
        frames = list(iter_batches(conn, query, batch_size=batch_size,
                                   declared=declared, strict=strict))

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


# smoke tests
def _smoke_declared_for(cfg: dict, table_name: str) -> dict | None:
    """Resolve declared schema for one table from schema_master_oracle_to_stg.csv.

    Returns None (with a SKIP notice) when the master or the table is
    absent — the caller skips, pointing the user at generate_schemas.
    """
    from dpl.lib.common import load_schema_master
    try:
        master = load_schema_master("config/extractors/schema_master_oracle_to_stg.csv")
    except FileNotFoundError:
        print("[SKIP] schema_master_*.csv not found - run "
              "python -m lib.generate_schemas --force first")
        return None
    t = next((t for t in cfg["tables"] if t["name"] == table_name), None)
    if t is None:
        return None
    folder = cfg["connections"][t["connection"]]["folder"]
    entry = master.get(f"{folder}.{t['name']}".lower())
    if entry is None:
        print(f"[SKIP] table '{t['name']}' not in schema master — run "
              "python -m lib.generate_schemas --force")
        return None
    return {col: resolve_dtype(dt) for col, dt in entry["columns"].items()}


def _smoke_load_config() -> dict:
    cfg = load_config(CONFIG_FILE, ORACLE_CONFIG_SCHEMA)
    assert cfg is not None, f"Could not load/validate {CONFIG_FILE}"
    print(
        f"[PASS] config loaded: {len(cfg['tables'])} table(s), "
        f"{len(cfg['connections'])} connection(s)"
    )
    return cfg

def _smoke_schema_rejects_retired_keys():
    """Retired config keys fail loudly (drift, not silent ignore)."""
    import tempfile
    from pathlib import Path

    base = (
        "connections:\n"
        "  ora_db:\n    {type: oracle, username: u, secret: s, host: h,\n"
        "     port: 1521, service: svc, folder: etl}\n"
        "transforms: {}\n"
        "tables:\n"
        "  - {connection: ora_db, schema: STG, name: T,\n"
        "     query: 'SELECT 1'%s}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f_ok = tmp / "cfg_ok.yaml"
        f_ok.write_text(base % "")
        cfg = load_config(str(f_ok), ORACLE_CONFIG_SCHEMA)
        assert cfg is not None, "clean config rejected"

        for extra in [", dedup_columns: all", ", dedup_columns: 42",
                      ", dedup_columns: [a, b]"]:
            f_bad = tmp / "cfg_bad.yaml"
            f_bad.write_text(base % extra)
            try:
                load_config(str(f_bad), ORACLE_CONFIG_SCHEMA)
                raise AssertionError(f"retired key accepted: {extra!r}")
            except ValueError as e:
                assert "retired config key" in str(e), e
    print("[PASS] config schema: retired/unknown table keys rejected")

def _smoke_load_settings() -> dict:
    settings = load_settings(SETTINGS_FILE)
    assert settings is not None, f"Could not load/validate {SETTINGS_FILE}"
    print(
        f"[PASS] settings loaded: batch_rows={settings['polars']['batch_rows']}, "
        f"memory_limit={settings['duckdb']['memory_limit']}"
    )
    return settings

def _smoke_connect(cfg: dict):
    with connect(cfg, "ora_db") as conn:
        v = conn.cursor().execute("SELECT 1 FROM dual").fetchone()[0]
        assert v == 1
        print("[PASS] connected; SELECT 1 FROM dual -> 1")

def _smoke_first_table_streamed(cfg: dict, settings: dict, table_name: str = None):
    """
    Stream the first configured table (or a named one) via the DECLARED
    schema path — same code path the extractor uses.
    """
    t = next((t for t in cfg["tables"] if t["name"] == table_name), cfg["tables"][0])
    declared = _smoke_declared_for(cfg, t["name"])
    if declared is None:
        return
    batch_rows = _batch_rows(settings)
    strict_cast = settings.get("polars", {}).get("strict_cast", True)
    print(
        f"[INFO] streaming {t['name']} in {batch_rows:,}-row batches "
        f"(strict_cast={strict_cast})"
    )

    with connect(cfg, t["connection"]) as conn:
        total = 0
        for i, df in enumerate(iter_batches(conn, t["query"], batch_rows,
                                            declared=declared,
                                            strict=strict_cast)):
            total += df.height
            if i == 0:
                assert df.columns == [c.lower() for c in df.columns], "columns not lowercased"
                assert list(declared.keys()) == df.columns, "dtypes: column order drift"
                print(f"    columns: {df.columns}")
                print(f"    dtypes: {[str(d) for d in df.dtypes]}")
                print(df.head(3))

    assert total > 0, "query returned zero rows"
    print(f"[PASS] streamed {total:,} rows across batches (declared schema)")


def _smoke_read_query(cfg: dict, settings: dict):
    declared = {"one": pl.Int64, "label": pl.String}
    strict_cast = settings.get("polars", {}).get("strict_cast", True)
    df = read_query(cfg, "ora_db",
                    "SELECT 1 AS ONE, 'x' AS LABEL FROM dual",
                    declared=declared, strict=strict_cast)
    assert df.height == 1 and df["one"][0] == 1
    print("[PASS] read_query -> one shot DataFrame")


def _smoke_bad_query_rejected(cfg: dict):
    with connect(cfg, "ora_db") as conn:
        try:
            next(iter_batches(conn, "BEGIN NULL; END;",
                              declared={"dummy": pl.Int64}))
        except ValueError:
            print("[PASS] non-SELECT query rejected with clear error")
        else:
            raise AssertionError("non-SELECT query was not rejected")


if __name__ == "__main__":
    # Run from project root: python -m lib.ora_connect
    setup_logging()

    cfg = _smoke_load_config()
    settings = _smoke_load_settings()
    print(f"[INFO] strict_cast={settings.get('polars', {}).get('strict_cast', True)}")
    _smoke_schema_rejects_retired_keys()
    _smoke_connect(cfg)
    _smoke_first_table_streamed(cfg, settings)
    _smoke_read_query(cfg, settings)
    _smoke_bad_query_rejected(cfg)

    print("\nALL SMOKE TESTS PASSED")