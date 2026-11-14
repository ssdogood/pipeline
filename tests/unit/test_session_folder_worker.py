# tests/unit/test_session_folder_worker.py
"""Unit tests for run_folder_tables - the two-lane folder worker.

Hermetic, offline: the same _FakeCM factory pattern as
test_session_pool.py; an injected sleeper plays the clock. Proves:
config-order results under concurrency, per-lane isolation, guaranteed
session shutdown, attempts accounting on both paths.
"""
from __future__ import annotations

import threading

from dpl.lib.session_pool import run_folder_tables

# ------------------------------------------------------------------ fakes ---

class _FakeCM:
    def __init__(self):
        self.conn = object()
        self.exited = False

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc) -> bool:
        self.exited = True
        return False


def _factory():
    created: list[_FakeCM] = []

    def make() -> _FakeCM:
        cm = _FakeCM()
        created.append(cm)
        return cm

    return make, created


def _no_sleep(_seconds: float) -> None:
    pass


# ------------------------------------------------------------------- tests ---

def test_serial_default_one_lane_all_tables():
    make, created = _factory()
    order: list[str] = []

    def _named(n: int):
        def task(c):
            order.append(f"t{n}")
            return "ok"
        return task

    tasks = [(f"t{n}", _named(n)) for n in range(4)]

    out = run_folder_tables("f", tasks, make, table_concurrency=1,
                            sleeper=_no_sleep)

    assert [o["name"] for o in out] == ["t0", "t1", "t2", "t3"]
    assert [o["status"] for o in out] == ["ok"] * 4
    assert [o["attempts"] for o in out] == [1, 1, 1, 1]
    assert len(created) == 1                    # ONE lane, ONE session
    assert created[0].exited                    # session closed


def test_conc_two_creates_two_sessions_and_truly_overlaps():
    # Barrier requires BOTH lanes inside tasks simultaneously:
    # sequential serving would deadlock it and fail this test loudly.
    make, created = _factory()
    barrier = threading.Barrier(2, timeout=5)

    def task(conn):
        barrier.wait()
        return "ok"

    tasks = [("a", task), ("b", task)]
    out = run_folder_tables("f", tasks, make, table_concurrency=2,
                            sleeper=_no_sleep)

    assert all(o["status"] == "ok" for o in out)
    assert len(created) == 2                    # two lanes, two sessions
    assert all(cm.exited for cm in created)


def test_results_in_config_order_despite_completion_order():
    # t0 is slow, t1 fast: t1 finishes FIRST, but results must come
    # back in config order.
    make, _ = _factory()

    def slow(conn):
        threading.Event().wait(0.25)
        return "slow-result"

    def fast(conn):
        return "fast-result"

    out = run_folder_tables("f", [("t0", slow), ("t1", fast)], make,
                            table_concurrency=2, sleeper=_no_sleep)
    assert [(o["name"], o["result"]) for o in out] == \
        [("t0", "slow-result"), ("t1", "fast-result")]


def test_failed_table_does_not_block_queue_siblings():
    make, _ = _factory()

    def boom(conn):
        raise RuntimeError("ORA-00942: table or view does not exist")

    tasks = [("t0", boom), ("t1", lambda c: "ok1"), ("t2", lambda c: "ok2")]
    out = run_folder_tables("f", tasks, make, table_concurrency=1,
                            retries=2, sleeper=_no_sleep)

    assert out[0]["status"] == "failed"
    assert out[0]["attempts"] == 1              # data-class: NO retry
    assert "ORA-00942" in out[0]["error"]
    assert [o["status"] for o in out[1:]] == ["ok", "ok"]


def test_session_class_failure_retries_in_lane_and_recovers():
    make, _ = _factory()
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("ORA-03113: end-of-file")
        return "recovered"

    out = run_folder_tables("f", [("t0", flaky)], make,
                                        table_concurrency=1, retries=2,
                                        sleeper=_no_sleep)
    assert out[0]["status"] == "ok"
    assert out[0]["attempts"] == 2
    assert out[0]["result"] == "recovered"


def test_exhausted_session_retries_record_attempts_and_fail():
    make, _ = _factory()

    def always_dead(conn):
        raise ConnectionResetError("ORA-03135: connection lost contact")

    out = run_folder_tables("f", [("t0", always_dead)], make,
                            table_concurrency=1, retries=2,
                            sleeper=_no_sleep)
    assert out[0]["status"] == "failed"
    assert out[0]["attempts"] == 3              # 1 initial + 2 retries
    assert "ORA-03135" in out[0]["error"]


def test_lane_failure_isolated_from_other_lane():
    # two lanes; lane holding t0 hits a data-class error - the other
    # lane's table is unaffected and still succeeds.
    make, _ = _factory()

    def boom(conn):
        raise ValueError("bad data, not transport")

    tasks = [("t0", boom), ("t1", lambda c: "ok")]
    out = run_folder_tables("f", tasks, make, table_concurrency=2,
                            retries=1, sleeper=_no_sleep)
    assert out[0]["status"] == "failed"
    assert out[1]["status"] == "ok"


def test_fewer_tables_than_lanes_opens_no_extra_session():
    make, created = _factory()
    out = run_folder_tables("f", [("only", lambda c: "ok")], make,
                            table_concurrency=2, sleeper=_no_sleep)
    assert out[0]["status"] == "ok"
    assert len(created) == 1                    # 1 task -> 1 lane


def test_all_sessions_closed_after_run_even_mid_crash():
    # a table exploding must still leave every lane's session closed
    make, created = _factory()

    def boom(conn):
        raise RuntimeError("table blew up mid-run")

    tasks = [(f"t{i}", boom if i == 0 else (lambda c: "ok"))
             for i in range(4)]
    run_folder_tables("f", tasks, make, table_concurrency=2,
                      retries=0, sleeper=_no_sleep)
    assert len(created) == 2
    assert all(cm.exited for cm in created)


def test_empty_task_list_returns_empty():
    make, created = _factory()
    assert run_folder_tables("f", [], make, table_concurrency=2,
                             sleeper=_no_sleep) == []
    assert created == []                        # nothing opened at all