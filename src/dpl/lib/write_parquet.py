# lib/write_parquet.py
"""Parquet writer: DuckDB staging, cleansing, sorting, atomic publish.

Pipeline per table:
    1. Stage Polars batches into DuckDB table `_stage` (spills to scratch
       beyond duckdb.memory_limit). Each batch is tagged with `_stage_seq`,
       a monotonically increasing arrival-order integer.
    2. Cleanse: uppercase + whitespace collapse on VARCHAR columns only;
       optional per-table dedup via QUALIFY row_number() ordered by
       _stage_seq (earliest arrival wins - deterministic)
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
    [(column, 'asc'|'desc'), ...] - position = precedence. Ties are broken
    by arrival order (_stage_seq): sorts are stable.

Guarantees:
    - Nothing appears in landing unless all parts completed
    - Tables with zero rows publish nothing (stats.published = False)
    - dedup/sort column names are matched case-insensitively against the
      result set; unknown names fail loudly in every mode
    - `_stage_seq` never appears in published parquet
    - Multi-part tables read back globally sorted when sort is active

Logging: lib/logger.py - "dpl.writer" under the central dpl tree.
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
    column (directly listed or excluded) exists in the result set -
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
            "dedup_columns excludes every column of the result set - "
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

    Returns '' when there is no sort (no ORDER BY at all - untouched
    tables keep their unsorted behavior). With a sort, the user keys are
    rendered in spec order (ASC/DESC from the spec) followed by an
    implicit '"_stage_seq" ASC' tiebreak: equal keys keep arrival order,
    making the sort stable and part contents reproducible.

    `columns` is the result set of the SELECT being ordered - used to
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
         ORDER BY _stage_seq - earliest arrival wins, deterministically.

    _stage_seq is KEPT in the output of this SQL (the caller's _clean
    table carries it) - it is needed as the sort tiebreak. It is dropped
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
    data query - the single-part COPY and each LIMIT/OFFSET slice. An
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

    # count on the unordered base - cheaper, ordering is irrelevant here
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

        # Fail fast on malformed dedup specs - BEFORE any connection or I/O
        if isinstance(dedup_columns, dict) and "mode" in dedup_columns:
            self.parsed = dedup_columns
        else:
            self.parsed = parse_dedup_spec(dedup_columns)
        self.dedup_active = self.parsed["mode"] != "none"

        # Parsed sort spec from table_rules: [(column, 'asc'|'desc'), ...].
        # Structure already validated by the loader; column EXISTENCE is
        # checked here against the actual result set (this layer alone
        # sees it - same rule as dedup columns).
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
                # schema (visible only - never _stage_seq)
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

    Thin glue ONLY - all logic lives in ParquetWriter. `batches` is drained
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
