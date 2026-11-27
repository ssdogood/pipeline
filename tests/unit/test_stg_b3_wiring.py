"""PT-048 B3 tests: run.py wiring helpers + staging README invariant."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from dpl.wrapper.run import _ensure_staging_readme, _stg_connections_in_scope


def _pipes():
    return [
        SimpleNamespace(name="ora_db", tables=["a", "b"]),
        SimpleNamespace(name="ad_sync", tables=["x"]),
    ]


def test_scope_full_run() -> None:
    assert _stg_connections_in_scope(_pipes(), None, None) == [
        "ora_db",
        "ad_sync",
    ]


def test_scope_connection_only() -> None:
    assert _stg_connections_in_scope(_pipes(), "ad_sync", None) == ["ad_sync"]


def test_scope_tables_intersect() -> None:
    assert _stg_connections_in_scope(_pipes(), None, ["x"]) == ["ad_sync"]
    assert _stg_connections_in_scope(_pipes(), None, ["b", "a"]) == ["ora_db"]
    assert _stg_connections_in_scope(_pipes(), None, ["nope"]) == []


def test_readme_created_once_never_overwritten(tmp_path: Path) -> None:
    landing = tmp_path / "landing"
    _ensure_staging_readme(landing)
    first = (landing / "README.md").read_text(encoding="utf-8")
    assert "DO NOT hand-delete" in first
    assert "PERMANENTLY unreadable" in first  # the act-fast warning made it in

    # operator customizes; the next run must not touch it
    (landing / "README.md").write_text("custom operator text", encoding="utf-8")
    _ensure_staging_readme(landing)
    assert (landing / "README.md").read_text(encoding="utf-8") == "custom operator text"
