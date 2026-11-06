"""Integration tests for dpl.lib.ora_connect (live Oracle).

Provenance: these were the live-connection _smoke_* routines at the
bottom of src/dpl/lib/ora_connect.py, run manually via
`python -m dpl.lib.ora_connect`. Moved here so test code never ships in
the production wheel.

These tests are INTEGRATION tests: they connect to the real Oracle
database with real vault credentials (credential_ref -> pass_mgr), read
the real extractor config, and stream real tables. They therefore:

  - never run as part of the routine suite or the green script
    (pyproject registers the marker and deselects it by default);
  - are run DELIBERATELY, on a machine with network access to Oracle
    and an initialized vault:   pytest -m integration -v

Where the original smoke skipped with a [SKIP] print (schema master not
yet generated), these tests pytest.skip - same semantics, no surprises.

Run with:  pytest tests/integration/test_ora_connect.py -m integration -v
"""

from __future__ import annotations

import polars as pl
import pytest

from dpl.lib.common import load_config, load_schema_master, load_settings
from dpl.lib.logger import setup_logging
from dpl.lib.ora_connect import (
    CONFIG_FILE,
    ORACLE_CONFIG_SCHEMA,
    SETTINGS_FILE,
    _batch_rows,
    connect,
    iter_batches,
    read_query,
    resolve_dtype,
)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def ora_cfg():
    """Real oracle_to_stg.yaml; fails loudly if it cannot load/validate."""
    setup_logging()
    cfg = load_config(CONFIG_FILE, ORACLE_CONFIG_SCHEMA)
    if cfg is None:
        pytest.fail(
            f"integration prerequisite failed: {CONFIG_FILE} did not "
            f"load/validate. Fix the config before judging the connector."
        )
    return cfg


@pytest.fixture(scope="module")
def ora_settings():
    settings = load_settings(SETTINGS_FILE)
    if settings is None:
        pytest.fail(
            f"integration prerequisite failed: {SETTINGS_FILE} did not "
            f"load/validate."
        )
    return settings


def _declared_for(cfg: dict, table_name: str) -> dict | None:
    """Resolve declared schema for one table from the schema master CSV.

    Returns None when the table entry is absent - the caller skips,
    pointing the user at generate_schemas (same [SKIP] semantics as
    the original smoke).
    """
    try:
        master = load_schema_master("config/extractors/schema_master_oracle_to_stg.csv")
    except FileNotFoundError:
        pytest.skip(
            "schema_master_*.csv not found - run "
            "python -m dpl.lib.generate_schemas --force first"
        )
    t = next((t for t in cfg["tables"] if t["name"] == table_name), None)
    if t is None:
        return None
    folder = cfg["connections"][t["connection"]]["folder"]
    entry = master.get(f"{folder}.{t['name']}".lower())
    if entry is None:
        pytest.skip(
            f"table '{t['name']}' not in schema master - run "
            "python -m dpl.lib.generate_schemas --force"
        )
    return {col: resolve_dtype(dt) for col, dt in entry["columns"].items()}


# ------------------------------------------------------------------ tests
@pytest.mark.integration
def test_connect_and_select_dual(ora_cfg):
    """Connect with vault-resolved password; SELECT 1 FROM dual -> 1."""
    with connect(ora_cfg, "ora_db") as conn:
        v = conn.cursor().execute("SELECT 1 FROM dual").fetchone()[0]
        assert v == 1


@pytest.mark.integration
def test_first_table_streamed(ora_cfg, ora_settings):
    """Stream the first configured table via the DECLARED schema path.

    Same code path the extractor uses: batched fetchmany, declared
    dtypes (no inference), strict cast policy honored.
    """
    t = ora_cfg["tables"][0]
    declared = _declared_for(ora_cfg, t["name"])
    if declared is None:
        pytest.skip(f"table '{t['name']}' not in config tables")

    batch_rows = _batch_rows(ora_settings)
    strict_cast = ora_settings.get("polars", {}).get("strict_cast", True)

    with connect(ora_cfg, t["connection"]) as conn:
        total = 0
        for i, df in enumerate(iter_batches(conn, t["query"], batch_rows,
                                            declared=declared,
                                            strict=strict_cast)):
            total += df.height
            if i == 0:
                assert df.columns == [c.lower() for c in df.columns], \
                    "columns not lowercased"
                assert list(declared.keys()) == df.columns, \
                    "dtypes: column order drift"

    assert total > 0, "query returned zero rows"


@pytest.mark.integration
def test_read_query_one_shot(ora_cfg, ora_settings):
    """read_query returns a single concatenated DataFrame (small query)."""
    declared = {"one": pl.Int64, "label": pl.String}
    strict_cast = ora_settings.get("polars", {}).get("strict_cast", True)
    df = read_query(ora_cfg, "ora_db",
                    "SELECT 1 AS ONE, 'x' AS LABEL FROM dual",
                    declared=declared, strict=strict_cast)
    assert df.height == 1 and df["one"][0] == 1


@pytest.mark.integration
def test_non_select_query_rejected(ora_cfg):
    """A PL/SQL block is refused: only result-bearing queries stream."""
    with connect(ora_cfg, "ora_db") as conn, pytest.raises(ValueError):
        next(iter_batches(conn, "BEGIN NULL; END;",
                          declared={"dummy": pl.Int64}))
