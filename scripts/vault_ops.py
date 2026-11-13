#!/usr/bin/env python3
"""In-memory vault operations for pass_mgr.

Commands:
    list  <env>                   print secret names (never values)
    scan  <env> [--dry-run]        sync vault to match config files

ARCHITECTURE (fernet-canonical):
    - The FERNET vault is canonical. scan maintains it unconditionally.
      Fernet has no external dependencies (baked into the Python
      environment), so secret maintenance works on any machine with
      Python + `cryptography`.
    - The SOPS vault is an opportunistic MIRROR. It is refreshed only
      when the sops binary and age identity are present on this machine.
      Any missing prerequisite -> quiet skip, logged as SYNC_SKIP.
      Sync is one-way: fernet -> sops, always. sops presence is never
      required for the primary flow.
    - default_method in config decides which vault the PIPELINE reads
      at runtime; scan always treats fernet as the source of truth.

POLICY (enforced by design):
    - There is NO fragment file. No human ever authors a plaintext
      secret file. Passwords enter only through the masked prompt
      (getpass) and are held in memory.
    - The ONLY plaintext ever written to disk is the short-lived
      internal temp file sops requires (sops v3.13.3 cannot read
      stdin). It is created at 0600 inside secrets/ (gitignored),
      overwritten with garbage and unlinked immediately after
      encryption - lifetime is sub-second, same process.
    - Candidate vaults are verified before the old file is replaced
      (atomic os.replace after verification), for BOTH methods.
    - Values are never printed or logged. Logs contain names,
      actions, timestamps, and one-way fingerprints only.
    - Both vault files carry the machine-generated DO-NOT-EDIT header,
      re-applied on every seal/sync.
    - Backups (encrypted copies) keep the last 3 versions of EACH
      vault under secrets/backups/.
    - The `secrets:` mapping in config/pass_mgr.<env>.yaml is
      machine-managed (see sync_secret_mapping).
"""

import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import yaml
from cryptography.fernet import Fernet, InvalidToken

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
EXTRACTORS_DIR = PROJECT_ROOT / "config" / "extractors"
SMTP_CREDENTIALS_DIR = PROJECT_ROOT / "config" / "smtp"
SECRETS_DIR = PROJECT_ROOT / "secrets"
BACKUP_DIR = SECRETS_DIR / "backups"
LOG_FILE = SECRETS_DIR / "logs" / "dpl_pass_mgr.log"

SOPS_TIMEOUT_SECONDS = 10
MAX_BACKUPS = 3

# Must match FERNET_ENVELOPE_VERSION in src/dpl/lib/pass_mgr/_providers.py.
# If the library bumps this for v2 rotation, this tool follows.
FERNET_ENVELOPE_VERSION = 1

# Ensure operational directories exist before logging starts.
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_FINGERPRINT_KEY = secrets.token_bytes(32)

