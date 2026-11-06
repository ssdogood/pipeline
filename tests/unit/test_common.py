"""Unit tests for dpl.lib.common (ported from the embedded smoke tests).

Provenance: these were the _smoke_* functions that used to live at the
bottom of src/dpl/lib/common.py, run manually via
`python -m dpl.lib.common`. They were moved here so that test code and
fixtures never ship inside the production wheel. Assertions are
unchanged; only the harness changed: pytest's tmp_path replaces the
hand-rolled TemporaryDirectory, and print("[PASS] ...") lines were
dropped (pytest's own report replaces them).

One deliberate improvement: the prune tests originally used hard-coded
dates (2026-01-01 / 2026-10-17). The "recent" date was about to fall
outside its 30-day window and fail the test on a calendar technicality,
so both prune tests now compute their dates relative to date.today().

Run with:  pytest tests/unit/test_common.py -v
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

from dpl.lib.common import (
    SETTINGS_SCHEMA,
    clean_scratch,
    landing_dir,
    load_config,
    load_schema_master,
    load_settings,
    load_table_rules,
    parse_dedup_spec,
    parse_sort_spec,
    prune_old_landing,
    prune_old_runs,
    read_pending,
    reconcile_pending,
    runs_dir,
    table_collision_check,
    update_pending,
    validate_config_folders,
    validate_rules_references,
    write_manifest,
)


def _write(tmp_path, name: str, text: str):
    """Write a registry CSV fixture into tmp_path (helper, not a test)."""
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


# ------------------------------------------------------------------ settings
def test_load_settings_minimal(tmp_path):
    """A minimal-but-valid settings.yaml loads and validates."""
    settings = {
        "paths": {
            "landing_root": str(tmp_path / "landing"),
            "scratch_root": str(tmp_path / "scratch"),
        },
        "duckdb": {"memory_limit": "2GB", "threads": 2},
        "writer": {"compression": "zstd", "max_file_mb": 512},
    }
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(yaml.dump(settings))

    loaded = load_settings(str(settings_file))
    assert loaded is not None, "settings.yaml failed to load/validate"
    assert loaded["duckdb"]["memory_limit"] == "2GB"


def test_load_config_missing_file_returns_none(tmp_path):
    """A missing config file returns None, not a crash."""
    result = load_config(str(tmp_path / "no_such_file.yaml"), SETTINGS_SCHEMA)
    assert result is None, "missing file should return None"


def test_settings_runs_root(tmp_path):
    """runs_root: explicit value honored; absent -> derived landing/_runs."""
    base = {
        "paths": {
            "landing_root": str(tmp_path / "landing"),
            "scratch_root": str(tmp_path / "scratch"),
        },
        "duckdb": {"memory_limit": "2GB"},
    }

    # absent -> derived
    f = tmp_path / "s_derived.yaml"
    f.write_text(yaml.dump(base))
    s = load_settings(str(f))
    assert s["paths"]["runs_root"] == str(tmp_path / "landing" / "_runs"), s["paths"]

    # explicit -> honored verbatim
    base["paths"]["runs_root"] = str(tmp_path / "elsewhere" / "runs")
    f2 = tmp_path / "s_explicit.yaml"
    f2.write_text(yaml.dump(base))
    s2 = load_settings(str(f2))
    assert s2["paths"]["runs_root"] == str(tmp_path / "elsewhere" / "runs")

    # empty string -> rejected by schema (And(str, len))
    base["paths"]["runs_root"] = ""
    f3 = tmp_path / "s_empty.yaml"
    f3.write_text(yaml.dump(base))
    assert load_settings(str(f3)) is None


# ------------------------------------------------------------------ landing i/o
def test_landing_dir_layout(tmp_path):
    """landing_dir takes the root explicitly: <root>/<date>/<source>."""
    d = landing_dir(str(tmp_path), date(2026, 10, 17), "oracle")
    expected = tmp_path / "2026-10-17" / "oracle"
    assert d == expected, f"expected {expected}, got {d}"


def test_runs_dir_layout(tmp_path):
    """runs_dir mirrors landing layout: <root>/<date>/<folder>."""
    d = runs_dir(str(tmp_path), date(2026, 10, 28), "etl")
    assert d == tmp_path / "2026-10-28" / "etl", d


def test_write_manifest_round_trip(tmp_path):
    """write_manifest writes JSON that round-trips identically."""
    d = tmp_path / "manifest_test"
    payload = {"tables": 2, "source": "oracle"}
    write_manifest(d, "oracle", payload)

    out = d / "_manifest_oracle.json"
    assert out.exists(), "manifest not written"
    assert __import__("json").loads(out.read_text()) == payload


def test_clean_scratch_removes_only_parquet(tmp_path):
    """clean_scratch removes *.parquet only; other extensions survive."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "x.parquet").write_bytes(b"junk")
    (scratch / "keep.txt").write_text("junk")
    (scratch / "y.pq").write_bytes(b"junk")  # .pq extension, not .parquet

    clean_scratch(scratch)

    assert not (scratch / "x.parquet").exists(), "parquet not removed"
    assert (scratch / "keep.txt").exists(), "text file removed"
    assert (scratch / "y.pq").exists(), ".pq file removed"


