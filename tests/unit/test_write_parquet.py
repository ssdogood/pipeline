"""Unit tests for dpl.lib.write_parquet (ported from embedded smoke tests).

Provenance: these were the test_* functions that lived at the bottom of
src/dpl/lib/write_parquet.py, run manually via
`python -m dpl.lib.write_parquet`. Moved here so test code never ships in
the production wheel. Assertions are unchanged; the harness changed:
pytest's tmp_path fixture (fresh per test) replaces the hand-rolled
TemporaryDirectory, and print("[PASS] ...") lines were dropped.

Coverage summary:
  - cleansing (uppercase + whitespace collapse), Unicode safety
  - dedup: none / all / include / exclude modes, earliest-arrival-wins
  - sort: single + mixed directions, stable tiebreak by arrival order
  - failure modes: typo'd dedup/sort columns, excluded-everything,
    midstream generator death, zero rows
  - multi-part splits and stale-part clearing on rerun

Run with:  pytest tests/unit/test_write_parquet.py -v
"""

from __future__ import annotations

import random
import string
from datetime import date
from pathlib import Path

import polars as pl

from dpl.lib.write_parquet import write_table


# ------------------------------------------------------------------ helpers
def _writer_settings(root: Path) -> dict:
    """Settings dict pointing landing/scratch inside the test's tmp_path."""
    return {
        "paths": {
            "landing_root": str(root / "landing"),
            "scratch_root": str(root / "scratch"),
        },
        "duckdb": {"memory_limit": "2GB", "threads": 2},
        "writer": {
            "compression": "zstd",
            "compression_level": 3,
            "max_file_mb": 512,
            "keep_scratch_on_success": False,
        },
    }


def _read_back(path_pattern: str) -> pl.DataFrame:
    """Polars lazy read-back for efficiency."""
    return pl.scan_parquet(path_pattern).collect()


def _glob_table(table: str, settings: dict, load_date: date) -> Path:
    return Path(settings["paths"]["landing_root"]) / load_date.isoformat() \
        / "oracle" / table