NAME_RE = re.compile(r"^[A-Za-z0-9_.\\-]+$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | [%(levelname)s] | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def fingerprint(secret_name: str, value: str | None) -> str:
    """Keyed, truncated log fingerprint of a secret value, bound to its
    name. Used ONLY in logs to prove a change occurred within this run.
    HMAC (not a bare hash) so log readers without this process's key
    cannot offline-test password guesses; truncation to 64 bits keeps
    fingerprints short and gives negligible false-match probability."""
    display = value if value is not None else "<absent>"
    payload = f"{secret_name}:{display}".encode()
    return hmac.new(_FINGERPRINT_KEY, payload, hashlib.sha256).hexdigest()[:16]

def fail(message: str) -> NoReturn:
    logger.error(f"VAULT ERROR: {message}")
    print(f"\nVAULT ERROR: {message}\n", file=sys.stderr)
    sys.exit(1)


def log_action(action: str, details: str) -> None:
    """Log a vault operation to the audit file, echo one line."""
    msg = f"{action}: {details}"
    logger.info(msg)
    print(f"[LOG] {msg}")


# ---------------------------------------------------------------- config ----

def load_env_config(env: str) -> dict:
    path = CONFIG_DIR / f"pass_mgr.{env}.yaml"
    if not path.is_file():
        known = sorted(p.name for p in CONFIG_DIR.glob("pass_mgr.*.yaml"))
        fail(f"no config for environment '{env}' (looked for {path}); "
             f"known configs: {known}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fernet_settings(cfg: dict):
    """Canonical vault settings. The fernet key is a HARD prerequisite:
    a missing key is a hard failure, not a skip - the canonical vault
    cannot be maintained without it."""
    fernet = (cfg.get("methods") or {}).get("fernet")
    if not isinstance(fernet, dict):
        fail("config has no 'fernet' method section - fernet is the "
             "canonical vault and must be configured")

    key_file = Path(fernet["key_file"]).expanduser()
    if not key_file.is_file():
        fail(f"fernet key file missing at {key_file} - run "
             f"./setup_secrets.sh <env>, then confirm the config path")

    encrypted_file = PROJECT_ROOT / fernet["encrypted_file"]
    return key_file, encrypted_file


def optional_sops_settings(cfg: dict):
    """MIRROR vault settings, or None when sops is not provisioned on
    this machine. Never crashes the flow: every missing prerequisite is
    a quiet skip, logged as SYNC_SKIP so drift is visible in the audit
    trail."""
    sops = (cfg.get("methods") or {}).get("sops")
    if not isinstance(sops, dict):
        log_action("SYNC_SKIP", "no sops method section in config - "
                                "sops mirror not maintained")
        return None

    binary = PROJECT_ROOT / sops["binary"]
    key_file = Path(sops["age_key_file"]).expanduser()
    encrypted_file = PROJECT_ROOT / sops["encrypted_file"]

    if not binary.is_file():
        log_action("SYNC_SKIP", f"sops binary not present at {binary} - "
                                "sops mirror not maintained")
        return None
    if not key_file.is_file():
        log_action("SYNC_SKIP", f"age identity not present at {key_file} - "
                                "sops mirror not maintained")
        return None

    return binary, key_file, encrypted_file


def recipient_from_identity(key_file: Path) -> str:
    """Extract the public key (age1...) from an age identity file."""
    for line in key_file.read_text(encoding="utf-8").splitlines():
        lowered = line.lower()
        if lowered.startswith("#") and "public key:" in lowered:
            for token in line.replace(":", " ").split():
                if token.startswith("age1"):
                    return token
    fail(f"could not find a public key (age1...) inside {key_file}; "
         f"regenerate with ./setup_secrets.sh")


# ------------------------------------------------------------- tree utils ---

def flatten(tree: dict, prefix: str = "") -> dict:
    """Flatten nested namespaces into dotted names; validates leaves."""
    result = {}
    for key, value in tree.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            if not value:
                continue  # tolerate empty namespaces (cleaned by prune)
            result.update(flatten(value, name))
        elif isinstance(value, str):
            result[name] = value
        else:
            fail(f"secret '{name}' must be a string, "
                 f"got {type(value).__name__}")
    return result


def unflatten(paths: dict) -> dict:
    """Convert flat dotted paths back into a nested structure."""
    result = {}
    for path, value in paths.items():
        parts = path.split(".")
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


def merge_tree(base: dict, extra: dict) -> dict:
    """Recursively merge 'extra' into 'base' (extra wins on conflict)."""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_tree(result[key], value)
        else:
            result[key] = value
    return result


def prune_empty_namespaces(tree: dict) -> list:
    """Remove empty namespace dicts left behind by leaf deletions.
    Mutates the tree in place; returns names of removed namespaces."""
    removed = []
    for key in list(tree.keys()):
        if isinstance(tree[key], dict):
            removed += prune_empty_namespaces(tree[key])
            if not tree[key]:
                del tree[key]
                removed.append(key)
    return removed


def discover_credential_refs() -> list:
    """Scan config/extractors/*.yaml and collect all credential_ref paths.

    Returns a list of tuples: [(vault_path, source_file, connection, user)]
    Users WITHOUT a credential_ref are ignored - a connection may exist
    with no managed password. Commenting out a credential_ref retires it.
    """
    refs = []
    if not EXTRACTORS_DIR.is_dir():
        log_action("SCAN", f"extractors directory not found at {EXTRACTORS_DIR}")
        return refs
    cfg_files = sorted(EXTRACTORS_DIR.glob("*.yaml"))
    # SMTP credentials live in their own folder (NOT an extractor);
    # same connections/users/credential_ref shape as extractor files.
    if SMTP_CREDENTIALS_DIR.is_dir():
        cfg_files += sorted(SMTP_CREDENTIALS_DIR.glob("*.yaml"))
    for cfg_file in cfg_files:
        try:
            cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:                                           # noqa: BLE001 - scan must survive one bad config file; log and continue
            log_action("SCAN_ERROR", f"failed to parse {cfg_file.name}: {e}")
            continue

        connections = cfg.get("connections", {}) if isinstance(cfg, dict) else {}
        if not isinstance(connections, dict):
            continue

        for conn_name, conn_cfg in connections.items():
            if not isinstance(conn_cfg, dict):
                continue
            users = conn_cfg.get("users", {})
            if not isinstance(users, dict):
                continue
            for user_name, user_cfg in users.items():
                if not isinstance(user_cfg, dict):
                    continue
                cred_ref = user_cfg.get("credential_ref")
                if not isinstance(cred_ref, str) or not cred_ref.strip():
                    continue   # users WITHOUT a credential_ref are
                                # ignored - by design (commenting out a
                                # credential_ref retires it)
                if not NAME_RE.match(cred_ref):
                    log_action("SCAN_ERROR",
                                f"invalid credential_ref '{cred_ref}' in "
                                f"{cfg_file.name} - allowed: letters, "
                                f"digits, '.', '_', '-', '\\'")
                    continue
                refs.append((cred_ref, cfg_file.name,
                                conn_name, user_name))

    log_action("SCAN", f"discovered {len(refs)} credential_ref entries "
                       f"across {len(cfg_files)} config files")
    return refs


# -------------------------------------------------------- config crosscheck --

def sync_secret_mapping(env: str, required_paths: set, dry_run: bool) -> None:
    """Cross-check extractor credential_refs against the `secrets:` mapping
    in config/pass_mgr.<env>.yaml and offer to regenerate the mapping so it
    contains EXACTLY the declared refs.

    The mapping is derived data - machine-managed. Rewrite rules:
      - everything ABOVE the `secrets:` line is preserved verbatim;
      - the `secrets:` section itself is regenerated to match the refs.
    Returns silently when the two sides already agree.
    """
    config_path = CONFIG_DIR / f"pass_mgr.{env}.yaml"
    if not config_path.is_file():
        fail(f"no config for environment '{env}' ({config_path})")

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    current = set((cfg.get("secrets") or {}).keys())

    missing = sorted(required_paths - current)
    stale = sorted(current - required_paths)
    if not missing and not stale:
        return  # already canonical - say nothing

    print(f"\n--- CONFIG CROSS-CHECK (pass_mgr.{env}.yaml) ---")
    if missing:
        print(f"  + declared in extractors, missing from config ({len(missing)}):")
        for name in missing:
            print(f"      {name}")
    if stale:
        print(f"  - in config, but no extractor declares them ({len(stale)}):")
        for name in stale:
            print(f"      {name}")

    if dry_run:
        print("  [DRY RUN] mapping left unchanged")
        return

    resp = input("  Regenerate the secrets: mapping to match the extractors? (y/N) "
                 ).strip().lower()
    if resp != "y":
        print("  [skip] Config left unchanged. Note: get_secret() will refuse "
              "names that are not declared in this mapping.")
        log_action("SYNC_MAPPING", f"declined for {env}")
        return

    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    cut_index = None
    for idx, line in enumerate(lines):
        if re.match(r"^secrets:\s*(#.*)?$", line):
            cut_index = idx
            break

    block = "secrets:\n" + "".join(f'  {json.dumps(n)}: {{}}\n'
                                   for n in sorted(required_paths))
    if cut_index is None:
        new_text = "".join(lines).rstrip("\n") + "\n\n" + block
    else:
        new_text = "".join(lines[:cut_index]) + block

    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, config_path)

    log_action("SYNC_MAPPING",
               f"pass_mgr.{env}.yaml regenerated: "
               f"added={[n for n in missing]}, removed={[n for n in stale]}")
    print("  [ok] Config mapping updated (commit this change).")


# ---------------------------------------------------------- fernet I/O ------

def _read_envelope_lines(path: Path) -> str:
    """Return the single non-comment token line of an envelope file.
    Mirrors the header-tolerance of FernetProvider.load(): '#' comment
    lines (the DO-NOT-EDIT banner) are skipped; exactly one non-empty,
    non-comment line must remain."""
    token_lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(token_lines) != 1:
        fail(f"fernet envelope {path} must contain exactly one token line, "
             f"found {len(token_lines)}")
    return token_lines[0]


def _load_fernet_key(key_file: Path) -> Fernet:
    key_text = key_file.read_text(encoding="utf-8").strip()
    try:
        return Fernet(key_text)
    except (ValueError, TypeError):
        fail(f"fernet key file {key_file} does not contain a valid key")


def decrypt_fernet_tree(key_file: Path, vault_file: Path) -> dict:
    """Decrypt the canonical fernet vault into a nested tree, in memory.
    A missing vault is an empty tree (first scan will create it)."""
    if not vault_file.is_file():
        return {}
    fernet = _load_fernet_key(key_file)
    token = _read_envelope_lines(vault_file)
    try:
        payload = fernet.decrypt(token.encode("ascii"))
    except InvalidToken:
        fail(f"fernet decryption failed for {vault_file} - wrong key or "
             f"corrupted envelope. Restore from {BACKUP_DIR} or re-seal.")
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as e:
        fail(f"fernet envelope payload is not valid JSON: {e}")
    if envelope.get("version") != FERNET_ENVELOPE_VERSION:
        fail(f"fernet envelope version {envelope.get('version')!r} "
             f"unsupported (expected {FERNET_ENVELOPE_VERSION})")
    entries = envelope.get("entries")
    if not isinstance(entries, dict) or not entries:
        fail("fernet envelope has no 'entries' mapping")
    return unflatten(entries)


def fernet_header() -> str:
    """The human-readable warning prepended to every fernet vault."""
    stamped = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# ============================================================================\n"
        "# AUTO-CREATED SECRET VAULT (Fernet, canonical) - DO NOT EDIT MANUALLY\n"
        "# ============================================================================\n"
        "# This file is machine-managed. Manage secrets ONLY via:\n"
        "#     ./vault.sh scan <env>   (add / rotate / delete secrets)\n"
        "#     ./vault.sh list <env>   (see which secret names exist)\n"
        "# The token line below is the encrypted envelope (JSON payload,\n"
        "# AES-128-CBC + HMAC-SHA256). Values cannot be recovered without\n"
        "# the Fernet key file, which lives outside the project tree.\n"
        f"# Last synced: {stamped}\n"
        "# ============================================================================\n"
    )