# ------------------------------------------------------------------ pruning
def test_prune_old_landing_removes_only_old_date_dirs(tmp_path):
    """Old date dirs are pruned; recent and non-date dirs survive."""
    old = (date.today() - timedelta(days=40)).isoformat()
    recent = (date.today() - timedelta(days=5)).isoformat()

    (tmp_path / old).mkdir()
    (tmp_path / old / "f.parquet").write_bytes(b"x")
    (tmp_path / recent).mkdir()      # recent, must survive
    (tmp_path / "not_a_date").mkdir()  # non-date, must survive

    prune_old_landing(tmp_path, days=30)

    assert not (tmp_path / old).exists(), "old date dir survived"
    assert (tmp_path / recent).exists(), "recent date dir removed"
    assert (tmp_path / "not_a_date").exists(), "non-date dir removed"


def test_prune_protects_non_date_dirs(tmp_path):
    """THE safety lock: prune never removes runs/, history/, non-date dirs.

    Locks the invariant the shared staging root depends on. If prune
    ever learns to delete non-date dirs, manifests and history die
    with them.
    """
    root = tmp_path / "prune_safety"
    root.mkdir()
    old1 = (date.today() - timedelta(days=40)).isoformat()
    old2 = (date.today() - timedelta(days=39)).isoformat()
    (root / old1).mkdir()          # old landing date-dir: pruned
    (root / old2).mkdir()          # old archive date-dir: pruned
    (root / "_runs").mkdir()       # non-date: MUST SURVIVE
    (root / "_history").mkdir()    # non-date: MUST SURVIVE
    (root / "not_a_date").mkdir()  # non-date: MUST SURVIVE

    prune_old_landing(root, days=30)
    prune_old_runs(root, days=30)

    assert not (root / old1).exists(), "old landing dir survived"
    assert not (root / old2).exists(), "old archive dir survived"
    assert (root / "_runs").exists(), "PRUNE SAFETY VIOLATION: runs/ deleted"
    assert (root / "_history").exists(), "PRUNE SAFETY VIOLATION: history/ deleted"
    assert (root / "not_a_date").exists(), "PRUNE SAFETY VIOLATION: non-date dir deleted"


# ------------------------------------------------------------------ folders/tables
def test_validate_folders_unique_passes(tmp_path):
    """Unique folder check passes for distinct folders."""
    cfg = {
        "connections": {
            "ora_db": {"folder": "oracle", "type": "oracle",
                       "host": "", "port": 1523, "service": ""},
            "pg_stg": {"folder": "postgres", "type": "postgres",
                       "host": "", "port": 5432, "service": ""},
        }
    }
    validate_config_folders(cfg)
    assert cfg["folder_validated"] is True


def test_validate_folders_duplicate_raises(tmp_path):
    """Duplicate folders raise ValueError."""
    cfg = {
        "connections": {
            "ora_db": {"folder": "oracle", "type": "oracle",
                       "host": "", "port": 1523, "service": ""},
            "pg_stg": {"folder": "oracle", "type": "postgres",
                       "host": "", "port": 5432, "service": ""},
        }
    }
    with pytest.raises(ValueError, match="Duplicate folder 'oracle'"):
        validate_config_folders(cfg)


def test_validate_folder_safe_segments(tmp_path):
    """Unsafe folder names are rejected at validation time."""
    unsafe = ["../evil", "/absolute", "http://bad", "contains/slash"]
    for folder in unsafe:
        cfg = {"connections": {"c": {"folder": folder}}}
        with pytest.raises(Exception):
            validate_config_folders(cfg)


def test_table_collision_detection(tmp_path):
    """Same table name in the same folder -> ValueError."""
    cfg = {
        "connections": {
            "ora_db": {"folder": "oracle", "type": "oracle",
                       "host": "", "port": 1523, "service": ""},
            "pg_stg": {"folder": "oracle", "type": "postgres",
                       "host": "", "port": 5432, "service": ""},
        },
        "transforms": {},
        "tables": [
            {"connection": "ora_db", "schema": "STG", "name": "CUSTOMER", "query": "SELECT 1"},
            {"connection": "pg_stg", "schema": "STG", "name": "CUSTOMER", "query": "SELECT 1"},
        ]
    }
    with pytest.raises(ValueError, match="Table collision"):
        table_collision_check(cfg)


