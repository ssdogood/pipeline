"""Unit tests for dpl.lib.ora_connect config schema (hermetic).

Provenance: ported from the _smoke_schema_rejects_retired_and_legacy_keys
function that lived at the bottom of src/dpl/lib/ora_connect.py. That
function was fully offline (temp-dir YAML fixtures + schema validation),
so unlike its siblings (the live-connection smokes, now in
tests/integration/) it belongs here in the unit suite.

Assertions are unchanged. The security invariants under test:
  - a config using vault credential_ref is accepted
  - legacy plaintext secret/username keys are REFUSED by the schema
    (not silently ignored) - passwords live in pass_mgr vaults only
  - retired dedup config keys raise a loud ValueError post-schema

The YAML below is built by plain string concatenation - no str.format(),
so the literal braces in the tables section need no escaping. Injection
slots match the original smoke: extras go at CONNECTION level (legacy
keys) or after the query inside the table mapping (retired dedup keys).

Run with:  pytest tests/unit/test_ora_schema.py -v
"""

from __future__ import annotations

import pytest

from dpl.lib.common import load_config
from dpl.lib.ora_connect import ORACLE_CONFIG_SCHEMA

_HEAD = (
    "connections:\n"
    "  ora_db:\n"
    "    type: oracle\n"
    "    host: h\n"
    "    port: 1521\n"
    "    service: svc\n"
    "    folder: etl"
)
_USERS = (
    "\n    users:\n"
    "      user_name:\n"
    "        credential_ref: ora_db.user_name.password\n"
)
_TABLE = (
    "transforms: {}\n"
    "tables:\n"
    "  - {connection: ora_db, schema: STG, name: T, query: 'SELECT 1'"
)


def _yaml(extra_conn: str = "", extra_table: str = "") -> str:
    """Assemble one valid-or-invalid extractor config.

    extra_conn is appended at CONNECTION level - the only place the
    schema rejects legacy keys (injecting them inline after
    credential_ref makes invalid YAML and the test would pass for the
    wrong reason). extra_table is appended after the query, inside the
    table flow mapping - exactly where the original smoke injected
    retired dedup keys.
    """
    return (
        _HEAD
        + extra_conn
        + _USERS
        + _TABLE
        + extra_table
        + "}\n"
    )


def _write(tmp_path, name: str, text: str):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_clean_credential_ref_config_accepted(tmp_path):
    """A clean config using vault credential_ref loads and validates."""
    f = _write(tmp_path, "cfg_ok.yaml", _yaml())
    cfg = load_config(str(f), ORACLE_CONFIG_SCHEMA)
    assert cfg is not None, "clean credential_ref config rejected"


def test_legacy_secret_keys_refused(tmp_path):
    """Legacy secret/username keys are refused by the schema -> None."""
    for extra in ["\n    secret: pw.txt", "\n    username: u"]:
        f = _write(tmp_path, "cfg_legacy.yaml", _yaml(extra_conn=extra))
        got = load_config(str(f), ORACLE_CONFIG_SCHEMA)
        assert got is None, f"legacy key accepted: {extra!r}"


def test_retired_dedup_keys_rejected(tmp_path):
    """Retired dedup keys raise a loud ValueError (post-schema check)."""
    for extra in [", dedup_columns: all", ", dedup_columns: 42"]:
        f = _write(tmp_path, "cfg_bad.yaml", _yaml(extra_table=extra))
        with pytest.raises(ValueError, match="retired config key"):
            load_config(str(f), ORACLE_CONFIG_SCHEMA)