def seal_fernet_and_verify(key_file: Path, target: Path, tree: dict) -> Path:
    """Flatten the tree into the envelope format (flat dotted names),
    encrypt, verify the candidate decrypts back to the same entries,
    back up the old vault, THEN atomically replace it."""
    entries = flatten(tree)
    if not entries:
        fail("refusing to seal an empty fernet vault")

    fernet = _load_fernet_key(key_file)
    envelope = json.dumps({"version": FERNET_ENVELOPE_VERSION,
                           "entries": entries}).encode("utf-8")
    token = fernet.encrypt(envelope).decode("ascii")

    fd, tmp_name = tempfile.mkstemp(dir=target.parent,
                                    prefix=".fernet-", suffix=".tmp")
    candidate = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(fernet_header())
            fh.write(token + "\n")

        # Round-trip verification on the CANDIDATE, before replacement.
        check_token = _read_envelope_lines(candidate)
        check_payload = fernet.decrypt(check_token.encode("ascii"))
        check = json.loads(check_payload).get("entries")
        if check != entries:
            raise RuntimeError("fernet verification mismatch: the candidate "
                               "does not decrypt to the expected entries")

        if target.is_file():
            create_backup(target)

        os.replace(candidate, target)    # atomic on POSIX
        log_action("WRITE", f"fernet vault updated: {target}")
        return target
    except SystemExit:
        candidate.unlink(missing_ok=True)
        raise
    except BaseException:                       # noqa: BLE001 - teardown: unlink temp even on interrupt/abort (incl. KeyboardInterrupt) before re-raising via fail()
        candidate.unlink(missing_ok=True)
        fail(f"failed while sealing fernet vault; old vault left "
             f"untouched: {sys.exc_info()[1]}")


