# lib/write_parquet.py
"""Parquet writer: DuckDB staging, cleansing, sorting, atomic publish.

Pipeline per table:
    1. Stage Polars batches into DuckDB table `_stage` (spills to scratch
       beyond duckdb.memory_limit). Each batch is tagged with `_stage_seq`,
       a monotonically increasing arrival-order integer.
    2. Cleanse: uppercase + whitespace collapse on VARCHAR columns only;
       optional per-table dedup via QUALIFY row_number() ordered by
       _stage_seq (earliest arrival wins — deterministic)
    3. Optional sort: ORDER BY the rules' sort_spec, applied OUTERMOST in
       every write query (single part and each LIMIT/OFFSET slice), with
       an implicit _stage_seq ASC tiebreak (stable sort)
    4. Write parts to a scratch temp dir as {table}-NNNN.parquet
       (single part below max_file_mb; measured split above)
    5. Publish: clear stale *.parquet in the destination folder, move
       the new parts in, remove scratch

Dedup modes (parsed by lib.common.parse_dedup_spec):
    none    -> no dedup (dedup_columns omitted, or scalar 'none')
    all     -> dedup on every column (scalar 'all'; warned on wide tables)
    include -> dedup on the listed columns only
    exclude -> dedup on every column EXCEPT those listed ('-col' entries)

Sort spec (parsed by lib.common.parse_sort_spec):
    [(column, 'asc'|'desc'), ...] — position = precedence. Ties are broken
    by arrival order (_stage_seq): sorts are stable.

Guarantees:
    - Nothing appears in landing unless all parts completed
    - Tables with zero rows publish nothing (stats.published = False)
    - dedup/sort column names are matched case-insensitively against the
      result set; unknown names fail loudly in every mode
    - `_stage_seq` never appears in published parquet
    - Multi-part tables read back globally sorted when sort is active

Logging: lib/logger.py — "dpl.writer" under the central dpl tree.

Run smoke tests:  python -m lib.write_parquet   (from project root)
"""

from __future__ import annotations

import shutil
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
import polars as pl

from dpl.lib.common import landing_dir, parse_dedup_spec
from dpl.lib.logger import setup_logging, get_run_logger

log = get_run_logger("dpl.writer")


WIDE_TABLE_WARNING_COLS = 10   # warn when 'all'-mode dedup sees more columns

# ------------------------------------------------------------------ helpers
def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _duckdb_session(settings: dict, scratch_root: Path) -> duckdb.DuckDBPyConnection:
    """Fresh in-memory DuckDB session, configured verbatim from settings.yaml."""
    con = duckdb.connect()

    duck_cfg = settings.get("duckdb", {})
    if duck_cfg.get("memory_limit"):
        con.execute(f"SET memory_limit='{duck_cfg['memory_limit']}'")
    if duck_cfg.get("threads"):
        con.execute(f"SET threads={int(duck_cfg['threads'])}")

    spill_dir = scratch_root / "duckdb_spill"
    spill_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{spill_dir.as_posix()}'")

    return con

def _column_types(con, table: str = "_stage") -> list[tuple[str, str]]:
    """[(column_name, UPPER(type)), ...] for the named table."""
    desc = con.execute(f"DESCRIBE {table}").fetchall()
    return [(r[0], r[1].upper()) for r in desc]

