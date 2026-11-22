# tests/unit/test_generate_schemas.py
"""Unit tests for generate_schemas (one-session-per-connection refactor).

All tests are OFFLINE: the Oracle connection (connect) and the session
worker (run_folder_tables) are faked at the generate_schemas module
boundary. The tests pin four contracts:

  1. Grouping  - the generator wires one connection factory per
                 CONNECTION NAME, never per table; non-oracle connections
                 are filtered out before grouping.
  2. Worker    - run_folder_tables is called once per connection with
                 table_concurrency=1 and the connection's retries/backoff.
  3. Assembly  - successful outcomes extend the CSV rows in config order
                 (column_seq restarts at 1 per table); failed tables are
                 excluded and drive exit code 1.
  4. Task      - _introspect_task takes a LIVE connection, wraps the query
                 in SELECT * FROM (...) WHERE 1=0, emits lowercased column
                 names with 1-based column_seq, and RAISES on failure.
"""

from __future__ import annotations

import pytest

from dpl.lib import generate_schemas as gs


# ----------------------------------------------------------------- stubs
def _desc_row(name: str, type_code: int, precision=None, scale=None):
    """Legacy cx_Oracle-shape description row: indices 4/5 are precision/scale."""
    return (name, type_code, None, None, precision, scale)


class _StubCursor:
    def __init__(self, conn, rows):
        self._conn = conn
        self._rows = rows

    def execute(self, sql):
        self._conn.executed.append(sql)
        self.description = self._rows

    def close(self):
        pass


class _StubConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []
        self.closed = False

    def cursor(self):
        return _StubCursor(self, self._rows)

    def close(self):
        self.closed = True


# NUMBER(10,0) -> Int64 via the legacy type_code path; VARCHAR2 -> Utf8.
LEGACY_DESC = [
    _desc_row("ACCT_ID", 2, 10, 0),
    _desc_row("ACCT_NAME", 1),
]

CFG = {
    "connections": {
        "app1": {"type": "oracle", "folder": "f1", "retries": 3,
                 "retry_backoff_seconds": 7},
        "app2": {"type": "oracle", "folder": "f2"},   # knobs omitted -> defaults
        "ldap_src": {"type": "ldap", "folder": "fx"},  # must be filtered out
    },
    "tables": [
        {"connection": "app1", "schema": "ETL", "name": "T1",
         "query": "SELECT ACCT_ID, ACCT_NAME FROM ETL.T1"},
        {"connection": "app1", "schema": "ETL", "name": "T2",
         "query": "SELECT ACCT_ID, ACCT_NAME FROM ETL.T2"},
        {"connection": "app2", "schema": "HR", "name": "T3",
         "query": "SELECT ACCT_ID, ACCT_NAME FROM HR.T3"},
        {"connection": "ldap_src", "schema": "AD", "name": "T4",
         "query": "not-an-oracle-table"},
    ],
}


# ------------------------------------------------------------ fixtures
@pytest.fixture
def env(monkeypatch, tmp_path):
    """Neutralize startup: config, settings, vault, secrets layout."""
    monkeypatch.setattr(gs, "load_config", lambda *a, **k: dict(CFG))
    monkeypatch.setattr(gs, "load_settings",
                        lambda *a, **k: {})  # pass_mgr section retired (PT-040 4a)
    monkeypatch.setattr(gs, "validate_secrets_layout", lambda: None)
    # PT-040 4a: generator resolves the env folder itself - point the
    # resolver at a scratch tree, never this machine's real config/.
    from dpl.lib.environment import ResolvedEnvironment
    scratch = ResolvedEnvironment(
        name="development",
        folder=tmp_path / "config" / "development")
    monkeypatch.setattr(gs, "resolve_environment", lambda: scratch)
    monkeypatch.setattr(gs.pass_mgr, "init", lambda _e: None)


def patch_connect(monkeypatch, calls: list[str]):
    """Fake connect(): records connection names, returns a stub session."""
    def fake_connect(cfg, name, **kwargs):
        calls.append(name)
        return _StubConn(LEGACY_DESC)
    # PT-034: connections dispatch via INTROSPECTORS, not a module
    # attribute - patch the oracle entry; setitem restores on teardown.
    monkeypatch.setitem(gs.INTROSPECTORS["oracle"], "connect", fake_connect)