# ------------------------------------------------------------- sops I/O ------

def run_sops(binary: Path, args, *, env_extra=None, stdin_data=None) -> str:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            [str(binary), *args],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=SOPS_TIMEOUT_SECONDS,
            env=env, 
            check=False
        )
    except subprocess.TimeoutExpired:
        fail(f"sops did not respond within {SOPS_TIMEOUT_SECONDS}s - "
             f"verify the binary per the guide, then retry")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "(no output)"
        fail(f"sops exited with status {proc.returncode}: {detail}")
    return proc.stdout


def encrypt_tree(binary: Path, recipient: str, tree: dict) -> str:
    """Encrypt a nested secret tree for the sops MIRROR.

    sops v3.13.3 cannot read plaintext from stdin ('-' is treated as a
    literal filename), so plaintext must pass through a short-lived temp
    file. Mitigations: created inside secrets/ (already gitignored),
    0600 permissions, .yaml suffix for mode sniffing, overwritten with
    garbage and unlinked immediately after encryption - all within the
    same process invocation.
    """
    plaintext_yaml = yaml.safe_dump(tree, sort_keys=True, default_flow_style=False)
    fd, tmp_name = tempfile.mkstemp(dir=SECRETS_DIR,
                                    prefix=".vault-plain-", suffix=".yaml")
    os.close(fd)  # close raw descriptor; file is reopened by path below
    plaintext_file = Path(tmp_name)
    try:
        with open(plaintext_file, "w", encoding="utf-8") as fh:
            fh.write(plaintext_yaml)
        os.chmod(plaintext_file, 0o600)
        return run_sops(binary,
                        ["--encrypt", "--age", recipient,
                         "--input-type", "yaml", str(plaintext_file)])
    finally:
        try:
            with open(plaintext_file, "w") as fh:
                fh.write("x" * 64)
            plaintext_file.unlink()
        except OSError:
            pass


