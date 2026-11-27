# tests/unit/test_stg_keys.py
"""Unit tests for the PT-048 staging key bin (dpl.lib.stg_keys).

100% module coverage per the house rule (N-02) together with the edge
file. The bin is exercised against a scratch tree with a freshly minted
master key per test - the REAL ~/.dpl and secrets/ tree are never
touched (R-03/R-07 isolation). Tests observe internals ONLY through
the peek_/poke_ test seam (strict-pyright-clean).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from dpl.lib import stg_keys
from dpl.lib.stg_keys import (
    SEP,
    StgKeyNotFoundError,
    StgKeysCorruptError,
    StgKeysMasterMissingError,
    StgKeysNotInitializedError,
)

LD = date(2026, 11, 26)  # canonical load_date for these tests
CONN = "ora_cust"
BIN_NAME = "stg_keys-development.bin"


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """Scratch project tree: config/<env>/ + selector + secrets/."""
    sel = tmp_path / "config" / "environment.yaml"
    sel.parent.mkdir(parents=True)
    sel.write_text("environment: development\n", encoding="utf-8")
    (tmp_path / "config" / "development").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    return tmp_path


@pytest.fixture()
def master(tmp_path: Path) -> Path:
    key_file = tmp_path / "master.key"
    key_file.write_text(Fernet.generate_key().decode("ascii"), encoding="utf-8")
    return key_file


@pytest.fixture()
def bound(tree: Path, master: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init() bound to the scratch tree; fresh state per test."""
    monkeypatch.setenv("DPL_HOME", str(tree))
    stg_keys.poke_reset()  # test seam: fresh state
    stg_keys.init(project_root=tree, master_path=master)


# --------------------------------------------------------------------- naming
def test_key_name_uses_frozen_separator(bound: None) -> None:
    """The external label is a PURE LABEL: prefix + SEP + connection.

    B1 PIN TEST of the separator ladder (plan section 6): SEP='.' is
    preferred (probed live on DuckDB 2.0.0 - dotted names accepted)
    and frozen here. If a build ever rejects dotted PRAGMA names,
    change SEP in stg_keys.py to '-' (rung 2) or '' (rung 3) AND update
    this assertion - never at runtime. The ladder is walked exactly
    once; thereafter we do not look back (user ruling 2026-11-26).
    """
    assert SEP == "."  # rung 1 of the ladder
    assert stg_keys.key_name(CONN) == f"stg.{CONN}"
    assert SEP in ("-", "", ".")  # ladder vocabulary only


def test_internal_entry_naming_roundtrip(bound: None) -> None:
    """Seam parity: peek_entries exposes <conn>@<yyyymmdd> internals."""
    stg_keys.provision([CONN], LD)
    entries = stg_keys.peek_entries()
    assert f"{CONN}@20261126" in entries  # internal name carries the date


def test_parse_entry_rejects_garbage(bound: None) -> None:
    """Malformed internal names must parse to None - never a guess."""
    assert stg_keys.peek_parse_entry("no-at-sign") is None
    assert stg_keys.peek_parse_entry("conn@20261399") is None  # bad month/day
    assert stg_keys.peek_parse_entry("conn@notadate") is None


