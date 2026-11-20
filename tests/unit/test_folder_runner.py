# tests/unit/test_folder_runner.py
"""Unit tests for folder_runner: real caps, chain serialization,
failure isolation, plan wiring.

Hermetic: tasks are fakes. Concurrency is PROVEN, not assumed - a
threading.Barrier only releases if `max_parallel` tasks truly overlap;
per-job counters prove chains never overlap. No network, no drivers.
"""
from __future__ import annotations

import threading

from dpl.lib.folder_runner import jobs_from_plan, run_jobs

# ------------------------------------------------------------------ fakes ---

class _Concurrent:
    """Counts simultaneous task entries; optional barrier forces overlap."""

    def __init__(self, barrier_parties: int | None = None):
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self._barrier = (threading.Barrier(barrier_parties, timeout=5)
                         if barrier_parties else None)

    def task(self):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            if self._barrier is not None:
                self._barrier.wait()     # releases only when parties meet
            else:
                threading.Event().wait(0.15)
        finally:
            with self._lock:
                self._active -= 1
        return "done"


def _job(folder: str, task):
    return (f"folder:{folder}", [(folder, task)])


def _recorder(order: list, name: str):
    def task():
        order.append(name)
        return name
    return task


def _plan():
    """Mirror of a validated plan: one free folder + one 2-member chain."""
    return {
        "max_parallel_connections": 2,
        "connections": {
            "ora_db": {"type": "oracle", "folder": "etl",
                       "table_concurrency": 1},
            "ad_main": {"type": "ldap", "folder": "ad-main",
                        "table_concurrency": 1, "serial_group": "box_a"},
            "ad_atm": {"type": "ldap", "folder": "ad-atm",
                       "table_concurrency": 1, "serial_group": "box_a"},
        },
        "serial_groups": {"box_a": ["ad_main", "ad_atm"]},
        "ungrouped": ["ora_db"],
        "tasks": 2,
        "worst_case_sessions": 2,
    }


# ------------------------------------------------------- concurrency caps ---

def test_cap_workers_truly_run_simultaneously():
    # 3 jobs, cap 3: the barrier only releases if all three overlap.
    # Sequential execution would break the barrier and fail loudly.
    tracker = _Concurrent(barrier_parties=3)
    jobs = [_job(f, tracker.task) for f in ("a", "b", "c")]
    results = run_jobs(jobs, 3)
    assert all(status == "ok" for status, _ in results.values())
    assert tracker.peak == 3


def test_cap_is_respected_under_pressure():
    # 6 jobs, cap 2, 150ms tasks: both slots overlap, never three.
    tracker = _Concurrent()
    jobs = [_job(f, tracker.task) for f in "abcdef"]
    run_jobs(jobs, 2)
    assert tracker.peak == 2


def test_cap_one_is_strictly_sequential():
    tracker = _Concurrent()
    run_jobs([_job(f, tracker.task) for f in ("a", "b", "c")], 1)
    assert tracker.peak == 1


# ------------------------------------------------------------ chain rules ---

def test_chain_steps_never_overlap():
    # a chain runs in ONE worker slot: entries can never overlap even
    # though the pool offers 4 slots to it.
    tracker = _Concurrent()
    steps = [(f"f{i}", tracker.task) for i in range(3)]
    results = run_jobs([("chain:box_a", steps)], 4)
    assert tracker.peak == 1
    assert len(results) == 3
    assert all(outcome == ("ok", "done") for outcome in results.values())


def test_chain_preserves_config_order():
    order: list = []
    steps = [(f, _recorder(order, f)) for f in ("a", "b", "c")]
    run_jobs([("chain:g", steps)], 2)
    assert order == ["a", "b", "c"]


# ------------------------------------------------------ failure isolation ---

def test_failed_folder_does_not_kill_others():
    def boom():
        raise RuntimeError("simulated folder crash")

    jobs = [_job("bad", boom), _job("good", lambda: "fine")]
    results = run_jobs(jobs, 2)
    assert results["good"] == ("ok", "fine")
    status, exc = results["bad"]
    assert status == "failed"
    assert isinstance(exc, RuntimeError)


def test_chain_continues_after_failed_step():
    ran: list = []

    def boom():
        ran.append("a")
        raise ValueError("step one dies")

    steps = [("a", boom), ("b", _recorder(ran, "b"))]
    results = run_jobs([("chain:g", steps)], 2)
    assert "b" in ran                       # box-mate still got its turn
    assert results["a"][0] == "failed"
    assert results["b"][0] == "ok"


def test_empty_jobs_returns_empty():
    assert run_jobs([], 4) == {}


# ---------------------------------------------------------- plan wiring ---

def test_jobs_from_plan_splits_ungrouped_and_chains():
    made: list = []

    def make_task(folder):
        made.append(folder)
        return lambda: folder

    jobs = jobs_from_plan(_plan(), make_task)
    assert [name for name, _ in jobs] == ["folder:etl", "chain:box_a"]
    assert made == ["etl", "ad-main", "ad-atm"]   # config order preserved
    chain = dict(jobs)["chain:box_a"]
    assert [folder for folder, _ in chain] == ["ad-main", "ad-atm"]


def test_plan_to_execution_end_to_end():
    # the full Phase 4 handshake, with fakes standing in for extractors
    plan = _plan()
    jobs = jobs_from_plan(plan, lambda f: (lambda: f.upper()))
    results = run_jobs(jobs, 2)
    assert results == {
        "etl": ("ok", "ETL"),
        "ad-main": ("ok", "AD-MAIN"),
        "ad-atm": ("ok", "AD-ATM"),
    }