def sops_header() -> str:
    """The human-readable warning prepended to every sops mirror vault."""
    stamped = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# ============================================================================\n"
        "# AUTO-CREATED SECRET VAULT (SOPS) - DO NOT EDIT MANUALLY\n"
        "# ============================================================================\n"
        "# This file is machine-managed and DERIVED: every value here is a\n"
        "# copy of the canonical Fernet vault. Manage secrets ONLY via:\n"
        "#     ./vault.sh scan <env>   (add / rotate / delete secrets)\n"
        "#     ./vault.sh list <env>   (see which secret names exist)\n"
        "# Manual edits will be overwritten by the next scan and may\n"
        "# corrupt the vault. The contents below are ciphertext; values\n"
        "# cannot be recovered without the age identity file.\n"
        f"# Last synced: {stamped}\n"
        "# ============================================================================\n"
    )


def decrypt_sops_tree(binary: Path, key_file: Path, vault_file: Path) -> dict:
    """Decrypt the sops mirror into a nested tree, in memory."""
    if not vault_file.is_file():
        return {}
    text = run_sops(
        binary,
        ["--decrypt", "--input-type", "yaml", "--output-type", "yaml",
         str(vault_file)],
        env_extra={"SOPS_AGE_KEY_FILE": str(key_file)},
    )
    return yaml.safe_load(text) or {}


def seal_sops_and_verify(binary: Path, key_file: Path, target: Path,
                         tree: dict) -> Path:
    """Encrypt tree for the sops mirror, verify the candidate decrypts
    correctly, back up the old vault, THEN atomically replace it."""
    expected_entries = flatten(tree)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent,
                                    prefix=".vault-", suffix=".sops.tmp")
    candidate = Path(tmp_name)
    try:
        ciphertext = encrypt_tree(binary, recipient_from_identity(key_file), tree)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sops_header())
            fh.write(ciphertext)

        reopened = run_sops(
            binary,
            ["--decrypt", "--input-type", "yaml", "--output-type", "yaml",
             str(candidate)],
            env_extra={"SOPS_AGE_KEY_FILE": str(key_file)},
        )
        reopened_entries = flatten(yaml.safe_load(reopened) or {})
        if reopened_entries != expected_entries:
            raise RuntimeError(
                f"verification mismatch: expected {sorted(expected_entries)}, "
                f"got {sorted(reopened_entries)}")

        if target.is_file():
            create_backup(target)

        os.replace(candidate, target)
        log_action("WRITE", f"sops mirror updated: {target}")
        return target
    except SystemExit:
        candidate.unlink(missing_ok=True)
        raise
    except BaseException:                                       # noqa: BLE001 - same teardown contract as fernet seal
        candidate.unlink(missing_ok=True)
        fail(f"failed while sealing the sops mirror; old vault left "
             f"untouched: {sys.exc_info()[1]}")


def sops_mirror_needed(sops_cfg, flat_entries: dict) -> bool:
    """True when the sops mirror is absent, unreadable, or differs from
    the canonical entries (name OR value). An unreadable mirror is
    refreshed wholesale - the canonical vault is always trusted."""
    _, _, sops_vault_file = sops_cfg
    if not sops_vault_file.is_file():
        return True
    try:
        existing = flatten(decrypt_sops_tree(*sops_cfg))
    except SystemExit:
        return True  # unreadable / wrong key / corrupt -> re-seal fresh
    return existing != flat_entries


