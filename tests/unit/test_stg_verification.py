# tests/unit/test_stg_verification.py
"""PT-048 Batch 2 tests: encrypted writes + mandatory verification.

Real DuckDB encryption round-trips against a scratch key bin - no
mocking of the crypto path. The writer is wired (imports write_parquet,
a non-strict legacy module), so per the strict-zone boundary rule this
test file is NOT added to the pyright strict list yet; it joins when
write_parquet.py does.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest
from cryptography.fernet import Fernet

from dpl.lib import stg_keys
from dpl.lib import write_parquet as wp

LD = date(2026, 11, 26)
SOURCE = "ora_test"
TABLE = "cust_det"


def _settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "scratch_root": str(tmp_path / "scratch"),
            "landing_root": str(tmp_path / "landing"),
        },
        "writer": {"compression": "zstd", "max_file_mb": 512},
    }


def _bind_bin(tmp_path: Path) -> None:
    """Scratch env tree + master key + init + provision SOURCE/LD."""
    root = tmp_path / "envtree"
    (root / "config" / "development").mkdir(parents=True)
    (root / "secrets").mkdir(parents=True)
    (root / "config" / "environment.yaml").write_text(
        "environment: development\n", encoding="utf-8"
    )
    master = root / "master.key"
    master.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.poke_reset()
    stg_keys.init(project_root=root, master_path=master)
    stg_keys.provision([SOURCE], LD)


def _frame(n: int = 5) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "acct_no": [f"ACC{i:05d}" for i in range(n)],
            "balance": [1000.0 + i for i in range(n)],
        }
    )


def _batches(df: pl.DataFrame) -> list[pl.DataFrame]:
    return [df]


def _dest(settings: dict[str, Any]) -> Path:
    return Path(settings["paths"]["landing_root"]) / "2026-11-26" / SOURCE / TABLE


# ------------------------------------------------------------------- happy path
def test_encrypted_write_publishes_verified(tmp_path: Path) -> None:
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    stats = wp.write_table(
        {"transforms": {}},
        settings,
        source=SOURCE,
        table=TABLE,
        load_date=LD,
        batches=_batches(_frame()),
    )
    assert stats["verification_passed"] is True
    assert stats["published"] is True
    assert stats["output_dir"] == str(_dest(settings))
    assert stats["rows_written"] == 5


def test_unkeyed_read_is_refused(tmp_path: Path) -> None:
    """The published files must be REAL ciphertext - a bare session
    cannot read them (the PT-048 threat model, proven on disk)."""
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    wp.write_table(
        {"transforms": {}},
        settings,
        source=SOURCE,
        table=TABLE,
        load_date=LD,
        batches=_batches(_frame()),
    )
    part = next(_dest(settings).glob("*.parquet"))
    con = duckdb.connect()
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT count(*) FROM read_parquet('{part.as_posix()}')")


def test_keyed_read_roundtrips(tmp_path: Path) -> None:
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    wp.write_table(
        {"transforms": {}},
        settings,
        source=SOURCE,
        table=TABLE,
        load_date=LD,
        batches=_batches(_frame()),
    )
    part = next(_dest(settings).glob("*.parquet"))
    con = duckdb.connect()
    con.execute(
        f"PRAGMA add_parquet_key('{stg_keys.key_name(SOURCE)}', "
        f"'{stg_keys.get(SOURCE, LD)}')"
    )
    row = con.execute(
        f"SELECT count(*), min(acct_no) FROM read_parquet("
        f"'{part.as_posix()}', "
        f"encryption_config={{footer_key: '{stg_keys.key_name(SOURCE)}'}})"
    ).fetchone()
    assert row == (5, "ACC00000")


def test_zero_row_table_leaves_no_stamp_and_no_files(tmp_path: Path) -> None:
    """Absence semantics: verification_passed absent when nothing
    published (R-13 ruling); no key fetched, nothing on disk."""
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    stats = wp.write_table(
        {"transforms": {}},
        settings,
        source=SOURCE,
        table=TABLE,
        load_date=LD,
        batches=[],
    )
    assert stats["published"] is False
    assert "verification_passed" not in stats
    assert not _dest(settings).exists()


# --------------------------------------------------------------- failure paths
def test_missing_key_is_loud_never_plaintext(tmp_path: Path) -> None:
    """R-12: no provisioned key -> loud refusal, zero files written."""
    root = tmp_path / "envtree"
    (root / "config" / "development").mkdir(parents=True)
    (root / "secrets").mkdir(parents=True)
    (root / "config" / "environment.yaml").write_text(
        "environment: development\n", encoding="utf-8"
    )
    master = root / "master.key"
    master.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.poke_reset()
    stg_keys.init(project_root=root, master_path=master)
    # deliberately NO provision
    settings = _settings(tmp_path)
    with pytest.raises(stg_keys.StgKeyNotFoundError, match="provision"):
        wp.write_table(
            {"transforms": {}},
            settings,
            source=SOURCE,
            table=TABLE,
            load_date=LD,
            batches=_batches(_frame()),
        )
    assert not _dest(settings).exists()  # nothing plaintext landed


def test_corrupt_part_purged_and_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-13: verification failure -> parts deleted, dir removed,
    StgVerificationError raised; staging holds verified files or
    nothing."""
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    real = wp._write_parts

    def sabotaged(
        con: Any,
        s: dict,
        from_sql: str,
        order_by: str,
        scratch_dir: Path,
        table: str,
        key_name: str,
    ) -> list[Path]:
        parts = real(con, s, from_sql, order_by, scratch_dir, table, key_name)
        blob = bytearray(parts[0].read_bytes())
        blob[-3] ^= 0xFF  # corrupt the tail (footer) region
        parts[0].write_bytes(bytes(blob))
        return parts

    monkeypatch.setattr(wp, "_write_parts", sabotaged)
    with pytest.raises(wp.StgVerificationError, match="purged"):
        wp.write_table(
            {"transforms": {}},
            settings,
            source=SOURCE,
            table=TABLE,
            load_date=LD,
            batches=_batches(_frame()),
        )
    assert not list(_dest(settings).glob("*.parquet"))  # purged
    assert not _dest(settings).exists()  # no husk dir


