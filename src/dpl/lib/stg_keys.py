# lib/stg_keys.py
"""Staging parquet encryption keys - the machine-managed key bin (PT-048).

Single authority for per-day, per-connection parquet encryption keys.
Separate from pass_mgr by design (PT-048 design doc section 2): a
different store, a different lifecycle (keys churn ~18x per night),
and a different master key file. vault.sh list/scan never touch this
bin and this bin never touches the canonical vault file.

Storage:
    PROJECT_ROOT/secrets/stg_keys-<environment>.bin     (gitignored)
    ~/.dpl/keys/<env>/stg_keys/key                     (Fernet master)

Envelope (Fernet-sealed JSON, AES-128-CBC + HMAC-SHA256):
    {"version": 1,
     "entries": {"<connection>@<yyyymmdd>": "<32-char AES-256 material>"}}

Internal entries carry the date so ~10 days x N connections coexist.
The EXTERNAL key name - what DuckDB registers, what the manifest
stamps - is a pure label (design section 2.2): "stg<SEP><connection>"
where SEP is the frozen separator chosen once by the B1 pin test
(design ladder: '.' -> '-' -> ''). Nothing anywhere parses the label;
the bin translates label + load_date <-> internal entry.

API (init once per process, then plain calls - the pass_mgr pattern):
    init()                         resolve env, locate bin+master key
    key_name(conn)                 external label for a connection
    get(conn, load_date)           material; loud error if absent
    provision(conns, load_date)    create-today's-keys; reuse, never
                                   regenerate (backfill-safe)
    sweep(landing_root)            disk-truth reconciliation: kill keys
                                   whose date-folder no longer exists
    snapshot()                      day-granular backup, newest 5 kept

Every seal: candidate temp file -> decrypt round-trip on the
candidate -> backup old -> atomic os.replace (the vault_ops pattern,
re-implemented small - vault_ops is a CLI script, not importable).

Loud failures everywhere; silent degradation forbidden (E-10).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as _secrets
from datetime import UTC, date, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from dpl.lib.logger import get_run_logger

__func_msg__ = {
    "name": "stg_keys",
    "invocation": "",
    "category": "secrets",
    "purpose": (
        "Machine-managed key bin for staging parquet encryption (PT-048):\n"
        "   one 256-bit key per connection per day, Fernet-sealed, auto-provisioned\n"
        "   before writers fan out, swept by end-of-run disk truth, backed up daily"
    ),
}

log: logging.LoggerAdapter[logging.Logger] = get_run_logger("dpl.stg_keys")

#: Envelope schema version. Reserved for future rotation; nothing
#: branches on it yet (same stance as FERNET_ENVELOPE_VERSION in pass_mgr).
STG_KEYS_ENVELOPE_VERSION = 1

#: Number of daily snapshots retained (requirement R-10, user ruling).
SNAPSHOT_KEEP = 5

# ---------------------------------------------------------------------------
# Separator ladder (design doc section 6, frozen once at B1)
#
# The label is a pure string to DuckDB (PRAGMA add_parquet_key takes any
# name); the '.' form is preferred and presumed accepted. The pin test
# test_stg_keys.py::test_separator_pin freezes the chosen rung. To move
# DOWN the ladder (dots rejected by a build), change SEP to '-' or ''
# and update the pin test - never at runtime, never per-call.
SEP = "."
LABEL_PREFIX = "stg"

#: DuckDB accepts a 32-CHARACTER string as a raw AES-256 key (B1 probe:
#: hex-64 REJECTED with 'Invalid AES key'). Material = base64 of 24
#: random bytes -> exactly 32 chars, 192 bits of entropy, no SQL-hostile
#: characters. More than sufficient for keys that live 10 days.
_MATERIAL_B64_BYTES = 24


class StgKeysError(RuntimeError):
    """Base class: every bin failure is loud and names its file."""


class StgKeysNotInitializedError(StgKeysError):
    """init() was not called before use - programming error, refuse."""


class StgKeysMasterMissingError(StgKeysError):
    """The Fernet master key file is absent (run setup_secrets.sh)."""


class StgKeysCorruptError(StgKeysError):
    """The bin cannot be decrypted/parsed - restore from backups/."""


class StgKeyNotFoundError(StgKeysError):
    """No entry for conn@date - loud; never guess, never regenerate."""


# --------------------------------------------------------------------- state
#: The decrypted bin: internal entry name -> key material (hex).
_BinState = dict[str, str]


class _State:
    """Process-wide singleton; tests reset via _reset()."""

    env_name: str | None = None
    project_root: Path | None = None
    bin_path: Path | None = None
    master_path: Path | None = None
    backups_dir: Path | None = None
    fernet: Fernet | None = None
    entries: _BinState | None = None  # lazy-decrypt on first use


_state = _State()


def _reset() -> None:
    """Test seam: forget everything (module reload without re-import)."""
    global _state
    _state = _State()


# ------------------------------------------------------------------- naming
def key_name(conn: str) -> str:
    """External label: what DuckDB registers and the manifest stamps."""
    return f"{LABEL_PREFIX}{SEP}{conn}"


def _entry_name(conn: str, load_date: date) -> str:
    """Internal entry name: connection@yyyymmdd (date lives only here)."""
    return f"{conn}@{load_date.strftime('%Y%m%d')}"


def _parse_entry(entry: str) -> tuple[str, date] | None:
    """Inverse of _entry_name; None when the entry is malformed."""
    if "@" not in entry:
        return None
    conn, _, ymd = entry.rpartition("@")
    try:
        return conn, datetime.strptime(ymd, "%Y%m%d").date()
    except ValueError:
        return None


def today_str() -> str:
    """UTC today as yyyymmdd - key dates are UTC (UTC-Z doctrine)."""
    return datetime.now(UTC).strftime("%Y%m%d")


# --------------------------------------------------------------------- init
def init(project_root: Path | None = None, master_path: Path | None = None) -> None:
    """Bind the bin to this machine's active environment. Idempotent.

    Production callers pass nothing: the project root comes from
    dpl.lib.paths.get_project_root() and the environment from
    dpl.lib.environment.resolve_environment() - both single sources
    (PT-040 doctrine: resolved, never named by config).

    Tests pass explicit roots to point the bin at a scratch tree.
    An ABSENT bin is legal here (first run on a machine creates it on
    the first provision) - the master key file must exist, though.
    """
    _reset()

    if project_root is None:
        from dpl.lib.paths import get_project_root

        project_root = get_project_root()
    root = Path(project_root)

    from dpl.lib.environment import resolve_environment

    env = resolve_environment(config_dir=root / "config")

    if master_path is None:
        master_path = Path.home() / ".dpl" / "keys" / env.name / "stg_keys" / "key"
    master = Path(master_path)

    bin_path = root / "secrets" / f"stg_keys-{env.name}.bin"
    backups = root / "secrets" / "backups" / "stg_keys"

    if not master.is_file():
        raise StgKeysMasterMissingError(
            f"stg_keys master key file missing at {master} - run "
            f"./setup_secrets.sh, which creates it for the active "
            f"environment alongside the vault keys."
        )

    _state.env_name = env.name
    _state.project_root = root
    _state.bin_path = bin_path
    _state.master_path = master
    _state.backups_dir = backups
    _state.fernet = Fernet(master.read_text(encoding="utf-8").strip())
    _state.entries = None  # decrypt lazily on first use


def _require() -> tuple[Fernet, Path, Path]:
    """The bound state triple or a loud programming-error refusal."""
    if _state.fernet is None or _state.bin_path is None or _state.backups_dir is None:
        raise StgKeysNotInitializedError(
            "stg_keys.init() was not called in this process - call it "
            "once at startup (the pass_mgr pattern) before any bin use."
        )
    return _state.fernet, _state.bin_path, _state.backups_dir


# ----------------------------------------------------------------- envelope
def _seal(fernet: Fernet, entries: _BinState) -> bytes:
    """Entries -> envelope JSON -> Fernet token, with header line."""
    payload = json.dumps(
        {"version": STG_KEYS_ENVELOPE_VERSION, "entries": entries}
    ).encode("utf-8")
    header = b"# machine-managed staging keys - do not edit; restore from backups/\n"
    return header + fernet.encrypt(payload)


def _open(fernet: Fernet, blob: bytes, source: Path) -> _BinState:
    """Fernet token -> envelope JSON -> entries. Every refusal loud."""
    import json

    # skip the one-line header comment (parser tolerates comments)
    token_lines = [
        ln
        for ln in blob.decode("utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(token_lines) != 1:
        raise StgKeysCorruptError(
            f"{source} must contain exactly one token line, found "
            f"{len(token_lines)} - restore from backups/ or re-run "
            f"setup_secrets.sh"
        )
    try:
        payload = fernet.decrypt(token_lines[0].encode("ascii"))
    except InvalidToken as e:
        raise StgKeysCorruptError(
            f"fernet decryption failed for {source} - wrong master key "
            f"or corrupted bin; restore from {source.parent}/backups/"
        ) from e
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as e:
        raise StgKeysCorruptError(
            f"envelope payload of {source} is not valid JSON: {e}"
        ) from e
    if envelope.get("version") != STG_KEYS_ENVELOPE_VERSION:
        raise StgKeysCorruptError(
            f"envelope version {envelope.get('version')!r} unsupported "
            f"(expected {STG_KEYS_ENVELOPE_VERSION}): {source}"
        )
    entries = envelope.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise StgKeysCorruptError(f"envelope of {source} has no 'entries' mapping")
    return {str(k): str(v) for k, v in entries.items()}  # type: ignore[unknown]


def _load(fernet: Fernet, bin_path: Path) -> _BinState:
    """Decrypt the bin from disk; absent bin = empty (first provision)."""
    if not bin_path.is_file():
        return {}
    return _open(fernet, bin_path.read_bytes(), bin_path)


def _write(fernet: Fernet, bin_path: Path, entries: _BinState) -> None:
    """Seal-verify-then-atomic-replace (the vault_ops pattern, small)."""
    if not entries:
        # Deleting the last key retires the bin (disk-as-truth); callers
        # reach here only via sweep(), which logs the retirement.
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        if bin_path.is_file():
            bin_path.unlink()
        return
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = bin_path.with_suffix(".bin.tmp")
    candidate.write_bytes(_seal(fernet, entries))
    # round-trip the CANDIDATE before replacing - a bad seal must
    # never destroy the last good bin (E-test pinned).
    check = _open(fernet, candidate.read_bytes(), candidate)
    if check != entries:
        candidate.unlink(missing_ok=True)
        raise StgKeysCorruptError(
            f"candidate seal of {bin_path} does not round-trip - bin left untouched"
        )
    os.replace(candidate, bin_path)  # atomic on POSIX and Windows


# ------------------------------------------------------------------ public
def get(conn: str, load_date: date) -> str:
    """Material for key_name(conn) on load_date. Loud when absent.

    Never regenerates: an absent entry is an error, not an opportunity
    to mint (silent regeneration would orphan the old ciphertext -
    requirement R-05, backfill-safety).
    """
    fernet, bin_path, _ = _require()
    if _state.entries is None:
        _state.entries = _load(fernet, bin_path)
    entry = _entry_name(conn, load_date)
    material = _state.entries.get(entry)
    if material is None:
        raise StgKeyNotFoundError(
            f"no staging key for '{conn}' on "
            f"{load_date.strftime('%Y-%m-%d')} - provision() must run "
            f"before writers (run-start order); if this is a retry of a "
            f"legitimate window, restore the bin from backups/"
        )
    return material


def provision(conns: list[str], load_date: date) -> list[str]:
    """Ensure a key exists for every conn on load_date. -> created names.

    Reuse, never regenerate (R-05): existing entries are returned
    untouched - a backfill/catch-up run on an already-keyed date
    inherits the original key material. New entries are 32 random
    bytes. Single seal per call; callers run this BEFORE the writer
    fan-out so there are no concurrent bin writers by construction.
    """
    fernet, bin_path, _ = _require()
    if _state.entries is None:
        _state.entries = _load(fernet, bin_path)

    created: list[str] = []
    changed = False
    for conn in conns:
        entry = _entry_name(conn, load_date)
        if entry in _state.entries:
            continue
        material = base64.b64encode(_secrets.token_bytes(_MATERIAL_B64_BYTES)).decode(
            "ascii"
        )
        _state.entries[entry] = material
        created.append(key_name(conn))
        log.info(
            "stg_keys: created %s (load_date=%s)", key_name(conn), load_date.isoformat()
        )
        changed = True

    if changed:
        _write(fernet, bin_path, _state.entries)
    return created


def sweep(landing_root: Path) -> list[str]:
    """Disk-truth reconciliation (R-15 + R-16, one rule): for every
    bin entry, if <landing_root>/<date>/<conn>/ no longer exists on
    disk, delete the entry. Covers retention pruning (paired
    deletion) AND manual rm -rf mid-window alike; no orphan key
    survives more than one completed run. Never consults the
    configured retention number - the disk is the only truth.
    """
    fernet, bin_path, _ = _require()
    if _state.entries is None:
        _state.entries = _load(fernet, bin_path)
    if not _state.entries:
        return []

    root = Path(landing_root)
    doomed: list[str] = []
    for entry in list(_state.entries):
        parsed = _parse_entry(entry)
        if parsed is None:
            # malformed entry: keep it loud, refuse to guess
            raise StgKeysCorruptError(
                f"malformed bin entry {entry!r} - cannot reconcile "
                f"with disk; restore from backups/"
            )
        conn, dt = parsed
        if not (root / dt.isoformat() / conn).is_dir():
            doomed.append(entry)
            log.info(
                "stg_keys: swept %s (folder %s/%s/%s gone)",
                key_name(conn),
                root,
                dt.isoformat(),
                conn,
            )

    if not doomed:
        return []

    swept_names: list[str] = []
    for entry in doomed:
        del _state.entries[entry]
        parsed = _parse_entry(entry)
        if parsed is None:  # already validated above
            raise StgKeysCorruptError(
                f"malformed bin entry {entry!r} - cannot reconcile "
                f"with disk; restore from backups/"
            )
        swept_names.append(key_name(parsed[0]))

    _write(fernet, bin_path, _state.entries)
    if not _state.entries:
        log.info("stg_keys: bin retired - last key swept (disk-as-truth)")
    return swept_names


def snapshot() -> Path | None:
    """Day-granular backup: at most one per calendar day, newest 5 kept.

    Runs after the end-of-run sweep so backups capture the stable
    overnight state (design 2.4). A snapshot for TODAY already existing
    is a no-op - the ~18 nightly rewrites never churn the backup set.
    """
    fernet, _, backups_dir = _require()
    if _state.entries is None:
        # no-op on a fresh process that never loaded; caller's
        # housekeeping always provisions/loads first
        return None
    if not _state.entries:
        return None

    backups_dir.mkdir(parents=True, exist_ok=True)
    env = _state.env_name or "env"
    todays = backups_dir / f"stg_keys-{env}.{today_str()}.bin"
    if todays.is_file():
        return todays  # one per day, already taken

    payload = _seal(fernet, _state.entries)
    cand = todays.with_suffix(".bin.tmp")
    cand.write_bytes(payload)
    os.replace(cand, todays)

    # prune to newest SNAPSHOT_KEEP by name (yyyymmdd sorts
    # lexicographically - the generate_schemas backups pattern)
    snaps = sorted(backups_dir.glob(f"stg_keys-{env}.*.bin"))
    for old in snaps[:-SNAPSHOT_KEEP]:
        old.unlink()
        log.info("stg_keys: pruned snapshot %s", old.name)
    return todays


def key_count() -> int:
    """Count of live entries (diagnostics; never material)."""
    fernet, bin_path, _ = _require()
    if _state.entries is None:
        _state.entries = _load(fernet, bin_path)
    return len(_state.entries)


# ------------------------------------------------------------------ test seam
# TEST-ONLY surface: the house 100%-coverage rule needs to observe and
# manipulate internals; strict pyright forbids tests touching privates,
# so everything they need is exposed here under peek_/poke_/forge_
# names. Production code must NEVER call these (gate greps for it).


def peek_entries() -> dict[str, str]:
    """TEST-ONLY: copy of the live decrypted entries (loads lazily)."""
    fernet, bin_path, _ = _require()
    if _state.entries is None:
        _state.entries = _load(fernet, bin_path)
    assert _state.entries is not None
    return dict(_state.entries)


def poke_entries(entries: dict[str, str]) -> None:
    """TEST-ONLY: overwrite the in-memory entries (no seal, no disk)."""
    _require()
    _state.entries = dict(entries)


def peek_bin_path() -> Path:
    """TEST-ONLY: where this environment's bin lives."""
    _, bin_path, _ = _require()
    return bin_path


def peek_backups_dir() -> Path:
    """TEST-ONLY: where day-granular snapshots land."""
    _, _, backups_dir = _require()
    return backups_dir


def peek_master_path() -> Path:
    """TEST-ONLY: the bin's master key file."""
    _require()
    assert _state.master_path is not None
    return _state.master_path


def peek_env_name() -> str:
    """TEST-ONLY: the bound environment name."""
    _require()
    assert _state.env_name is not None
    return _state.env_name


def poke_reset() -> None:
    """TEST-ONLY: forget all bindings (fresh-import equivalent)."""
    _reset()


def forge_seal(entries: dict[str, str]) -> bytes:
    """TEST-ONLY: seal a forged entry set with the live master key."""
    fernet, _, _ = _require()
    return _seal(fernet, entries)


def peek_parse_entry(entry: str) -> tuple[str, date] | None:
    """TEST-ONLY: parse an internal entry name back to (conn, date)."""
    return _parse_entry(entry)