# --------------------------------------------------------------- backups -----

def prune_backups(basename: str) -> None:
    """Keep only the newest MAX_BACKUPS copies of ONE vault file."""
    backups = sorted(BACKUP_DIR.glob(f"{basename}.*"))
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        try:
            oldest.unlink()
            log_action("PRUNE", f"deleted old backup: {oldest.name}")
        except OSError as e:
            log_action("PRUNE_WARN", f"could not delete {oldest}: {e}")


def create_backup(encrypted_file: Path) -> Path:
    """Timestamped encrypted backup of a vault file, then prune."""
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = BACKUP_DIR / f"{encrypted_file.name}.{timestamp}"
    shutil.copy2(encrypted_file, backup_path)
    prune_backups(encrypted_file.name)
    log_action("BACKUP", f"created: {backup_path.name}")
    return backup_path


# -------------------------------------------------------------- commands ----

def cmd_list(env: str) -> None:
    log_action("LIST", f"environment={env}")
    cfg = load_env_config(env)
    key_file, encrypted_file = fernet_settings(cfg)
    tree = decrypt_fernet_tree(key_file, encrypted_file)
    names = sorted(flatten(tree))
    if not names:
        print(f"(no fernet vault for '{env}' yet, or it is empty - "
              f"run: ./vault.sh scan {env})")
        return
    for name in names:
        print(name)


