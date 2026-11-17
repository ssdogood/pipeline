# tests/unit/test_manifest_compare.py
"""Unit tests for manifest_compare - the Phase 4 equivalence comparator.

Synthetic envelopes modeled on the real manifest format (sample shared
2026-11-12; corrected to duration_sec). No real data, no network.
"""
from __future__ import annotations

import json

from dpl.lib.manifest_compare import collect_manifests, compare

# ------------------------------------------------------------------ helpers

def _table(name: str, status: str = "extracted", rr: int = 100,
           rw: int = 100, error: str | None = None,
           path: str = "D:\\staging\\etl\\tbl") -> dict:
    return {
        "connection": "ora_db", "schema": "ETL", "name": name,
        "load_date": "2026-11-12", "status": status,
        "rows_read": rr, "rows_written": rw, "duration_sec": 1.5,
        "output_path": path, "error": error,
    }


def _envelope(tables: list[dict], folder: str = "etl",
              status: str = "success", pending: list | None = None,
              exit_code: int = 0,
              summary: dict | None = None) -> dict:
    env = {
        "run_id": "run_20261112_123522", "profile": "full",
        "started_at": "2026-11-12T08:35:37Z",
        "finished_at": "2026-11-12T09:41:17Z",
        "status": status, "pending_tables": pending or [],
        "folder": folder, "load_date": "2026-11-12", "tables": tables,
        "wrapper": {"exit_code": exit_code,
                    "run_finished_at": "2026-11-12T09:41:48Z"},
    }
    if summary:
        env.update(summary)
    return env


def _write(root, envelopes: dict[str, dict],
           filenames: dict[str, str] | None = None) -> None:
    """Write <root>/<folder>/<manifest file> for each folder."""
    filenames = filenames or {}
    for folder, env in envelopes.items():
        sub = root / folder
        sub.mkdir(parents=True, exist_ok=True)
        fname = filenames.get(folder, "_manifest_run_20261112_120000.json")
        (sub / fname).write_text(json.dumps(env, indent=2))


# -------------------------------------------------------------------- tests

def test_identical_except_volatile_fields_matches(tmp_path):
    tables = [_table("table1"), _table("table2")]
    _write(tmp_path / "base", {"etl": _envelope(tables)})
    cand = _envelope([_table("table1", rr=100), _table("table2", rr=100)])
    # volatile fields differ freely - must not affect the verdict
    cand["run_id"] = "run_20261113_999999"
    cand["started_at"] = "2026-11-12T10:00:00Z"
    cand["finished_at"] = "2026-11-12T10:20:00Z"
    for t in cand["tables"]:
        t["duration_sec"] = 99.9
    cand["wrapper"]["run_finished_at"] = "2026-11-12T10:20:30Z"
    _write(tmp_path / "cand", {"etl": cand})

    fatal, notes = compare(collect_manifests(tmp_path / "base"),
                           collect_manifests(tmp_path / "cand"))
    assert fatal == [] and notes == []


def test_path_separator_normalization_matches(tmp_path):
    tabs_b = [_table("table1", path="D:\\staging\\etl\\table1")]
    tabs_c = [_table("table1", path="D:/staging/etl/table1")]
    _write(tmp_path / "base", {"etl": _envelope(tabs_b)})
    _write(tmp_path / "cand", {"etl": _envelope(tabs_c)})
    fatal, notes = compare(collect_manifests(tmp_path / "base"),
                           collect_manifests(tmp_path / "cand"))
    assert fatal == [] and notes == []


def test_table_status_mismatch_is_fatal(tmp_path):
    _write(tmp_path / "base",
           {"etl": _envelope([_table("table1", status="extracted")])})
    _write(tmp_path / "cand",
           {"etl": _envelope([_table("table1", status="failed",
                                     error="boom")])})
    fatal, _ = compare(collect_manifests(tmp_path / "base"),
                      collect_manifests(tmp_path / "cand"))
    assert any("status" in f for f in fatal)