def _resolve_dedup_columns(columns: list[tuple[str, str]],
                           parsed: dict) -> list[str] | None:
    """Turn a parsed dedup spec into the final partition column list.

    Returns None when mode is 'none'. Validates that every referenced
    column (directly listed or excluded) exists in the result set —
    unknown names fail loudly in BOTH modes, since a typo'd exclusion is
    just as dangerous as a typo'd inclusion (it silently widens or
    mis-targets the dedup key).

    Raises ValueError naming the missing columns and what was available.
    """
    mode = parsed["mode"]
    if mode == "none":
        return None

    available = [name.lower() for name, _typ in columns]

    if mode == "all":
        if len(available) > WIDE_TABLE_WARNING_COLS:
            log.warning(
                "dedup on ALL %d columns of a wide table: any single-column "
                "difference prevents deduplication and performance may suffer. "
                "Consider an exclusion list instead.",
                len(available),
            )
        return available

    spec_cols = parsed["columns"]

    if mode == "include":
        missing = [c for c in spec_cols if c not in available]
        if missing:
            raise ValueError(
                f"dedup_columns not in result set: {missing}. "
                f"Available columns: {sorted(available)}"
            )
        return spec_cols

    # exclude mode: complement of the excluded columns
    missing = [c for c in spec_cols if c not in available]
    if missing:
        raise ValueError(
            f"dedup_columns (exclusions) not in result set: {missing}. "
            f"Available columns: {sorted(available)}"
        )
    keep = [c for c in available if c not in set(spec_cols)]
    if not keep:
        raise ValueError(
            "dedup_columns excludes every column of the result set — "
            "there would be no dedup key left."
        )
    return keep

def _validate_sort_columns(sort_spec: list[tuple[str, str]],
                           columns: list[tuple[str, str]]) -> None:
    """Every sort column must exist in the result set. Loud failure."""
    available = {name.lower() for name, _typ in columns}
    missing = [c for c, _d in sort_spec if c not in available]
    if missing:
        raise ValueError(
            f"sort_order columns not in result set: {missing}. "
            f"Available columns: {sorted(available)}"
        )

def _order_by_clause(sort_spec: list[tuple[str, str]],
                     columns: list[tuple[str, str]]) -> str:
    """Build the ORDER BY clause for a table's sort spec.

    Returns '' when there is no sort (no ORDER BY at all — untouched
    tables keep their unsorted behavior). With a sort, the user keys are
    rendered in spec order (ASC/DESC from the spec) followed by an
    implicit '"_stage_seq" ASC' tiebreak: equal keys keep arrival order,
    making the sort stable and part contents reproducible.

    `columns` is the result set of the SELECT being ordered — used to
    validate that the tiebreak column is available.
    """
    if not sort_spec:
        return ""

    _validate_sort_columns(sort_spec, columns)

    terms = [
        f"{_quote_ident(c)} {'DESC' if d == 'desc' else 'ASC'}"
        for c, d in sort_spec
    ]
    available = {name.lower() for name, _typ in columns}
    if "_stage_seq" in available:
        terms.append('"_stage_seq" ASC')     # stable-sort tiebreak
    return "ORDER BY " + ", ".join(terms)

def _build_cleanse_sql(con, cfg: dict, parsed: dict) -> str:
    """Build the cleansing SELECT over _stage.

    Two layers, innermost to outermost:
      1. transforms (uppercase / collapse_whitespace on VARCHAR columns
         only) over ALL staged columns, _stage_seq included
      2. dedup: QUALIFY row_number() over the resolved key columns
         ORDER BY _stage_seq — earliest arrival wins, deterministically.

    _stage_seq is KEPT in the output of this SQL (the caller's _clean
    table carries it) — it is needed as the sort tiebreak. It is dropped
    from published parquet by the write-time projection in close().
    """
    transforms = cfg.get("transforms", {})
    upper = bool(transforms.get("uppercase"))
    collapse = bool(transforms.get("collapse_whitespace"))

    columns = _column_types(con)               # includes _stage_seq
    varchars = {name for name, typ in columns if typ == "VARCHAR"}

    exprs = []
    for name, _typ in columns:
        q = _quote_ident(name)
        if name in varchars and upper:
            q = f"upper({q})"
        if name in varchars and collapse:
            q = f"trim(regexp_replace({q}, '[[:space:]]+', ' ', 'g'))"
        exprs.append(f"{q} AS {_quote_ident(name)}")

    sql = f"SELECT {', '.join(exprs)} FROM _stage"

    # visible columns = result set WITHOUT the internal _stage_seq; dedup
    # keys are resolved against these (an 'all'-mode key must never
    # include the sequencing column)
    visible = [(n, t) for n, t in columns if n != "_stage_seq"]

    dedup_cols = _resolve_dedup_columns(visible, parsed)
    if dedup_cols:
        partition = ", ".join(_quote_ident(c) for c in dedup_cols)
        sql = (
            f"SELECT * FROM ({sql}) "
            f"QUALIFY row_number() OVER (PARTITION BY {partition} "
            f"ORDER BY _stage_seq) = 1"
        )

    return sql