def cmd_scan(env: str, dry_run: bool = False) -> None:
    """Declarative sync: extractor configs are the source of truth; the
    fernet vault (canonical) and the sops vault (mirror) are
    implementations. Flow: cross-check config mapping -> discover ->
    categorize -> prompt -> confirm -> backup -> write fernet -> mirror
    to sops when provisioned. Nothing is written until the final
    confirmation, so Ctrl-C during password entry is a clean abort."""
    log_action("SCAN_START", f"environment={env}, dry_run={dry_run}")
    cfg = load_env_config(env)

    # Canonical vault - hard prerequisites, no skipping.
    fernet_key, fernet_vault = fernet_settings(cfg)

    # Mirror vault - every missing prerequisite is a quiet skip.
    sops_cfg = optional_sops_settings(cfg)

    refs = discover_credential_refs()
    required_paths = {r[0] for r in refs}

    if not required_paths:
        existing_now = set(flatten(decrypt_fernet_tree(fernet_key, fernet_vault)))
        if not existing_now:
            print("\nNo credential_ref entries found in config/extractors/*.yaml")
            print("Hint: under a user, add 'credential_ref: <conn>.<user>.password'")
            return
        print("\nNOTE: no credential_refs declared - ALL existing secrets "
              "count as stale. The secrets: mapping in the config is left "
              "unchanged (load_config requires a non-empty mapping).")

    sync_secret_mapping(env, required_paths, dry_run=dry_run)

    existing_tree = decrypt_fernet_tree(fernet_key, fernet_vault)
    existing_paths = set(flatten(existing_tree).keys())
    if not existing_paths:
        log_action("SCAN", "no fernet vault exists yet - first scan "
                           "will create it")

    new_paths = sorted(required_paths - existing_paths)
    existing_but_stale = sorted(existing_paths - required_paths)
    existing_and_needed = sorted(required_paths & existing_paths)

    # Detect empty namespaces left by earlier deletions
    cleanup_probe = dict(existing_tree)
    pending_cleanup = prune_empty_namespaces(cleanup_probe)
    if pending_cleanup:
        print(f"\n--- EMPTY NAMESPACES TO CLEAN UP ({len(pending_cleanup)}) ---")
        for ns in pending_cleanup:
            print(f"  x {ns}")

    print(f"\n{'='*60}")
    print("PASS_MGR SCAN RECONCILIATION")
    print(f"{'='*60}")
    print(f"\nDiscovered {len(required_paths)} credential_ref(s) in config")
    print(f"Fernet vault (canonical) holds {len(existing_paths)} secret(s)")
    if sops_cfg:
        print("SOPS mirror: provisioned - will be refreshed if it drifts")
    else:
        print("SOPS mirror: not provisioned on this machine (skipped, logged)")

    if new_paths:
        print(f"\n--- NEW SECRETS TO ADD ({len(new_paths)}) ---")
        for p in new_paths:
            print(f"  + {p}")
    if existing_but_stale:
        print(f"\n--- STALE SECRETS (no longer referenced) "
              f"({len(existing_but_stale)}) ---")
        for p in existing_but_stale:
            print(f"  ? {p}")
    if existing_and_needed:
        print(f"\n--- EXISTING SECRETS (rotate or keep) "
              f"({len(existing_and_needed)}) ---")
        for p in existing_and_needed:
            print(f"  ~ {p}")

    if dry_run:
        print("\n[DRY RUN] No changes will be made.")
        print(f"To proceed, run: ./vault.sh scan {env}")
        return

    # Interactive collection (nothing written yet)
    final_tree = dict(existing_tree)
    additions = []
    updates = []
    deletions = []

    if new_paths:
        print(f"\n{'-'*60}")
        print("NEW SECRETS: enter passwords (input is masked)")
        print(f"{'-'*60}")
        for path in new_paths:
            while True:
                print(f"\n{path}")
                pwd = getpass.getpass("Password: ")
                confirm = getpass.getpass("Confirm: ")
                if pwd == confirm and pwd:
                    additions.append(path)
                    nested = unflatten({path: pwd})
                    final_tree = merge_tree(final_tree, nested)
                    log_action("FINGERPRINT",
                               f"{path}: new={fingerprint(path, pwd)}")
                    print(f"[ok] Password recorded for {path}")
                    break
                print("[retry] Entries did not match (or were empty). Try again.")

    if existing_but_stale:
        print(f"\n{'-'*60}")
        print("STALE SECRETS: confirm deletion")
        print(f"{'-'*60}")
        for path in existing_but_stale:
            resp = input(f"Delete {path}? (y/n) ").strip().lower()
            if resp == "y":
                deletions.append(path)
            else:
                print(f"[skip] Keeping {path}")

    if existing_and_needed:
        print(f"\n{'-'*60}")
        print("EXISTING SECRETS: rotate password or keep?")
        print(f"{'-'*60}")
        for path in existing_and_needed:
            resp = input(f"Rotate password for {path}? (y/n) ").strip().lower()
            if resp == "y":
                while True:
                    pwd = getpass.getpass("New password: ")
                    confirm = getpass.getpass("Confirm: ")
                    if pwd == confirm and pwd:
                        old = flatten(existing_tree).get(path)
                        if old == pwd:
                            print("[note] New password is identical to the "
                                  "current one - logging anyway.")
                        updates.append(path)
                        nested = unflatten({path: pwd})
                        final_tree = merge_tree(final_tree, nested)
                        log_action("FINGERPRINT",
                                   f"{path}: old={fingerprint(path, old)} "
                                   f"new={fingerprint(path, pwd)}")
                        print(f"[ok] Password updated for {path}")
                        break
                    print("[retry] Entries did not match (or were empty). "
                          "Try again.")
            else:
                print(f"[skip] Keeping existing password for {path}")

    # Apply deletions to the in-memory tree
    for path in deletions:
        parts = path.split(".")
        current = final_tree
        for part in parts[:-1]:
            current = current.get(part, {})
        if parts[-1] in current:
            del current[parts[-1]]

    # Cleanup after applying deletions
    cleaned = prune_empty_namespaces(final_tree)

    # Mirror drift check against the FINAL tree
    flat_final = flatten(final_tree)
    if not flat_final:
        print(f"\n{'='*60}")
        print(f"VAULT RETIREMENT - all {len(existing_paths)} secret(s) "
              f"would be REMOVED")
        print(f"{'='*60}")
        print(f"Backup copies will be kept in {BACKUP_DIR}.")
        resp = input("Type 'retire' to delete the vault files: ").strip().lower()
        if resp != "retire":
            log_action("SCAN_ABORT", "user declined vault retirement")
            print("Aborted. Nothing was written.")
            return
        if fernet_vault.is_file():
            create_backup(fernet_vault)
            fernet_vault.unlink()
            log_action("RETIRED", f"fernet vault removed: {fernet_vault}")
        mirror_file = sops_cfg[2] if sops_cfg else None
        if mirror_file and mirror_file.is_file():
            create_backup(mirror_file)
            mirror_file.unlink()
            log_action("RETIRED", f"sops mirror removed: {mirror_file}")
        print("\nVault retired. Backups retained in secrets/backups/.")
        log_action("SCAN_COMPLETE", "vault retired - all secrets removed")
        return
    
    mirror_needed = sops_mirror_needed(sops_cfg, flat_final) if sops_cfg else False

    # Summary and final confirmation
    fernet_changed = bool(additions or updates or deletions or cleaned)
    if not fernet_changed and not mirror_needed:
        print(f"\n{'='*60}")
        print("NO CHANGES REQUIRED")
        print(f"{'='*60}")
        print("Fernet vault matches the config declarations, and the sops "
              "mirror is in sync.")
        return

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Additions:  {len(additions)}")
    print(f"Updates:    {len(updates)}")
    print(f"Deletions:  {len(deletions)}")
    print(f"Cleanup:    {len(cleaned)} empty namespace(s) removed")
    print(f"Unchanged:  {len(existing_and_needed) - len(updates)}")
    print(f"Fernet vault:  {'will be rewritten' if fernet_changed else 'unchanged'}")
    if sops_cfg:
        print(f"SOPS mirror:   {'will be refreshed' if mirror_needed else 'already in sync'}")
    else:
        print("SOPS mirror:   skipped on this machine (see SYNC_SKIP in log)")

    print(f"\nReady to write. A backup of each rewritten vault will be "
          f"saved to {BACKUP_DIR} before its replacement.")
    resp = input("Proceed? (y/N) ").strip().lower()
    if resp != "y":
        log_action("SCAN_ABORT", "user declined final confirmation")
        print("Aborted. Nothing was written.")
        return

    if fernet_changed:
        seal_fernet_and_verify(fernet_key, fernet_vault, final_tree)
    else:
        log_action("SKIP_WRITE", "fernet vault unchanged - not rewritten")

    if sops_cfg and mirror_needed:
        seal_sops_and_verify(sops_cfg[0], sops_cfg[1], sops_cfg[2], final_tree)
        log_action("SYNC_SOPS", "sops mirror refreshed from canonical vault")

    for path in deletions:
        log_action("DELETE", f"removed from vault: {path}")

    final_names = sorted(flat_final)
    print(f"\nFinal vault contains {len(final_names)} secret(s):")
    for name in final_names:
        print(f"  {name}")

    log_action("SCAN_COMPLETE",
               f"added={len(additions)}, updated={len(updates)}, "
               f"deleted={len(deletions)}, cleaned={len(cleaned)}, "
               f"final_count={len(final_names)}, "
               f"sops_mirror={'refreshed' if (sops_cfg and mirror_needed) else ('in_sync' if sops_cfg else 'skipped')}")