def test_missing_and_extra_tables_are_fatal(tmp_path):
    _write(tmp_path / "base",
           {"etl": _envelope([_table("table1"), _table("table2")])})
    _write(tmp_path / "cand",
           {"etl": _envelope([_table("table1"), _table("tableX")])})
    fatal, _ = compare(collect_manifests(tmp_path / "base"),
                      collect_manifests(tmp_path / "cand"))
    assert any("table2" in f and "MISSING" in f for f in fatal)
    assert any("tableX" in f and "ABSENT" in f for f in fatal)


def test_row_delta_is_a_note_not_fatal(tmp_path):
    # same-day intraday data drift: rows move, statuses hold
    _write(tmp_path / "base",
           {"etl": _envelope([_table("table1", rr=100, rw=100)])})
    _write(tmp_path / "cand",
           {"etl": _envelope([_table("table1", rr=102, rw=102)])})
    fatal, notes = compare(collect_manifests(tmp_path / "base"),
                           collect_manifests(tmp_path / "cand"))
    assert fatal == []
    assert any("rows_written" in n and "+2" in n for n in notes)


def test_folder_presence_mismatches_are_fatal(tmp_path):
    b = {"etl": _envelope([_table("table1")])}
    c = {"etl": _envelope([_table("table1")]),
         "ad-main": _envelope([_table("users")], folder="ad-main")}
    _write(tmp_path / "base", b)
    _write(tmp_path / "cand", c)
    fatal, _ = compare(collect_manifests(tmp_path / "base"),
                      collect_manifests(tmp_path / "cand"))
    assert any("'ad-main'" in f for f in fatal)


def test_exit_code_mismatch_is_fatal(tmp_path):
    _write(tmp_path / "base",
           {"etl": _envelope([_table("table1")], exit_code=0)})
    _write(tmp_path / "cand",
           {"etl": _envelope([_table("table1")], exit_code=1)})
    fatal, _ = compare(collect_manifests(tmp_path / "base"),
                      collect_manifests(tmp_path / "cand"))
    assert any("exit_code" in f for f in fatal)


def test_pending_tables_mismatch_is_fatal(tmp_path):
    _write(tmp_path / "base",
           {"etl": _envelope([_table("table1")], pending=[])})
    _write(tmp_path / "cand",
           {"etl": _envelope([_table("table1")], pending=["table1"])})
    fatal, _ = compare(collect_manifests(tmp_path / "base"),
                      collect_manifests(tmp_path / "cand"))
    assert any("pending_tables" in f for f in fatal)


def test_newest_manifest_in_a_folder_wins(tmp_path):
    # two manifest files in one folder: compare the newer (later run_id)
    older = _envelope([_table("table1")])
    newer = _envelope([_table("table1"), _table("table2")])
    _write(tmp_path / "base",
           {"etl": older},
           filenames={"etl": "_manifest_run_20261112_010000.json"})
    _write(tmp_path / "base",
           {"etl": newer},
           filenames={"etl": "_manifest_run_20261112_230000.json"})
    collected = collect_manifests(tmp_path / "base")
    assert len(collected["etl"]["tables"]) == 2   # the newer one


def test_summary_counts_compared_only_when_both_sides_have_them(tmp_path):
    base = _envelope([_table("table1")],
                     summary={"tables_total": 1, "tables_extracted": 1,
                              "tables_skipped": 0, "tables_failed": 0})
    same = _envelope([_table("table1")],
                     summary={"tables_total": 1, "tables_extracted": 1,
                              "tables_skipped": 0, "tables_failed": 0})
    drifted = _envelope([_table("table1")],
                        summary={"tables_total": 2, "tables_extracted": 2,
                                 "tables_skipped": 0, "tables_failed": 0})
    legacy = _envelope([_table("table1")])   # no summary keys (old code)

    fatal, _ = compare({"etl": base}, {"etl": same})
    assert fatal == []
    fatal, _ = compare({"etl": base}, {"etl": drifted})
    assert any("tables_total" in f for f in fatal)
    fatal, _ = compare({"etl": base}, {"etl": legacy})
    assert fatal == []      # old baselines without summaries compare fine