def test_compression_level_fallback_stays_encrypted() -> None:
    """THE pinned fallback test (unit level): when a DuckDB build
    rejects COMPRESSION_LEVEL, _copy's fallback branch reissues the
    COPY with the FULL option set - ENCRYPTION_CONFIG present in BOTH
    the rejected attempt and the fallback reissue. Never plaintext.

    Recording fake con: no DuckDB internals are wrapped (execute
    wrappers break the Polars replacement scan - lesson of this
    test's first version)."""

    class FakeCon:
        def __init__(self) -> None:
            self.sqls: list[str] = []

        def execute(self, sql: str) -> None:
            self.sqls.append(sql)
            if "COMPRESSION_LEVEL" in sql:
                raise duckdb.Error(
                    "COMPRESSION_LEVEL not accepted by this DuckDB "
                    "(simulated old build)"
                )

    fake = FakeCon()
    wp._copy(fake, "SELECT 1", Path("/tmp/any.parquet"), "zstd", 3, "stg.someconn")

    assert len(fake.sqls) == 2  # rejected attempt, then fallback reissue
    assert "COMPRESSION_LEVEL" in fake.sqls[0]
    assert "ENCRYPTION_CONFIG" in fake.sqls[0]
    assert "COMPRESSION_LEVEL" not in fake.sqls[1]
    assert "ENCRYPTION_CONFIG" in fake.sqls[1]
    assert "footer_key: 'stg.someconn'" in fake.sqls[1]


def test_rowcount_mismatch_purges_and_raises(tmp_path: Path) -> None:
    """Line 629: a landed file whose total rows mismatch the recorded
    count triggers the purge path - verification is MORE than
    decryption. Driven directly: write successfully (verified), then
    re-verify with a deliberately wrong rows_written."""
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    w = wp.ParquetWriter(
        settings,
        source=SOURCE,
        table=TABLE,
        load_date=LD,
        transforms={},
    )
    try:
        w.append(_frame())
        stats = w.close()
        assert stats["verification_passed"] is True
        landed = sorted(_dest(settings).glob("*.parquet"))
        assert landed  # the table wrote for real
        w.stats["rows_written"] += 1  # the lie: one row more than on disk
        with pytest.raises(wp.StgVerificationError, match="purged"):
            w._verify_parts(landed)
        assert not _dest(settings).exists()  # parts + empty dir removed
    finally:
        w._release(keep=False)  # idempotent teardown, already closed


def test_purge_leaves_nonempty_dir_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines 648-649: when the landing dir holds a foreign file, the
    purge's rmdir fails and the warning branch fires - dir left in
    place, loudly."""
    _bind_bin(tmp_path)
    settings = _settings(tmp_path)
    # pre-plant a foreign file in the destination BEFORE extraction
    dest = _dest(settings)
    dest.mkdir(parents=True)
    foreign = dest / "foreign.txt"
    foreign.write_text("not a parquet", encoding="utf-8")
    real = wp._write_parts

    def sabotaged(
        con: Any,
        s: dict,
        from_sql: str,
        order_by: str,
        scratch_dir: Path,
        table: str,
        key_name: str,
    ) -> list[Path]:
        parts = real(con, s, from_sql, order_by, scratch_dir, table, key_name)
        blob = bytearray(parts[0].read_bytes())
        blob[-3] ^= 0xFF
        parts[0].write_bytes(bytes(blob))
        return parts

    monkeypatch.setattr(wp, "_write_parts", sabotaged)
    with pytest.raises(wp.StgVerificationError, match="purged"):
        wp.write_table(
            {"transforms": {}},
            settings,
            source=SOURCE,
            table=TABLE,
            load_date=LD,
            batches=_batches(_frame()),
        )
    # foreign file survived; dir still exists (not-empty branch)
    assert foreign.exists()
    assert dest.exists()
    # parts purged: only the foreign file remains
    assert list(dest.glob("*.parquet")) == []
