# tests/integration/test_generate_schemas.py
"""Live-Oracle integration test for the refactored introspection task.

Deselected by default, like tests/integration/test_ora_connect.py - run it
explicitly from the office network using the same command/marker you use
for the other integration tests. If your existing integration tests use a
different marker name or pytest.ini/pyproject registration, copy THEIR
convention exactly (check the top of test_ora_connect.py).

Proves the real path end to end: config -> live session -> the new
_introspect_task describing a real query result set through session_pool's
contract (one task, one live connection, task raises on failure).
"""

from __future__ import annotations

import pytest

from dpl.lib import generate_schemas as gs
from dpl.lib.common import load_config
from dpl.lib.ora_connect import ORACLE_CONFIG_SCHEMA, connect

pytestmark = pytest.mark.integration

# Top-level dtypes the generator may emit (prefix match: Decimal(12.2) etc).
KNOWN_DTYPES = {"Int64", "Float64", "Float32", "Utf8", "Datetime", "Binary",
                "Decimal"}


def test_introspect_task_against_real_oracle():
    cfg = load_config(gs.CONFIG_FILE, ORACLE_CONFIG_SCHEMA)
    if cfg is None:
        pytest.skip(f"extractor config not found: {gs.CONFIG_FILE}")

    tables = [t for t in cfg["tables"]
              if cfg["connections"].get(t["connection"], {}).get("type")
              == "oracle"]
    if not tables:
        pytest.skip("no oracle tables configured")

    table = tables[0]

    with connect(cfg, table["connection"]) as conn:
        rows = gs._introspect_task(cfg, table, gs.oracle_dtype_to_comma_free)(conn)

    assert rows, f"no columns introspected for {table['connection']}.{table['name']}"
    assert [r["column_seq"] for r in rows] == \
        [str(i) for i in range(1, len(rows) + 1)]
    assert len({r["column_name"] for r in rows}) == len(rows)  # no duplicates
    for r in rows:
        assert r["dtype"].split("(")[0] in KNOWN_DTYPES, \
            f"unexpected dtype {r['dtype']} for {r['column_name']}"
