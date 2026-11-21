# dpl/lib/folder_runner.py
"""Bounded-concurrency folder scheduler for parallel extraction.

Phase 3 of the parallel-extraction design (design v3, LOCKED).

Plan shape (from extraction_config.build_plan, Phase 1):
    plan["ungrouped"]      -> connection names with no serial_group
    plan["serial_groups"] -> {group_name: [member connection names]}
    plan["connections"]    -> per-connection validated summary (has folder)

jobs_from_plan converts a plan into jobs:
    - each ungrouped folder      -> one single-step job
    - each serial group          -> ONE chain job: member folders run
      strictly one after another (they share a physical box - that is
      the serial_group contract), while the chain as a whole occupies
      one slot in the concurrency pool.

run_jobs executes jobs with at most max_parallel folders in flight.

Doctrine carried to folder level:
    - continue-on-failure: a folder that raises records ("failed", exc)
      and never disturbs its siblings or the run; run_jobs itself does
      not raise task exceptions.
    - results are keyed by FOLDER name (globally unique - the loader's
      folder validation guarantees 1:1 connection:folder, so no key
      collisions are possible).
    - threading model: extraction is I/O-bound (socket waits release
      the GIL), so a ThreadPoolExecutor sized to the cap is the right
      tool; workers return per-job outcome dicts and touch no shared
      mutable state.

This module knows nothing about Oracle, LDAP, or drivers: tasks are
plain callables. Phase 4 supplies them (per-folder extract closures).
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Any

from dpl.lib.logger import get_run_logger

__func_msg__ = {
    "name": "folder runner",
    "invocation": "",
    "category": "extraction",
    "purpose": (
        "Bounded-concurrency folder scheduler for parallel extraction:\n"
        "   1. converts the validated run plan into jobs,\n"
        "   2. runs ungrouped connections concurrently, serial groups in order,\n"
        "   3. bounded by max_parallel_connections at all times"
    ),
}

log = get_run_logger("dpl.folder_runner")

Task = Callable[[], Any]
Steps = list[tuple[str, Task]]
Job = tuple[str, Steps]


def jobs_from_plan(plan: dict,
                   make_task: Callable[[str], Task]) -> list[Job]:
    """Convert a validated plan into schedulable jobs.

    make_task(folder_name) builds the callable that extracts that one
    folder (Phase 4 wires the real extractor here; tests wire fakes).
    Chain jobs preserve member order as declared in the config.
    """
    conns = plan["connections"]
    jobs: list[Job] = []

    for conn in plan["ungrouped"]:
        folder = conns[conn]["folder"]
        jobs.append((f"folder:{folder}", [(folder, make_task(folder))]))

    for group in sorted(plan["serial_groups"]):
        steps: Steps = []
        for conn in plan["serial_groups"][group]:
            folder = conns[conn]["folder"]
            steps.append((folder, make_task(folder)))
        jobs.append((f"chain:{group}", steps))

    return jobs


def _run_steps(job_name: str, steps: Steps) -> dict[str, tuple[str, Any]]:
    """Run one job's steps in order, recording every outcome.

    A failed step is recorded and the NEXT step still runs: inside a
    chain, one folder dying must not starve its box-mates of their turn.
    """
    outcomes: dict[str, tuple[str, Any]] = {}
    for folder, task in steps:
        started = monotonic()
        try:
            value = task()
        except Exception as exc:  # noqa: BLE001 - folder-level continue-on-failure: every task outcome must be recorded, never propagated
            log.warning("[%s] folder FAILED after %.1fs: %s (%s: %s)",
                        job_name, monotonic() - started, folder,
                        type(exc).__name__, str(exc)[:300])
            outcomes[folder] = ("failed", exc)
            continue
        log.info("[%s] folder done in %.1fs: %s",
                 job_name, monotonic() - started, folder)
        outcomes[folder] = ("ok", value)
    return outcomes


def run_jobs(jobs: list[Job], max_parallel: int) -> dict[str, tuple[str, Any]]:
    """Execute jobs with at most max_parallel folders in flight.

    Returns {folder: ("ok", task_result) | ("failed", exception)}.
    Every task's outcome is recorded; task exceptions are captured, not
    propagated - the run-survives-a-folder doctrine.
    """
    results: dict[str, tuple[str, Any]] = {}
    if not jobs:
        return results

    with ThreadPoolExecutor(max_workers=max_parallel,
                            thread_name_prefix="dpl-x") as pool:
        futures = [pool.submit(_run_steps, name, steps)
                   for name, steps in jobs]
        for future in futures:
            results.update(future.result())

    return results