# tests/unit/test_stg_keys_edge.py
"""Edge-path tests for stg_keys: the lines the main file's happy paths
never reach - default-argument resolution branches, every
envelope-corruption refusal, and the defensive double-check in sweep.
Internals observed only via the peek_/poke_ test seam."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from dpl.lib import stg_keys
from dpl.lib.stg_keys import StgKeysCorruptError

LD = date(2026, 11, 26)
CONN = "ora_cust"
BIN_NAME = "stg_keys-development.bin"


def _mint(tree: Path) -> Path:
    """Mint master + bind the bin to the scratch tree."""
    master = tree / "master.key"
    master.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.init(project_root=tree, master_path=master)
    return master


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "proj"
    (tree / "config" / "development").mkdir(parents=True)
    (tree / "secrets").mkdir(parents=True)
    (tree / "config" / "environment.yaml").write_text(
        "environment: development\n", encoding="utf-8"
    )
    return tree


# ---------------------------------------------------- default-argument branches
def test_init_with_default_master_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init(master_path=None) resolves under ~/.dpl/keys/<env>/stg_keys/."""
    tree = _tree(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    default_master = home / ".dpl" / "keys" / "development" / "stg_keys" / "key"
    default_master.parent.mkdir(parents=True)
    default_master.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=None)
    assert stg_keys.peek_master_path() == default_master
    assert stg_keys.peek_env_name() == "development"


def test_init_via_dpl_home_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init(project_root=None) resolves via get_project_root() (DPL_HOME)."""
    tree = _tree(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("DPL_HOME", str(tree))
    default_master = home / ".dpl" / "keys" / "development" / "stg_keys" / "key"
    default_master.parent.mkdir(parents=True)
    default_master.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    stg_keys.poke_reset()
    stg_keys.init()  # both defaults exercised
    assert stg_keys.peek_bin_path() == tree / "secrets" / BIN_NAME


# ------------------------------------------------------ envelope refusals
def test_two_token_lines_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    master = _mint(tree)
    stg_keys.provision([CONN], LD)
    fernet = Fernet(master.read_text())
    good = tree / "secrets" / BIN_NAME
    forged = (
        b"# header\n" + fernet.encrypt(b"{}") + b"\n" + fernet.encrypt(b"{}") + b"\n"
    )
    good.write_bytes(forged)
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError, match="exactly one token line"):
        stg_keys.get(CONN, LD)


def test_envelope_version_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    master = _mint(tree)
    stg_keys.provision([CONN], LD)
    fernet = Fernet(master.read_text())
    good = tree / "secrets" / BIN_NAME
    payload = json.dumps({"version": 99, "entries": {"x@20261126": "y"}}).encode()
    good.write_bytes(b"# header\n" + fernet.encrypt(payload) + b"\n")
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError, match="version 99"):
        stg_keys.get(CONN, LD)


def test_envelope_bad_json_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    master = _mint(tree)
    stg_keys.provision([CONN], LD)
    fernet = Fernet(master.read_text())
    good = tree / "secrets" / BIN_NAME
    good.write_bytes(b"# header\n" + fernet.encrypt(b"not-json{") + b"\n")
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError, match="not valid JSON"):
        stg_keys.get(CONN, LD)


def test_envelope_no_entries_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    master = _mint(tree)
    stg_keys.provision([CONN], LD)
    fernet = Fernet(master.read_text())
    good = tree / "secrets" / BIN_NAME
    payload = json.dumps({"version": 1, "entries": {}}).encode()
    good.write_bytes(b"# header\n" + fernet.encrypt(payload) + b"\n")
    stg_keys.poke_reset()
    stg_keys.init(project_root=tree, master_path=master)
    with pytest.raises(StgKeysCorruptError, match="no 'entries'"):
        stg_keys.get(CONN, LD)


# ------------------------------------------------------ seal/write defenses
def test_seal_roundtrip_failure_never_destroys_bin(tmp_path: Path) -> None:
    """If the candidate seal reopens to DIFFERENT entries, the old bin
    survives untouched and no candidate is left behind.

    Surgical sabotage: patch Fernet.encrypt so the CANDIDATE token
    decrypts to altered entries - the exact production condition the
    round-trip guard exists for.
    """
    tree = _tree(tmp_path)
    _mint(tree)
    stg_keys.provision([CONN], LD)
    good = tree / "secrets" / BIN_NAME
    before = good.read_bytes()

    real_encrypt = Fernet.encrypt

    def tampered(self: Fernet, data: bytes) -> bytes:
        obj = json.loads(data)
        obj["entries"]["injected@20260101"] = "z" * 32
        return real_encrypt(self, json.dumps(obj).encode("utf-8"))

    Fernet.encrypt = tampered  # type: ignore[method-assign]
    try:
        with pytest.raises(StgKeysCorruptError, match="round-trip"):
            stg_keys.provision(["other_conn"], LD)
    finally:
        Fernet.encrypt = real_encrypt  # type: ignore[method-assign]
    assert good.read_bytes() == before  # untouched
    assert not good.with_suffix(".bin.tmp").exists()  # no leftover candidate


def test_sweep_malformed_in_second_pass_defensive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second parse in sweep (defensive) raises loudly if it sees a
    malformed name the first pass already validated."""
    tree = _tree(tmp_path)
    _mint(tree)
    stg_keys.provision([CONN], LD)
    (tree / "landing" / "2026-11-26" / CONN).mkdir(parents=True)

    calls = {"n": 0}

    def flaky(entry: str) -> tuple[str, date] | None:
        calls["n"] += 1
        if calls["n"] > 1:
            return None  # second pass: pretend corruption
        return (CONN, LD)

    monkeypatch.setattr(stg_keys, "_parse_entry", flaky)
    shutil.rmtree(tree / "landing" / "2026-11-26")
    with pytest.raises(StgKeysCorruptError, match="malformed"):
        stg_keys.sweep(tree / "landing")


# ------------------------------------------------------ misc
def test_key_count_lazy_load(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    _mint(tree)
    assert stg_keys.key_count() == 0  # absent bin = lazy empty
    assert stg_keys.peek_entries() == {}


def test_sweep_short_circuits_on_loaded_empty(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    _mint(tree)
    stg_keys.provision([CONN], LD)
    stg_keys.poke_entries({})  # in-memory empty, loaded
    assert stg_keys.sweep(tree / "landing") == []  # short-circuit: falsy entries


def test_get_unknown_after_provision_raises_entry_missing(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    _mint(tree)
    stg_keys.provision([CONN], LD)
    with pytest.raises(stg_keys.StgKeyNotFoundError, match="provision"):
        stg_keys.get("some_other", LD)


def test_snapshot_noop_before_first_load(tmp_path: Path) -> None:
    """snapshot() before any load/provision is a safe no-op."""
    tree = _tree(tmp_path)
    _mint(tree)
    assert stg_keys.snapshot() is None


def test_peek_entries_lazy_loads_fresh_bin(tmp_path: Path) -> None:
    """Seam: peek_entries() lazy-loads a not-yet-decrypted (None) bin."""
    tree = _tree(tmp_path)
    _mint(tree)  # init() leaves entries = None; bin not yet on disk
    assert stg_keys.peek_entries() == {}