def _copy(con, sql: str, path: Path, compression: str,
          level: int | None) -> None:
    """COPY ... TO parquet. Tries COMPRESSION_LEVEL, falls back if this
    DuckDB build rejects the option (warns once per attempt)."""
    opts = f"FORMAT PARQUET, COMPRESSION {compression}"
    if level is not None:
        try:
            con.execute(
                f"COPY ({sql}) TO '{path.as_posix()}' "
                f"({opts}, COMPRESSION_LEVEL {int(level)})"
            )
            return
        except duckdb.Error as e:
            log.warning(
                "COMPRESSION_LEVEL %d not accepted by this DuckDB (%s); "
                "using codec default", level, e,
            )
    con.execute(f"COPY ({sql}) TO '{path.as_posix()}' ({opts})")

def _write_parts(con, settings: dict, from_sql: str, order_by: str,
                 scratch_dir: Path, table: str) -> list[Path]:
    """Write {table}-NNNN.parquet parts into scratch_dir.

    Single file below max_file_mb; above it, parts are sized from the
    measured bytes-per-row of a first full write (two-pass split).

    `order_by` (possibly '') is applied at the OUTERMOST level of every
    data query — the single-part COPY and each LIMIT/OFFSET slice. An
    ORDER BY buried in a subquery is not contractual; putting it here,
    beside the LIMIT, is what makes part contents deterministically
    ordered and seams between parts correct.
    """
    writer_cfg = settings.get("writer", {})
    compression = writer_cfg.get("compression", "zstd").lower()
    level = writer_cfg.get("compression_level")
    max_bytes = int(writer_cfg.get("max_file_mb", 512)) * 1024 * 1024

    single = scratch_dir / f"{table}-0001.parquet"
    _copy(con, f"{from_sql} {order_by}", single, compression, level)

    size = single.stat().st_size
    if size <= max_bytes:
        return [single]

    # --- split: measure, then rewrite as parts (target 90% of max_bytes) ---
    single.unlink()

    # count on the unordered base — cheaper, ordering is irrelevant here
    total_rows = con.execute(f"SELECT count(*) FROM ({from_sql})").fetchone()[0]
    bytes_per_row = size / max(total_rows, 1)
    rows_per_part = max(1, int(max_bytes * 0.9 / bytes_per_row))
    num_parts = max(2, -(-total_rows // rows_per_part))  # ceil

    parts = []
    for i in range(num_parts):
        offset = i * rows_per_part
        part = scratch_dir / f"{table}-{i + 1:04d}.parquet"
        # ORDER BY sits WITH the LIMIT, at the outer level of the slice
        _copy(
            con,
            f"{from_sql} {order_by} LIMIT {rows_per_part} OFFSET {offset}",
            part, compression, level,
        )
        parts.append(part)

    return parts

# ----------------------------------------------------------------- public API
class ParquetWriter:
    """Streaming Parquet writer: append() batches, close() publishes atomically.

    Construction validates the dedup + sort specs immediately (typos fail
    before any I/O). The DuckDB session, scratch dir and _stage table are
    created lazily on the first non-empty append(); a writer that never
    receives data publishes nothing and costs nothing.

    Every appended batch is tagged with `_stage_seq` (arrival order,
    monotonic across batches). It drives deterministic dedup ("earliest
    arrival wins"), the stable-sort tiebreak, and never reaches the
    published parquet.
    """

    def __init__(self, settings: dict, source: str, table: str,
                 load_date: date, transforms: dict | None = None,
                 dedup_columns=None,
                 sort_spec: list[tuple[str, str]] | None = None) -> None:
        self.settings = settings
        self.cfg = {"transforms": transforms or {}}   # _build_cleanse_sql reads transforms
        self.source = source
        self.table = table.lower()
        self.load_date = load_date

        # Fail fast on malformed dedup specs — BEFORE any connection or I/O
        if isinstance(dedup_columns, dict) and "mode" in dedup_columns:
            self.parsed = dedup_columns
        else:
            self.parsed = parse_dedup_spec(dedup_columns)
        self.dedup_active = self.parsed["mode"] != "none"

        # Parsed sort spec from table_rules: [(column, 'asc'|'desc'), ...].
        # Structure already validated by the loader; column EXISTENCE is
        # checked here against the actual result set (this layer alone
        # sees it — same rule as dedup columns).
        self.sort_spec = list(sort_spec or [])
        self.sort_active = bool(self.sort_spec)

        self._keep_scratch = bool(
            settings.get("writer", {}).get("keep_scratch_on_success", False))

        self.scratch_root = Path(settings["paths"]["scratch_root"])
        self.landing_root = settings["paths"]["landing_root"]
        self.tmp_dir = self.scratch_root / f"tmp_{source}_{self.table}_{uuid.uuid4().hex[:8]}"
        self.dest_dir = landing_dir(self.landing_root, load_date, source) / self.table

        self._con: duckdb.DuckDBPyConnection | None = None
        self._have_stage = False
        self._closed = False
        self._rows_read = 0
        self._rows_staged = 0              # cumulative counter for _stage_seq
        self._start_wall = time.time()

        self.stats = {
            "name": self.table,
            "dedup": self.dedup_active,
            "rows_read": 0,
            "rows_written": 0,
            "published": False,
            "output_dir": None,
            "parts": 0,
            "start_time": _utc_now_iso(),
            "end_time": None,
            "duration_sec": 0.0,
            "compression": settings.get("writer", {})
                                 .get("compression", "zstd").lower(),
        }
        if self.dedup_active:
            self.stats["dedup_mode"] = self.parsed["mode"]
        if self.sort_active:
            self.stats["sort_columns"] = [c for c, _d in self.sort_spec]

    # ------------------------------------------------------------- streaming
    def append(self, df: pl.DataFrame) -> None:
        """Stage one batch into _stage. Empty frames are ignored."""
        if self._closed:
            raise RuntimeError(
                f"ParquetWriter for {self.source}.{self.table} already closed")
        if df is None or df.height == 0:
            return

        # tag arrival order: monotonic across all batches of this writer
        df = df.with_row_index("_stage_seq", offset=self._rows_staged)
        self._rows_staged += df.height

        try:
            if self._con is None:
                self.tmp_dir.mkdir(parents=True, exist_ok=True)
                self._con = _duckdb_session(self.settings, self.scratch_root)
            if not self._have_stage:
                self._con.execute("CREATE TABLE _stage AS SELECT * FROM df LIMIT 0")
                self._have_stage = True
            self._con.execute("INSERT INTO _stage SELECT * FROM df")
            self._rows_read += df.height
        except Exception:
            self._release(keep=False)   # no dangling DuckDB locks on failure
            raise

    def close(self) -> dict:
        """Finalize: cleanse, dedup, sort, write parts, publish atomically. -> stats."""
        if self._closed:
            return self.stats
        self._closed = True

        try:
            if self._con is None:
                log.info("%s.%s: zero rows, nothing published",
                         self.source, self.table)
                return _finish(self.stats, self._start_wall)

            clean_sql = _build_cleanse_sql(self._con, self.cfg, self.parsed)
            # _clean retains _stage_seq (needed for the sort tiebreak);
            # the write-time projection below drops it
            self._con.execute(f"CREATE TABLE _clean AS {clean_sql}")
            self.stats["rows_read"] = self._rows_read
            self.stats["rows_written"] = self._con.execute(
                "SELECT count(*) FROM _clean"
            ).fetchone()[0]

            clean_cols = _column_types(self._con, "_clean")
            visible = [(n, t) for n, t in clean_cols if n != "_stage_seq"]

            if self.dedup_active:
                self.stats["duplicates_removed"] = (
                    self._rows_read - self.stats["rows_written"])
                # snapshot the RESOLVED partition set against the published
                # schema (visible only — never _stage_seq)
                self.stats["dedup_columns"] = _resolve_dedup_columns(
                    visible, self.parsed)

            # sort: validated against the real result set; the clause is
            # applied outermost in every write query by _write_parts
            order_by = _order_by_clause(self.sort_spec, visible)

            # write-time projection: everything except _stage_seq
            select_list = ", ".join(_quote_ident(n) for n, _t in visible)
            from_sql = f"SELECT {select_list} FROM _clean"

            parts = _write_parts(self._con, self.settings, from_sql,
                                 order_by, self.tmp_dir, self.table)

            # ---- publish: clear stale parts, then move (same filesystem) ----
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            for old in self.dest_dir.glob("*.parquet"):
                old.unlink()
            for part in parts:
                shutil.move(str(part), str(self.dest_dir / part.name))

            self.stats["published"] = True
            self.stats["output_dir"] = str(self.dest_dir)
            self.stats["parts"] = len(parts)

            total_mb = sum(p.stat().st_size
                           for p in self.dest_dir.glob("*.parquet")) / 1024**2
            log.info("%s.%s published: %d rows read, %d rows written, "
                     "%d part(s)%s, %.1f MB",
                     self.source, self.table, self._rows_read,
                     self.stats["rows_written"], len(parts),
                     ", sorted" if self.sort_active else "", total_mb)
            return _finish(self.stats, self._start_wall)

        finally:
            # honour keep_scratch_on_success only on the success path;
            # exceptions here mean failure -> scratch always swept
            self._release(keep=self._keep_scratch)

    # ---------------------------------------------------------- context manager
    def __enter__(self) -> "ParquetWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Abandoned stream (generator died mid-way, caller skipped close()):
        mark closed, tear down session and scratch. Never swallows."""
        if not self._closed:
            self._closed = True
            self._release(keep=False)
        return False

    # -------------------------------------------------------------- internals
    def _release(self, keep: bool) -> None:
        """Close the DuckDB session; remove scratch unless told to keep it."""
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None
        if not keep and self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

def _finish(stats: dict, start_wall: float) -> dict:
    stats["end_time"] = _utc_now_iso()
    stats["duration_sec"] = round(time.time() - start_wall, 2)
    return stats

def write_table(cfg: dict, settings: dict, source: str, table: str,
                load_date: date, batches: Iterable[pl.DataFrame],
                dedup_columns=None,
                sort_spec: list[tuple[str, str]] | None = None) -> dict:
    """One-shot facade over ParquetWriter: stage, cleanse, publish. -> stats.

    Thin glue ONLY — all logic lives in ParquetWriter. `batches` is drained
    fully; pass a fresh generator per call (a retry is simply a new call).
    """
    with ParquetWriter(
        settings, source=source, table=table, load_date=load_date,
        transforms=cfg.get("transforms", {}),
        dedup_columns=dedup_columns,
        sort_spec=sort_spec,
    ) as w:
        for df in batches:
            w.append(df)
        return w.close()

# =================================================================== smoke tests
# Module-level: runnable via  python -m lib.write_parquet
def _smoke_settings(tmp: Path) -> dict:
    return {
        "paths": {
            "landing_root": str(tmp / "landing"),
            "scratch_root": str(tmp / "scratch"),
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

def test_basic_cleanse_unicode(tmp: Path):
    """Core: strings transformed, numerics untouched, Unicode preserved.

    Greek, Cyrillic, Japanese alongside English to verify upper/whitespace
    transformations handle multi-byte characters correctly. Also asserts
    include-mode dedup with an uppercase column name.
    """
    cfg = {"transforms": {"uppercase": True, "collapse_whitespace": True}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [
        pl.DataFrame({
            "code": [" ab ", "AB" ],
            "desc_en": ["x  y\tz", "abc"],
            "desc_el": ["α β γ", "δ  ε  ζ"],     # Greek
            "desc_ru": ["привет", "мир\tмир"],    # Cyrillic (tab inside)
            "desc_ja": ["あ い", "う え"],        # Japanese (double space)
            "amount": [1, 2],                    # numeric: untouched
        }),
        pl.DataFrame({
            "code": ["   aB   "],                     # near-dupe on `code` after upper
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
        list(df["desc_ja"].to_list()) == ["あ い"]      # ja has no case
    assert list(df["amount"].to_list()) == [1]             # numeric untouched

    # _stage_seq never reaches the published parquet
    assert "_stage_seq" not in df.columns, df.columns
    print("[PASS] basic cleanse + Unicode + include-mode dedup (uppercase name)")

def test_no_transforms(tmp: Path):
    """With empty transforms, data lands exactly as sent."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"code": [" AbC "], "val": [1]})]

    stats = write_table(cfg, settings, "oracle", "no_xform", load_date, iter(batches))
    assert stats["published"] and stats["dedup"] is False
    assert "dedup_columns" not in stats and "duplicates_removed" not in stats, stats

    df = _read_back(str(_glob_table("no_xform", settings, load_date) / "*.parquet"))
    assert df["code"][0] == " AbC ", df["code"][0]   # unchanged
    assert "_stage_seq" not in df.columns, df.columns
    print("[PASS] no transforms: data unchanged, no dedup fields, no _stage_seq")

def test_dedup_include_first_wins(tmp: Path):
    """Include mode keeps the EARLIEST ARRIVAL per key (deterministic)."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"key": ["a", "a"], "payload": ["first", "second"]})]

    stats = write_table(cfg, settings, "oracle", "dedup_inc", load_date,
                        iter(batches), dedup_columns=["KEY"])

    assert stats["rows_read"] == 2 and stats["rows_written"] == 1, stats
    assert stats["duplicates_removed"] == 1, stats
    assert stats["dedup_mode"] == "include", stats

    df = _read_back(str(_glob_table("dedup_inc", settings, load_date) / "*.parquet"))
    assert df["payload"][0] == "first", "earliest arrival should survive"
    print("[PASS] include-mode dedup: earliest arrival wins (deterministic)")

def test_dedup_exclude_mode(tmp: Path):
    """Exclude mode: dedup on all columns EXCEPT the listed ones.

    Rows differing ONLY in the excluded column must collapse.
    """
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({
        "key": ["a", "a", "b"],          # partition columns after exclusion
        "value": [10, 10, 20],
        "updated_at": ["t1", "t2", "t3"]  # excluded: differences here ignored
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
    print("[PASS] exclude-mode dedup: complement computed, earliest wins")

def test_dedup_all_mode(tmp: Path):
    """All mode: dedup on every column of the result set (no _stage_seq)."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print("[PASS] all-mode dedup (scalar keyword): every visible column is the key")

def test_dedup_none_keyword(tmp: Path):
    """Scalar 'none' behaves like omitting dedup_columns entirely."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": ["x", "x"]})]

    stats = write_table(cfg, settings, "oracle", "dedup_none", load_date,
                        iter(batches), dedup_columns="NONE")

    assert stats["dedup"] is False, stats
    assert stats["rows_written"] == 2, stats     # duplicate kept
    assert "dedup_columns" not in stats and "duplicates_removed" not in stats
    print("[PASS] scalar 'none' keyword: no dedup")

def test_missing_dedup_col_raises(tmp: Path):
    """Unknown include column raises ValueError; nothing published.

    Also verifies a typo'd EXCLUSION raises: both are equally dangerous.
    """
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print("[PASS] missing dedup columns (include AND exclude) raise loudly")

def test_exclude_everything_raises(tmp: Path):
    """Excluding all columns leaves no dedup key -> loud failure."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": [1]})]

    try:
        write_table(cfg, settings, "oracle", "exc_all", load_date,
                    iter(batches), dedup_columns=["-a"])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "no dedup key left" in str(e), f"error message: {e}"
    print("[PASS] excluding every column raises")