# ------------------------------------------------------------------ cleansing
def test_basic_cleanse_unicode(tmp_path):
    """Core: strings transformed, numerics untouched, Unicode preserved.

    Greek, Cyrillic, Japanese alongside English to verify upper/whitespace
    transformations handle multi-byte characters correctly. Also asserts
    include-mode dedup with an uppercase column name.
    """
    cfg = {"transforms": {"uppercase": True, "collapse_whitespace": True}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [
        pl.DataFrame({
            "code": [" ab ", "AB"],
            "desc_en": ["x  y\tz", "abc"],
            "desc_el": ["α β γ", "δ  ε  ζ"],     # Greek
            "desc_ru": ["привет", "мир\tмир"],    # Cyrillic (tab inside)
            "desc_ja": ["あ い", "う え"],        # Japanese (double space)
            "amount": [1, 2],                    # numeric: untouched
        }),
        pl.DataFrame({
            "code": ["   aB   "],                 # near-dupe on `code` after upper
            "desc_en": ["X Y Z"],
            "desc_el": ["θ"],
            "desc_ru": ["тест"],
            "desc_ja": ["かきく"],
            "amount": [3],
        }),
    ]

    stats = write_table(
        cfg, settings, "oracle", "SMOKE1", load_date,
        iter(batches), dedup_columns=["CODE"],
    )

    assert stats["published"], f"publish failed: {stats}"
    assert stats["rows_read"] == 3, stats
    # all three rows share `code` 'AB' after cleansing -> 2 removed, 1 kept
    assert stats["rows_written"] == 1, stats
    assert stats["duplicates_removed"] == 2, stats
    assert stats["dedup"] is True
    assert stats["dedup_mode"] == "include", stats
    assert stats["dedup_columns"] == ["code"], stats

    out = _glob_table("smoke1", settings, load_date)
    parts = sorted(out.glob("smoke1-*.parquet"))
    assert [p.name for p in parts] == ["smoke1-0001.parquet"], parts

    df = _read_back(str(parts[0]))

    # the surviving row is the FIRST occurrence: batch 1, row 1 (earliest arrival)
    assert list(df["desc_en"].to_list()) == ["X Y Z"]
    assert list(df["desc_el"].to_list()) == ["Α Β Γ"]      # Greek upper, collapsed
    assert list(df["desc_ru"].to_list()) == ["ПРИВЕТ"]     # Cyrillic upper
    assert list(df["desc_ja"].to_list()) == ["あ い".upper()] or \
        list(df["desc_ja"].to_list()) == ["あ い"]         # ja has no case
    assert list(df["amount"].to_list()) == [1]             # numeric untouched

    # _stage_seq never reaches the published parquet
    assert "_stage_seq" not in df.columns, df.columns


def test_no_transforms(tmp_path):
    """With empty transforms, data lands exactly as sent."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"code": [" AbC "], "val": [1]})]

    stats = write_table(cfg, settings, "oracle", "no_xform", load_date, iter(batches))
    assert stats["published"] and stats["dedup"] is False
    assert "dedup_columns" not in stats and "duplicates_removed" not in stats, stats

    df = _read_back(str(_glob_table("no_xform", settings, load_date) / "*.parquet"))
    assert df["code"][0] == " AbC ", df["code"][0]   # unchanged
    assert "_stage_seq" not in df.columns, df.columns


# ------------------------------------------------------------------ dedup
def test_dedup_include_first_wins(tmp_path):
    """Include mode keeps the EARLIEST ARRIVAL per key (deterministic)."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"key": ["a", "a"], "payload": ["first", "second"]})]

    stats = write_table(cfg, settings, "oracle", "dedup_inc", load_date,
                        iter(batches), dedup_columns=["KEY"])

    assert stats["rows_read"] == 2 and stats["rows_written"] == 1, stats
    assert stats["duplicates_removed"] == 1, stats
    assert stats["dedup_mode"] == "include", stats

    df = _read_back(str(_glob_table("dedup_inc", settings, load_date) / "*.parquet"))
    assert df["payload"][0] == "first", "earliest arrival should survive"


def test_dedup_exclude_mode(tmp_path):
    """Exclude mode: dedup on all columns EXCEPT the listed ones.

    Rows differing ONLY in the excluded column must collapse.
    """
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({
        "key": ["a", "a", "b"],            # partition columns after exclusion
        "value": [10, 10, 20],
        "updated_at": ["t1", "t2", "t3"]   # excluded: differences here ignored
    })]

    stats = write_table(cfg, settings, "oracle", "dedup_exc", load_date,
                        iter(batches), dedup_columns=["-UPDATED_AT"])

    assert stats["dedup_mode"] == "exclude", stats
    assert stats["dedup_columns"] == ["key", "value"], stats
    # rows 1+2 identical on (key,value) -> collapse; row 3 kept
    assert stats["rows_written"] == 2, stats
    assert stats["duplicates_removed"] == 1, stats

    df = _read_back(str(_glob_table("dedup_exc", settings, load_date) / "*.parquet"))
    assert sorted(df["key"].to_list()) == ["a", "b"]
    # surviving 'a' row is the EARLIEST arrival -> its updated_at is t1
    a_rows = df.filter(pl.col("key") == "a")
    assert a_rows["updated_at"][0] == "t1"


def test_dedup_all_mode(tmp_path):
    """All mode: dedup on every column of the result set (no _stage_seq)."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({
        "a": ["x", "x", "y"],
        "b": [1, 1, 3],
    })]

    stats = write_table(cfg, settings, "oracle", "dedup_all", load_date,
                        iter(batches), dedup_columns="all")

    assert stats["dedup_mode"] == "all", stats
    assert stats["dedup_columns"] == ["a", "b"], stats
    assert stats["rows_written"] == 2, stats
    assert stats["duplicates_removed"] == 1, stats

    df = _read_back(str(_glob_table("dedup_all", settings, load_date) / "*.parquet"))
    assert len(df) == 2


def test_dedup_none_keyword(tmp_path):
    """Scalar 'none' behaves like omitting dedup_columns entirely."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": ["x", "x"]})]

    stats = write_table(cfg, settings, "oracle", "dedup_none", load_date,
                        iter(batches), dedup_columns="NONE")

    assert stats["dedup"] is False, stats
    assert stats["rows_written"] == 2, stats     # duplicate kept
    assert "dedup_columns" not in stats and "duplicates_removed" not in stats