# ------------------------------------------------------------------ init refusals
def test_init_refuses_missing_master(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPL_HOME", str(tree))
    with pytest.raises(StgKeysMasterMissingError, match="setup_secrets"):
        stg_keys.init(project_root=tree, master_path=tree / "absent.key")


def test_not_initialized_loud(bound: None, monkeypatch: pytest.MonkeyPatch) -> None:
    stg_keys.poke_reset()  # seam: unbind without touching privates
    with pytest.raises(StgKeysNotInitializedError, match="init"):
        stg_keys.get(CONN, LD)


def test_bin_paths_are_per_environment(bound: None, tree: Path) -> None:
    assert stg_keys.peek_bin_path() == tree / "secrets" / BIN_NAME
    assert stg_keys.peek_backups_dir() == tree / "secrets" / "backups" / "stg_keys"


# ------------------------------------------------------------ provision + get
def test_provision_creates_then_seals(bound: None, tree: Path) -> None:
    created = stg_keys.provision([CONN], LD)
    assert created == [f"stg.{CONN}"]
    blob = (tree / "secrets" / BIN_NAME).read_bytes()
    assert blob.startswith(b"#")  # minimal one-line header
    token = next(
        ln for ln in blob.decode().splitlines() if ln.strip() and not ln.startswith("#")
    )
    fernet = Fernet((tree / "master.key").read_text())
    entries = json.loads(fernet.decrypt(token.encode()))["entries"]
    entry = entries[f"{CONN}@20261126"]
    assert len(entry) == 32  # 32-char AES-256 material
    assert entry.isprintable()  # SQL-safe, no hostile chars


def test_get_returns_provisioned_material(bound: None) -> None:
    stg_keys.provision([CONN], LD)
    material = stg_keys.get(CONN, LD)
    assert material == stg_keys.peek_entries()[f"{CONN}@20261126"]


def test_get_unknown_is_loud_never_regenerates(bound: None) -> None:
    with pytest.raises(StgKeyNotFoundError, match="provision"):
        stg_keys.get("never_provisioned", LD)
    # no bin file was minted by a failed get
    assert stg_keys.peek_bin_path().exists() is False


def test_provision_reuses_existing_entries(bound: None) -> None:
    """R-05 reuse-never-regenerate: second provision returns SAME material."""
    stg_keys.provision([CONN], LD)
    first = stg_keys.get(CONN, LD)
    created = stg_keys.provision([CONN], LD)
    assert created == []  # nothing new minted
    assert stg_keys.get(CONN, LD) == first


def test_provision_scoped_to_requested_connections(bound: None) -> None:
    stg_keys.provision(["conn_a", "conn_b"], LD)
    assert stg_keys.key_count() == 2
    with pytest.raises(StgKeyNotFoundError):
        stg_keys.get("conn_c", LD)


# ---------------------------------------------------------------------- sweep
def test_sweep_pairs_folder_removal_with_key_death(bound: None, tree: Path) -> None:
    """R-15: retention-pruned folder -> key dies the same pass."""
    stg_keys.provision([CONN], LD)
    stg_keys.sweep(tree / "nonexistent_landing")
    assert stg_keys.key_count() == 0
    assert not stg_keys.peek_bin_path().exists()  # retired: empty bin removed


def test_sweep_keeps_keys_whose_folders_exist(bound: None, tree: Path) -> None:
    landing = tree / "landing"
    stg_keys.provision([CONN], LD)
    (landing / "2026-11-26" / CONN).mkdir(parents=True)
    swept = stg_keys.sweep(landing)
    assert swept == []
    assert stg_keys.key_count() == 1


def test_sweep_covers_manual_midwindow_deletion(bound: None, tree: Path) -> None:
    """R-16: rm -rf of one of two days - only that day's key dies."""
    other = date(2026, 11, 20)
    landing = tree / "landing"
    stg_keys.provision([CONN], LD)
    stg_keys.provision([CONN], other)
    (landing / "2026-11-20" / CONN).mkdir(parents=True)  # only older day exists
    swept = stg_keys.sweep(landing)
    assert swept == [f"stg.{CONN}"]
    assert stg_keys.key_count() == 1  # the older day's key survives
    assert stg_keys.get(CONN, other)  # its folder exists on disk
    with pytest.raises(StgKeyNotFoundError):
        stg_keys.get(CONN, LD)  # folder-absent day is loud


def test_sweep_refuses_malformed_entry_loudly(
    bound: None, master: Path, tree: Path
) -> None:
    """A corrupt name in the envelope must NEVER be silently guessed away."""
    stg_keys.provision([CONN], LD)
    bin_path = stg_keys.peek_bin_path()
    # forge a bin holding a malformed entry (sealed with the real master,
    # so only the entry NAME is corrupt - exactly the case under test)
    forged = stg_keys.forge_seal({"garbage-entry": "ab" * 32})
    bin_path.write_bytes(forged)
    stg_keys.poke_reset()  # seam: force reload from the forged bin
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError, match="malformed"):
        stg_keys.sweep(tree / "landing")