# ------------------------------------------------------------------ dedup spec
def test_dedup_modes(tmp_path):
    """All four modes parse correctly; keywords case-insensitive."""
    cases = [
        (None,                   {"mode": "none",    "columns": []}),
        ("none",                 {"mode": "none",    "columns": []}),
        ("NONE",                 {"mode": "none",    "columns": []}),
        ("All",                  {"mode": "all",     "columns": []}),
        (["a", "b"],             {"mode": "include", "columns": ["a", "b"]}),
        (["CUSTOMER_ID"],        {"mode": "include", "columns": ["customer_id"]}),
        (["-updated_at", "-rv"], {"mode": "exclude", "columns": ["updated_at", "rv"]}),
        (["-A"],                 {"mode": "exclude", "columns": ["a"]}),
    ]
    for spec, expected in cases:
        got = parse_dedup_spec(spec)
        assert got == expected, f"spec {spec!r}: expected {expected}, got {got}"

    # empty list == none
    assert parse_dedup_spec([]) == {"mode": "none", "columns": []}


def test_dedup_invalid_specs_raise(tmp_path):
    """Bad specs raise with useful messages; nothing silently accepted."""
    bad = [
        "alll",                # typo'd keyword
        "*",                   # reserved-YAML char someone quoted
        ["-a", "b"],           # mixed signs
        ["b", "-a"],           # mixed signs, other order
        ["-"],                 # bare minus
        [""],                  # empty entry
        ["a", ""],             # one empty entry
        42,                    # wrong type
    ]
    for spec in bad:
        with pytest.raises(ValueError):
            parse_dedup_spec(spec)


# ------------------------------------------------------------------ sort spec
def test_parse_sort_spec(tmp_path):
    """Sort grammar: asc default, trailing '-', '+' sugar, combos, errors."""
    assert parse_sort_spec("") == []
    assert parse_sort_spec(None) == []
    assert parse_sort_spec("updated_at") == [("updated_at", "asc")]
    assert parse_sort_spec("updated_at-") == [("updated_at", "desc")]
    assert parse_sort_spec("updated_at+") == [("updated_at", "asc")]
    assert parse_sort_spec("UPDATED_AT-|CUSTNO") == \
        [("updated_at", "desc"), ("custno", "asc")]
    assert parse_sort_spec("a-|b|c+") == \
        [("a", "desc"), ("b", "asc"), ("c", "asc")]

    bad = ["col--", "col-+", "-", "+", "a|--", "a||b", "a|-"]
    for cell in bad:
        with pytest.raises(ValueError):
            parse_sort_spec(cell)


# ------------------------------------------------------------------ registries
MASTER_HEADER = ("folder,connection_name,schema,table_name,"
                 "column_seq,column_name,dtype,description,policy_flags\n")


def test_load_schema_master(tmp_path):
    """Master loader: 9-col format, comments, order, duplicates."""
    auto = _write(tmp_path, "master_auto.csv",
                  "# generated - do not edit by hand\n"
                  + MASTER_HEADER +
                  "oracle,ora_db,STG,customer,1,id,Int64,PK,tz=utc\n"
                  "oracle,ora_db,STG,customer,2,name,Utf8,display name,\n"
                  "\n"
                  "etl,ora_db,STG,account,1,balance,Decimal(12.2),,\n")

    master = load_schema_master(auto)          # ad omitted -> no AD tables
    cust = master["oracle.customer"]
    assert list(cust["columns"]) == ["id", "name"], cust  # order preserved
    assert cust["flags"]["id"] == {"tz": "utc"}
    assert cust["descriptions"]["name"] == "display name"
    assert cust["connection"] == "ora_db" and cust["schema"] == "STG"
    assert master["etl.account"]["columns"]["balance"] == "decimal(12.2)"

    # duplicate column raises
    dup = _write(tmp_path, "dup.csv",
                 MASTER_HEADER +
                 "o,c,S,t,1,x,Utf8,,\n"
                 "o,c,S,t,2,x,Utf8,,\n")
    with pytest.raises(ValueError, match="duplicate column"):
        load_schema_master(dup)


def test_master_bad_seq_rejected(tmp_path):
    """Non-integer column_seq rejected with line number."""
    bad = _write(tmp_path, "m_seq.csv",
                 MASTER_HEADER +
                 "o,c,S,t,xyz,col,Utf8,,\n")
    with pytest.raises(ValueError, match="column_seq"):
        load_schema_master(bad)