def test_sort_desc(tmp: Path):
    """Single descending key: output non-increasing; stats record the spec."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"ts": [3, 1, 2], "id": [10, 20, 30]})]

    stats = write_table(cfg, settings, "oracle", "sort_desc", load_date,
                        iter(batches), sort_spec=[("ts", "desc")])

    assert stats["published"] and stats["sort_columns"] == ["ts"], stats
    df = _read_back(str(_glob_table("sort_desc", settings, load_date) / "*.parquet"))
    assert df["ts"].to_list() == [3, 2, 1], df["ts"].to_list()
    assert "_stage_seq" not in df.columns, df.columns
    print("[PASS] sort: single descending key ordered correctly")

def test_sort_mixed_directions(tmp: Path):
    """grp ASC then val DESC: primary first, secondary breaks ties reversed."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print("[PASS] sort: mixed directions (asc primary, desc secondary)")

def test_sort_tiebreak_is_arrival_order(tmp: Path):
    """Equal sort keys keep arrival order across batches (stable sort)."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [
        pl.DataFrame({"key": ["x", "x"], "payload": ["p1", "p2"]}),
        pl.DataFrame({"key": ["x"], "payload": ["later"]}),
    ]

    write_table(cfg, settings, "oracle", "sort_tie", load_date,
                iter(batches), sort_spec=[("key", "asc")])

    df = _read_back(str(_glob_table("sort_tie", settings, load_date) / "*.parquet"))
    assert df["payload"].to_list() == ["p1", "p2", "later"], df["payload"].to_list()
    print("[PASS] sort: ties broken by arrival order (stable)")

def test_sort_typo_raises(tmp: Path):
    """Unknown sort column raises ValueError; nothing published."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    batches = [pl.DataFrame({"a": [1]})]

    try:
        write_table(cfg, settings, "oracle", "bad_sort", load_date,
                    iter(batches), sort_spec=[("nope", "asc")])
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e), f"error message: {e}"

    assert not (_glob_table("bad_sort", settings, load_date)).exists()
    print("[PASS] missing sort column raises loudly, nothing published")

