# dpl/lib/ora_connect.py
"""
Oracle connection and query helpers (python-oracledb thin mode).

Config:     config/extractors/oracle_to_stg.yaml (connections, tables)
Secrets:    pass_mgr vaults - per-user credential_ref; the vault method is
             chosen by default_method in pass_mgr.<env>.yaml. Plaintext
             password files are NOT supported (removed by design).
Settings:   config/settings.*.yaml (batch_rows etc.)
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import oracledb
import polars as pl
from schema import And, Forbidden, Optional, Schema

from dpl.lib import pass_mgr
from dpl.lib.logger import get_run_logger

log = get_run_logger("dpl.ora")


CONFIG_FILE = "config/extractors/oracle_to_stg.yaml"
SETTINGS_FILE = "config/settings.laptop.yaml"

DEFAULT_BATCH_SIZE = 50_000

ORACLE_CONFIG_SCHEMA = Schema({
    "connections": {
        str: {  # Dynamic key: Matches 'ora_db', 'postgres_prod', etc.
             "type": And(str, len, lambda t: t.lower() == "oracle"),
            Forbidden("username"): object,   # legacy keys are refused, not
            Forbidden("secret"): object,     # ignored - passwords live in
                                             # the pass_mgr vaults only
            "users": {
                str: {  # username - the users-map key (AD 'DOMAIN\user' OK)
                    "credential_ref": And(str, len),
                    Optional(str): object,   # e.g. role: read-only
                }
            },
            "host": And(str, len),
            "port": int,
            "service": And(str, len),
            "folder": And(str, len),  # required folder name per connection
            Optional(str): object  # Future-proof: allows adding new keys like 'timeout: 30' later
            # NOTE: "folder_validated" is NOT declared here - it is runtime
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
    
    users = c.get("users") or {}
    if not users:
        raise ValueError(
            f"Connection '{connection_name}' declares no users: section. "
            f"Add at least one user with a 'credential_ref' pointing into "
            f"the vault (e.g. {connection_name}.<user>.password)."
        )
    # One connection = one login: the first declared user connects.
    # The username is the users-map key; the password comes from the
    # pass_mgr vault (never from a file in secrets/).
    user_name, user_cfg = next(iter(users.items()))
    password = pass_mgr.get_secret(user_cfg["credential_ref"])

    dsn = oracledb.makedsn(c["host"], c["port"], service_name=c["service"])

    log.info("connecting to %s (%s:%s/%s)", connection_name, c["host"], c["port"], c["service"])

    conn = oracledb.connect(user=user_name, password=password, dsn=dsn)
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
                 declared: dict[str, DTypeLike] | None = None,
                 strict: bool = True):
     """Run query, yield one DataFrame per batch constructed with DECLARED dtypes.

     `declared` comes from the schema master (schema_master_*.csv,
     resolved via resolve_dtype). Polars NEVER infers types from data -
     inference mis-types NULL-leading columns and crashes mid-batch.

     Column names AND order must match the cursor exactly; any drift
     between master CSV and the live query fails loudly. This check is
     unconditional - not governed by `strict`.

     `strict=True` (default; settings key polars.strict_cast): a value that
     does not fit its declared dtype raises. `strict=False` coerces
     silently - emergency valve only.
     """
     if declared is None:
         raise ValueError(
             "iter_batches: a declared schema is REQUIRED - Polars type "
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

# A schema dtype can be a bare class (pl.Int64) OR a parameterised
# instance (pl.Datetime("us"), pl.Decimal(18, 6)). Both are valid.
DTypeLike = pl.DataType | type[pl.DataType]
def resolve_dtype(dtype_str: str) -> DTypeLike:
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
              declared: dict[str, DTypeLike] | None = None,
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