def patch_worker(monkeypatch, calls: list[dict],
                 failing: set[str] | frozenset[str] = frozenset()):
    """Fake run_folder_tables(): runs each task on one stub session,
    marking the named tables as failed (outcome shape mirrors session_pool)."""

    def fake_run(label, tasks, factory, table_concurrency=1,
                 retries=2, backoff_seconds=5):
        calls.append({
            "label": label,
            "tables": [name for name, _fn in tasks],
            "table_concurrency": table_concurrency,
            "retries": retries,
            "backoff_seconds": backoff_seconds,
        })
        conn = factory()
        outcomes = []
        for name, fn in tasks:
            if name in failing:
                outcomes.append({"status": "failed",
                                 "error": "boom", "attempts": 2})
                continue
            try:
                outcomes.append({"status": "ok", "result": fn(conn)})
            except Exception as exc:  # noqa: BLE001 - test boundary
                outcomes.append({"status": "failed",
                                 "error": str(exc), "attempts": 1})
        return outcomes

    monkeypatch.setattr(gs, "run_folder_tables", fake_run)
    return calls


_HEADER = ",".join(gs.SCHEMA_MASTER_COLUMNS)


def _data_lines(csv_text: str) -> list[str]:
    """Data rows only: drop the '#' comment line AND the CSV header row."""
    return [
        ln for ln in csv_text.splitlines()
        if ln and not ln.startswith("#") and ln != _HEADER
    ]


# ------------------------------------------------- grouping / worker
def test_one_session_per_connection_and_worker_contract(env, monkeypatch, tmp_path):
    connects: list[str] = []
    worker_calls: list[dict] = []
    patch_connect(monkeypatch, connects)
    patch_worker(monkeypatch, worker_calls)

    rc = gs.generate_schemas("cfg.yaml", str(tmp_path / "master.csv"),
                                force=True, dry_run=False, source_type="oracle")

    assert rc == 0
    # 3 oracle tables on 2 connections -> 2 connect() calls, not 3;
    # the ldap connection is filtered out entirely.
    assert sorted(connects) == ["app1", "app2"]
    # worker invoked once per connection, tasks in config order
    assert worker_calls == [
        {"label": "app1", "tables": ["T1", "T2"], "table_concurrency": 1,
         "retries": 3, "backoff_seconds": 7},
        {"label": "app2", "tables": ["T3"], "table_concurrency": 1,
         "retries": 2, "backoff_seconds": 5},   # defaults: knobs omitted
    ]


# ------------------------------------------------------- assembly / CSV
def test_rows_written_in_config_order_with_per_table_seq(env, monkeypatch, tmp_path):
    patch_connect(monkeypatch, [])
    patch_worker(monkeypatch, [])

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(tmp_path / "master.csv"),
                                    force=True, dry_run=False, source_type="oracle")
    assert rc == 0

    lines = _data_lines(out.read_text(encoding="utf-8"))
    # 3 tables x 2 columns; folder/connection/schema/table from config,
    # column_seq restarts at 1 per table, dtype as emitted by the mapper
    # (dtypes are lowercased only at LOAD time, not at generation).
    assert lines == [
        "f1,app1,ETL,T1,1,acct_id,Int64,,",
        "f1,app1,ETL,T1,2,acct_name,Utf8,,",
        "f1,app1,ETL,T2,1,acct_id,Int64,,",
        "f1,app1,ETL,T2,2,acct_name,Utf8,,",
        "f2,app2,HR,T3,1,acct_id,Int64,,",
        "f2,app2,HR,T3,2,acct_name,Utf8,,",
    ]


def test_failed_table_excluded_exit_code_1(env, monkeypatch, tmp_path):
    patch_connect(monkeypatch, [])
    patch_worker(monkeypatch, [], failing={"T2"})

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(tmp_path / "master.csv"),
                                    force=True, dry_run=False, source_type="oracle")

    assert rc == 1                       # partial failure -> exit 1
    lines = _data_lines(out.read_text(encoding="utf-8"))
    assert lines == [                    # T2 columns absent, siblings present
        "f1,app1,ETL,T1,1,acct_id,Int64,,",
        "f1,app1,ETL,T1,2,acct_name,Utf8,,",
        "f2,app2,HR,T3,1,acct_id,Int64,,",
        "f2,app2,HR,T3,2,acct_name,Utf8,,",
    ]


def test_all_failed_returns_1_and_writes_nothing(env, monkeypatch, tmp_path):
    patch_connect(monkeypatch, [])
    patch_worker(monkeypatch, [], failing={"T1", "T2", "T3"})

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(tmp_path / "master.csv"),
                                    force=True, dry_run=False, source_type="oracle")

    assert rc == 1
    assert not out.exists()              # nothing to write - never publish empty


def test_dry_run_writes_nothing(env, monkeypatch, tmp_path):
    patch_connect(monkeypatch, [])
    patch_worker(monkeypatch, [])

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(tmp_path / "master.csv"),
                                    force=True, dry_run=True, source_type="oracle")

    assert rc == 0
    assert not out.exists()