def test_multi_part_sorted_seam(tmp: Path):
    """Forced split + sort: concatenated parts read back globally sorted."""
    import random
    import string

    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print(f"[PASS] sorted split: {len(names)} parts, globally ordered across seams")

def test_zero_rows(tmp: Path):
    """Empty input -> publish nothing, no folder."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
    load_date = date(2026, 10, 17)

    stats = write_table(cfg, settings, "oracle", "empty_tbl", load_date, iter([]))

    assert not stats["published"] and stats["rows_read"] == 0
    assert not _glob_table("empty_tbl", settings, load_date).exists()
    print("[PASS] zero rows: nothing published")

def test_midstream_failure(tmp: Path):
    """Generator raises mid-stream: nothing published, scratch cleaned.

    Exercises __exit__: the facade's `with` propagates the generator's
    error, the abandoned writer tears down its DuckDB session and tmp dir.
    """
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print("[PASS] midstream failure: nothing published, scratch cleaned")

def test_rerun_clears_stale_parts(tmp: Path):
    """Stale parts from a prior larger --force run are cleared on rerun."""
    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print("[PASS] rerun clears stale parts")

def test_forced_split(tmp: Path):
    """Force splitting at 1 MB; verify naming pattern and full row recovery."""
    import random
    import string

    cfg = {"transforms": {}}
    settings = _smoke_settings(tmp)
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
    print(f"[PASS] split: {len(names)} parts, all {rows:,} rows recovered, no _stage_seq")

if __name__ == "__main__":
    import tempfile

    setup_logging()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_basic_cleanse_unicode(tmp)
        test_no_transforms(tmp)
        test_dedup_include_first_wins(tmp)
        test_dedup_exclude_mode(tmp)
        test_dedup_all_mode(tmp)
        test_dedup_none_keyword(tmp)
        test_missing_dedup_col_raises(tmp)
        test_exclude_everything_raises(tmp)
        test_sort_desc(tmp)
        test_sort_mixed_directions(tmp)
        test_sort_tiebreak_is_arrival_order(tmp)
        test_sort_typo_raises(tmp)
        test_multi_part_sorted_seam(tmp)
        test_zero_rows(tmp)
        test_midstream_failure(tmp)
        test_rerun_clears_stale_parts(tmp)
        test_forced_split(tmp)

    print("\nALL WRITER SMOKE TESTS PASSED")