def test_missing_dedup_col_raises(tmp_path):
    """Unknown include column raises ValueError; nothing published.

    Also verifies a typo'd EXCLUSION raises: both are equally dangerous.
    """
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)
    batches = [pl.DataFrame({"a": [1]})]

    # include-mode typo
    try:
        write_table(cfg, settings, "oracle", "bad_inc", load_date,
                    iter(batches), dedup_columns=["NOPE"])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e), f"error message: {e}"

    # exclude-mode typo
    try:
        write_table(cfg, settings, "oracle", "bad_exc", load_date,
                    iter(batches), dedup_columns=["-NOPE"])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e), f"error message: {e}"

    for tbl in ("bad_inc", "bad_exc"):
        assert not (_glob_table(tbl, settings, load_date)).exists()


def test_exclude_everything_raises(tmp_path):
    """Excluding all columns leaves no dedup key -> loud failure."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": [1]})]

    try:
        write_table(cfg, settings, "oracle", "exc_all", load_date,
                    iter(batches), dedup_columns=["-a"])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "no dedup key left" in str(e), f"error message: {e}"


# ------------------------------------------------------------------ sort
def test_sort_desc(tmp_path):
    """Single descending key: output non-increasing; stats record the spec."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"ts": [3, 1, 2], "id": [10, 20, 30]})]

    stats = write_table(cfg, settings, "oracle", "sort_desc", load_date,
                        iter(batches), sort_spec=[("ts", "desc")])

    assert stats["published"] and stats["sort_columns"] == ["ts"], stats
    df = _read_back(str(_glob_table("sort_desc", settings, load_date) / "*.parquet"))
    assert df["ts"].to_list() == [3, 2, 1], df["ts"].to_list()
    assert "_stage_seq" not in df.columns, df.columns


def test_sort_mixed_directions(tmp_path):
    """grp ASC then val DESC: primary first, secondary breaks ties reversed."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({
        "grp": ["b", "a", "a", "b"],
        "val": [1, 2, 1, 2],
    })]

    stats = write_table(cfg, settings, "oracle", "sort_mix", load_date,
                        iter(batches),
                        sort_spec=[("grp", "asc"), ("val", "desc")])

    assert stats["published"], stats
    df = _read_back(str(_glob_table("sort_mix", settings, load_date) / "*.parquet"))
    assert list(zip(df["grp"].to_list(), df["val"].to_list())) == \
        [("a", 2), ("a", 1), ("b", 2), ("b", 1)], df


def test_sort_tiebreak_is_arrival_order(tmp_path):
    """Equal sort keys keep arrival order across batches (stable sort)."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [
        pl.DataFrame({"key": ["x", "x"], "payload": ["p1", "p2"]}),
        pl.DataFrame({"key": ["x"], "payload": ["later"]}),
    ]

    write_table(cfg, settings, "oracle", "sort_tie", load_date,
                iter(batches), sort_spec=[("key", "asc")])

    df = _read_back(str(_glob_table("sort_tie", settings, load_date) / "*.parquet"))
    assert df["payload"].to_list() == ["p1", "p2", "later"], df["payload"].to_list()