# ------------------------------------------------------------------ main ----

USAGE = """usage: vault.sh <command> <environment> [options]

commands:
  list  <env>                  print secret names (never values)
  scan  <env> [--dry-run]      sync vaults to match config/extractors/*.yaml

notes:
  - The Fernet vault is canonical; the SOPS vault is an opportunistic
    mirror (refreshed only when the sops binary + age key are present).
  - 'scan' is the ONLY way to add, rotate, or delete secrets.
    New secrets are entered via a masked prompt; no plaintext
    fragment files are ever used.
  - 'scan' also cross-checks and regenerates the secrets: mapping in
    config/pass_mgr.<env>.yaml to match the extractors' credential_refs.
  - Always run with --dry-run first to preview the reconciliation plan.
  - Vaults are created automatically on the first 'scan' if none exist.
"""

def main(argv) -> int:
    args = argv[1:]
    flags = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]

    unknown = [f for f in flags if f != "--dry-run"]
    if unknown:
        sys.stderr.write(f"VAULT ERROR: unknown option(s): {' '.join(unknown)}\n\n")
        sys.stderr.write(USAGE)
        return 2

    if not positional:
        sys.stderr.write(USAGE)
        return 2

    command = positional[0]
    env = positional[1] if len(positional) > 1 else None

    if command == "list":
        if env is None:
            sys.stderr.write(
                "VAULT ERROR: 'list' needs an environment.\n"
                "    ./vault.sh list laptop\n\n")
            return 2
        cmd_list(env)

    elif command == "scan":
        if env is None:
            sys.stderr.write(
                "VAULT ERROR: 'scan' needs an environment.\n"
                "    ./vault.sh scan laptop --dry-run\n\n")
            return 2
        cmd_scan(env, dry_run=("--dry-run" in flags))

    else:
        sys.stderr.write(USAGE)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