# --------------------------------------------------------- the task itself
def test_task_builds_wrapped_query_and_rows():
    table_cfg = CFG["tables"][0]                     # T1 on app1
    conn = _StubConn(LEGACY_DESC)

    rows = gs._introspect_task(CFG, table_cfg, gs.oracle_dtype_to_comma_free)(conn)

    # the introspection wrapper is present in the executed SQL
    assert conn.executed == [
        "SELECT * FROM (SELECT ACCT_ID, ACCT_NAME FROM ETL.T1) WHERE 1=0"
    ]
    assert [r["column_seq"] for r in rows] == ["1", "2"]
    assert [r["column_name"] for r in rows] == ["acct_id", "acct_name"]
    assert [r["dtype"] for r in rows] == ["Int64", "Utf8"]
    for r in rows:
        assert r["folder"] == "f1"
        assert r["connection_name"] == "app1"
        assert r["schema"] == "ETL"
        assert r["table_name"] == "T1"
        assert r["description"] == ""
        assert r["policy_flags"] == ""


def test_task_raises_on_cursor_error():
    table_cfg = CFG["tables"][0]
    task = gs._introspect_task(CFG, table_cfg, gs.oracle_dtype_to_comma_free)

    class _ExplodingConn(_StubConn):
        def cursor(self):
            cur = super().cursor()

            def boom(sql):
                raise RuntimeError("ORA-00942")

            cur.execute = boom
            return cur

    with pytest.raises(RuntimeError, match="ORA-00942"):
        task(_ExplodingConn(LEGACY_DESC))

# ── PT-036: paused tables skip introspection (PT-014 complement) ──

_EVERGREEN = {"start": "1970-01-01", "end": "2999-12-31",
              "reason": "paused for test"}


def _pg_cfg(paused_conns=(), paused_tables=()):
    """Fresh CFG literal (no shared-state mutation) with chosen pauses.
    Window start 1970 / end 2999 => PAUSED on any real today()."""
    conns = {
        "app1": {"type": "oracle", "folder": "f1", "retries": 3,
                 "retry_backoff_seconds": 7},
        "app2": {"type": "oracle", "folder": "f2"},
        "ldap_src": {"type": "ldap", "folder": "fx"},
    }
    tables = []
    for conn, schema, name, q in [
        ("app1", "ETL", "T1", "SELECT ACCT_ID FROM ETL.T1"),
        ("app1", "ETL", "T2", "SELECT ACCT_ID FROM ETL.T2"),
        ("app2", "HR", "T3", "SELECT ACCT_ID FROM HR.T3"),
        ("ldap_src", "AD", "T4", "not-an-oracle-table"),
    ]:
        t: dict = {"connection": conn, "schema": schema, "name": name,
                   "query": q}
        if conn in paused_conns or name in paused_tables:
            t["pause"] = dict(_EVERGREEN)
        tables.append(t)
    return {"connections": conns, "tables": tables}


def test_generator_silently_skips_paused_tables(env, monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "load_config",
                        lambda *a, **k: _pg_cfg(paused_conns=("app1",)))
    connects: list[str] = []
    worker_calls: list[dict] = []
    patch_connect(monkeypatch, connects)
    patch_worker(monkeypatch, worker_calls)

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(out), force=True,
                             dry_run=False, source_type="oracle")

    assert rc == 0
    assert connects == ["app2"]     # app1 fully paused -> NO session
    assert worker_calls == [
        {"label": "app2", "tables": ["T3"], "table_concurrency": 1,
         "retries": 2, "backoff_seconds": 5},
    ]
    csv_text = out.read_text(encoding="utf-8")
    assert "T3" in csv_text and "T1" not in csv_text and "T2" not in csv_text


def test_generator_all_paused_exit_zero_no_connection(env, monkeypatch,
                                                      tmp_path):
    monkeypatch.setattr(gs, "load_config",
                        lambda *a, **k: _pg_cfg(paused_conns=("app1",
                                                              "app2",
                                                              "ldap_src")))
    connects: list[str] = []
    worker_calls: list[dict] = []
    patch_connect(monkeypatch, connects)
    patch_worker(monkeypatch, worker_calls)

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(out), force=True,
                             dry_run=False, source_type="oracle")

    assert rc == 0                    # the "no complaints" ruling
    assert connects == []              # NO connection opened at all
    assert worker_calls == []
    assert not out.exists()            # master left untouched


def test_generator_override_introspects_paused_tables(env, monkeypatch,
                                                      tmp_path):
    monkeypatch.setattr(gs, "load_config",
                        lambda *a, **k: _pg_cfg(paused_conns=("app1",)))
    connects: list[str] = []
    worker_calls: list[dict] = []
    patch_connect(monkeypatch, connects)
    patch_worker(monkeypatch, worker_calls)

    out = tmp_path / "master.csv"
    rc = gs.generate_schemas("cfg.yaml", str(out), force=True,
                             dry_run=False, source_type="oracle",
                             paused_override=True)

    assert rc == 0
    assert sorted(connects) == ["app1", "app2"]   # full service restored
    assert [c["label"] for c in worker_calls] == ["app1", "app2"]