# ------------------------------------------------------------------ corrupt bin
def test_corrupt_bin_is_loud(bound: None, master: Path, tree: Path) -> None:
    stg_keys.provision([CONN], LD)
    bin_path = stg_keys.peek_bin_path()
    # flip one payload byte without touching the header line
    blob = bin_path.read_bytes()
    bin_path.write_bytes(blob[:-2] + b"\x00\x00")
    stg_keys.poke_reset()  # seam: fresh load of the damaged bin
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError):
        stg_keys.get(CONN, LD)


def test_wrong_master_key_is_loud(bound: None, tree: Path) -> None:
    """Lazy decryption: a wrong master passes init, fails at first access."""
    stg_keys.provision([CONN], LD)
    other = tree / "other.key"
    other.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=other)  # binds fine...
    with pytest.raises(StgKeysCorruptError, match="restore"):
        stg_keys.get(CONN, LD)  # ...loud at first use


# ------------------------------------------------------------------ snapshots
def test_snapshot_day_granular(bound: None, tree: Path) -> None:
    stg_keys.provision([CONN], LD)
    first = stg_keys.snapshot()
    assert first is not None and first.is_file()
    stg_keys.snapshot()  # same-day snapshot is a no-op (one per calendar day)
    snaps = sorted((tree / "secrets" / "backups" / "stg_keys").glob("*.bin"))
    assert len(snaps) == 1


def test_snapshot_restores_yesterday(bound: None, tree: Path) -> None:
    """R-10: a snapshot decrypts to the entries it was taken from."""
    stg_keys.provision([CONN], LD)
    snap = stg_keys.snapshot()
    assert snap is not None
    fernet = Fernet((tree / "master.key").read_text())
    blob = snap.read_bytes()
    token = next(
        ln for ln in blob.decode().splitlines() if ln.strip() and not ln.startswith("#")
    )
    entries = json.loads(fernet.decrypt(token.encode()))["entries"]
    assert entries[f"{CONN}@20261126"] == stg_keys.get(CONN, LD)


def test_snapshot_prunes_to_five(bound: None, tree: Path) -> None:
    """R-10: oldest snapshots die; newest SNAPSHOT_KEEP survive."""
    stg_keys.provision([CONN], LD)
    backups = tree / "secrets" / "backups" / "stg_keys"
    backups.mkdir(parents=True, exist_ok=True)
    for d in range(1, 7):  # six older snapshots; glob order is lexical
        fake = backups / f"stg_keys-development.2026112{6 - d}.bin"
        fake.write_bytes(b"placeholder")
    kept = stg_keys.snapshot()
    snaps = sorted(backups.glob("stg_keys-development.*.bin"))
    assert len(snaps) == stg_keys.SNAPSHOT_KEEP
    assert kept in snaps


def test_snapshot_noop_when_empty(bound: None) -> None:
    assert stg_keys.snapshot() is None


# ----------------------------------------------------------- vault isolation
def test_bin_never_touches_canonical_vault(bound: None, tree: Path) -> None:
    """R-07/R-03: bin churn leaves the password vault untouched."""
    vault = tree / "secrets" / "secrets.fernet.bin"
    vault.write_bytes(b"the canonical vault contents")
    stg_keys.provision([CONN], LD)
    stg_keys.sweep(tree / "landing_empty")  # rewrites the BIN
    stg_keys.snapshot()
    assert vault.read_bytes() == b"the canonical vault contents"