def test_sort_typo_raises(tmp_path):
    """Unknown sort column raises ValueError; nothing published."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": [1]})]

    try:
        write_table(cfg, settings, "oracle", "bad_sort", load_date,
                    iter(batches), sort_spec=[("nope", "asc")])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e), f"error message: {e}"

    assert not (_glob_table("bad_sort", settings, load_date)).exists()


# ------------------------------------------------------------------ splits
def test_multi_part_sorted_seam(tmp_path):
    """Forced split + sort: concatenated parts read back globally sorted."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    settings["writer"]["max_file_mb"] = 1  # force split
    load_date = date(2026, 10, 17)

    rnd = random.Random(42)
    rows = 40_000
    blob = ["".join(rnd.choices(string.ascii_letters, k=200)) for _ in range(rows)]
    # feed seq in REVERSE order so accidental pass-through fails the test
    batches = [pl.DataFrame({
        "seq": list(range(rows - 1, -1, -1)),
        "blob": blob,
    })]

    stats = write_table(cfg, settings, "oracle", "sort_split", load_date,
                        iter(batches), sort_spec=[("seq", "asc")])

    assert stats["published"] and stats["parts"] >= 2, f"expected split: {stats}"
    out = _glob_table("sort_split", settings, load_date)
    names = sorted(p.name for p in out.glob("sort_split-*.parquet"))
    assert names[0] == "sort_split-0001.parquet", names

    df = _read_back(str(out / "sort_split-*.parquet"))
    assert len(df) == rows, f"lost rows: {len(df)} vs {rows}"
    seqs = df["seq"].to_list()
    assert seqs == sorted(seqs), "rows not globally sorted across part seams"


def test_forced_split(tmp_path):
    """Force splitting at 1 MB; verify naming pattern and full row recovery."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    settings["writer"]["max_file_mb"] = 1  # force split
    load_date = date(2026, 10, 17)

    rnd = random.Random(42)
    rows = 40_000
    blob = ["".join(rnd.choices(string.ascii_letters, k=200)) for _ in range(rows)]
    batches = [pl.DataFrame({"seq": list(range(rows)), "blob": blob})]

    stats = write_table(cfg, settings, "oracle", "split", load_date, iter(batches))

    assert stats["published"] and stats["parts"] >= 2, f"expected split: {stats}"
    out = _glob_table("split", settings, load_date)
    names = sorted(p.name for p in out.glob("split-*.parquet"))
    assert names[0] == "split-0001.parquet", names

    df = _read_back(str(out / "*.parquet"))
    assert len(df) == rows, f"lost rows: {len(df)} vs {rows}"
    assert "_stage_seq" not in df.columns, df.columns


# ------------------------------------------------------------------ lifecycle
def test_zero_rows(tmp_path):
    """Empty input -> publish nothing, no folder."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    stats = write_table(cfg, settings, "oracle", "empty_tbl", load_date, iter([]))

    assert not stats["published"] and stats["rows_read"] == 0
    assert not _glob_table("empty_tbl", settings, load_date).exists()


def test_midstream_failure(tmp_path):
    """Generator raises mid-stream: nothing published, scratch cleaned.

    Exercises __exit__: the facade's `with` propagates the generator's
    error, the abandoned writer tears down its DuckDB session and tmp dir.
    """
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)

    def failing_batches():
        yield pl.DataFrame({"a": [1, 2]})
        raise RuntimeError("simulated connection drop")

    try:
        write_table(cfg, settings, "oracle", "fail_mid", load_date,
                    failing_batches())
    except RuntimeError:
        pass

    assert not _glob_table("fail_mid", settings, load_date).exists()
    assert list((Path(settings["paths"]["scratch_root"])).glob("tmp_*")) == []


def test_rerun_clears_stale_parts(tmp_path):
    """Stale parts from a prior larger --force run are cleared on rerun."""
    cfg = {"transforms": {}}
    settings = _writer_settings(tmp_path)
    load_date = date(2026, 10, 17)
    batches = [pl.DataFrame({"a": list(range(10))})]

    stats = write_table(cfg, settings, "oracle", "rerun", load_date, iter(batches))
    out = Path(stats["output_dir"])

    # plant stale part
    (out / "rerun-9999.parquet").write_bytes(b"stale")

    stats2 = write_table(cfg, settings, "oracle", "rerun", load_date, iter(batches))
    names = sorted(p.name for p in out.glob("*.parquet"))
    assert "rerun-9999.parquet" not in names, names
    assert stats2["published"] and stats2["rows_written"] == 10, stats2