def test_load_table_rules(tmp_path):
    f = _write(tmp_path, "rules.csv",
               "# curated by hand\n"
               "folder,table_name,dedup_columns,sort_order\n"
               "oracle,customer,id|region,id\n"
               "oracle,customer2,id|region,updated_at|id\n"
               "oracle,customer3,-update_at|-rv,updated_at\n"
               "oracle,customer4,all,\n"
               "etl,plain,,created\n")
    rules = load_table_rules(f)
    assert rules["oracle.customer"]["dedup"] == {"mode": "include", "columns": ["id", "region"]}
    assert rules["oracle.customer"]["sort_cols"] == ["id"]
    assert rules["oracle.customer2"]["dedup"] == {"mode": "include", "columns": ["id", "region"]}
    assert rules["oracle.customer2"]["sort_cols"] == ["updated_at", "id"]
    assert rules["oracle.customer3"]["dedup"]["mode"] == "exclude"
    assert rules["oracle.customer4"]["dedup"]["mode"] == "all"
    assert rules["etl.plain"]["dedup"] is None
    assert rules["etl.plain"]["sort_cols"] == ["created"]

    # missing file -> None (the visible-degrade contract)
    assert load_table_rules(tmp_path / "nope.csv") is None


def test_registry_csv_ragged_rows_rejected(tmp_path):
    bad = _write(tmp_path, "ragged.csv",
                 "folder,table_name,dedup_columns,sort_order\n"
                 "oracle,customer,id|region,updated_at,EXTRA\n")  # 5 cells
    with pytest.raises(ValueError, match=r":2:"):
        load_table_rules(bad)


def test_validate_rules_references(tmp_path):
    master = load_schema_master(_write(tmp_path, "m2.csv",
                                       MASTER_HEADER +
                                       "oracle,ora_db,STG,customer,1,id,Int64,,\n"
                                       "oracle,ora_db,STG,customer,2,region,Utf8,,\n"
                                       "oracle,ora_db,STG,customer,3,updated_at,Datetime,,\n"))
    good = load_table_rules(_write(tmp_path, "r2.csv",
                                   "folder,table_name,dedup_columns,sort_order\n"
                                   "oracle,customer,id|region,updated_at|id\n"))
    validate_rules_references(master, good)          # passes silently

    bad = load_table_rules(_write(tmp_path, "r3.csv",
                                  "folder,table_name,dedup_columns,sort_order\n"
                                  "oracle,customer,customr_id,id\n"))
    with pytest.raises(ValueError, match="customr_id"):
        validate_rules_references(master, bad)

    ghost = load_table_rules(_write(tmp_path, "r4.csv",
                                    "folder,table_name,dedup_columns,sort_order\n"
                                    "oracle,nosuchtable,id,\n"))
    with pytest.raises(ValueError, match="not in schema master"):
        validate_rules_references(master, ghost)


def test_rules_with_sort(tmp_path):
    """Rules loader carries parsed sort_spec; validate checks stripped names."""
    master = load_schema_master(_write(tmp_path, "ms.csv",
                                       MASTER_HEADER +
                                       "etl,ora_db,STG,customer,1,updated_at,Datetime,,\n"
                                       "etl,ora_db,STG,customer,2,custno,Int64,,\n"
                                       "etl,ora_db,STG,customer,3,region,Utf8,,\n"))

    rules = load_table_rules(_write(tmp_path, "rs.csv",
                                    "folder,table_name,dedup_columns,sort_order\n"
                                    "etl,customer,custno,updated_at-|custno\n"))
    assert rules["etl.customer"]["sort_spec"] == \
        [("updated_at", "desc"), ("custno", "asc")]
    assert rules["etl.customer"]["sort_cols"] == ["updated_at", "custno"]

    validate_rules_references(master, rules)     # desc column passes

    # typo AFTER the dash must name the stripped column, not 'custn-'
    bad = load_table_rules(_write(tmp_path, "rb.csv",
                                  "folder,table_name,dedup_columns,sort_order\n"
                                  "etl,customer,custno,custn-\n"))
    with pytest.raises(ValueError) as excinfo:
        validate_rules_references(master, bad)
    assert "'custn'" in str(excinfo.value) and "custn-" not in str(excinfo.value)


# ------------------------------------------------------------------ pending ledger
def test_pending_ledger(tmp_path):
    p = tmp_path / "_pending.yaml"
    findings = [{"folder": "oracle", "table": "customer", "column": "deptcode"}]
    update_pending(findings, p)
    update_pending(findings, p)
    pend = read_pending(p)
    key = "oracle.customer.deptcode"
    assert pend[key]["occurrences"] == 2, pend
    assert "first_seen" in pend[key] and "last_seen" in pend[key]

    master = {"oracle.customer": {"columns": {"deptcode": "varchar"},
                                  "descriptions": {}, "flags": {}}}
    resolved = reconcile_pending(master, p)
    assert resolved == [key], resolved
    assert read_pending(p) == {}
