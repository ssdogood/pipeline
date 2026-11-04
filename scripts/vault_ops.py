#!/usr/bin/env python3
"""In-memory vault operations for pass_mgr.

Commands:
    list  <env>                   print secret names (never values)
    scan  <env> [--dry-run]        sync vault to match config files

POLICY (enforced by design):
    - There is NO fragment file. No human ever authors a plaintext
      secret file. Passwords enter only through the masked prompt
      (getpass) and are held in memory.
    - The ONLY plaintext ever written to disk is the short-lived
      internal temp file sops requires (sops v3.13.3 cannot read
      stdin). It is created at 0600 inside secrets/ (gitignored),
      and overwritten with garbage and unlinked immediately after
      encryption — lifetime is sub-second, same process.
    - The candidate vault is verified by decrypting it BEFORE the old
      vault is replaced (atomic os.replace after verification).
    - Values are never printed or logged. Logs contain names,
      actions, timestamps, and one-way fingerprints only.
    - The encrypted vault file always carries a human-readable
      warning header at the top, telling readers not to edit it.
    - The `secrets:` mapping in config/pass_mgr.<env>.yaml is
      machine-managed: scan cross-checks it against the extractors'
      credential_refs and offers to regenerate it. Comments ABOVE the
      `secrets:` line survive; everything below it is regenerated.
    - Backups (encrypted copies) keep the last 3 versions under
      secrets/backups/.
"""

import getpass
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
EXTRACTORS_DIR = PROJECT_ROOT / "config" / "extractors"
SECRETS_DIR = PROJECT_ROOT / "secrets"
BACKUP_DIR = SECRETS_DIR / "backups"
LOG_FILE = SECRETS_DIR / "logs" / "dpl_pass_mgr.log"

SOPS_TIMEOUT_SECONDS = 10
MAX_BACKUPS = 3

# Ensure operational directories exist before logging starts.
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | [%(levelname)s] | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def fingerprint(secret_name: str, value: str) -> str:
    """Short non-reversible fingerprint of a secret value, salted with its
    name. Used ONLY in logs to prove a change occurred (and whether a
    re-entered value is identical). Truncation is deliberate: it cannot
    be meaningfully brute-forced for a value of unknown length/charset."""
    payload = f"{secret_name}:{value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def fail(message: str) -> None:
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
    return yaml.safe_load(path.read_text())


def sops_settings(cfg: dict):
    """Return (binary, key_file, encrypted_file) for the sops method."""
    sops = (cfg.get("methods") or {}).get("sops")
    if not isinstance(sops, dict):
        fail("config has no 'sops' method section")

    binary = PROJECT_ROOT / sops["binary"]
    if not binary.is_file():
        fail(f"sops binary missing at {binary} — see docs/pass_mgr-guide.md, "
             f"section 'Restoring bin/'")

    key_file = Path(sops["age_key_file"]).expanduser()
    if not key_file.is_file():
        fail(f"age identity file missing at {key_file} — run "
             f"./setup_secrets.sh <env>, then confirm the config path")

    encrypted_file = PROJECT_ROOT / sops["encrypted_file"]
    return binary, key_file, encrypted_file


def recipient_from_identity(key_file: Path) -> str:
    """Extract the public key (age1...) from an age identity file."""
    for line in key_file.read_text().splitlines():
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
    Users WITHOUT a credential_ref are ignored — a connection may exist
    with no managed password. Commenting out a credential_ref retires it:
    the next scan flags the corresponding vault secret as stale.
    """
    refs = []
    if not EXTRACTORS_DIR.is_dir():
        log_action("SCAN", f"extractors directory not found at {EXTRACTORS_DIR}")
        return refs

    cfg_files = sorted(EXTRACTORS_DIR.glob("*.yaml"))
    for cfg_file in cfg_files:
        try:
            cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
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
                if cred_ref and isinstance(cred_ref, str):
                    refs.append((cred_ref, cfg_file.name, conn_name, user_name))

    log_action("SCAN", f"discovered {len(refs)} credential_ref entries "
                       f"across {len(cfg_files)} config files")
    return refs


# -------------------------------------------------------- config crosscheck --

def sync_secret_mapping(env: str, required_paths: set, dry_run: bool) -> None:
    """Cross-check extractor credential_refs against the `secrets:` mapping
    in config/pass_mgr.<env>.yaml and offer to regenerate the mapping so it
    contains EXACTLY the declared refs.

    The mapping is derived data — machine-managed. Rewrite rules:
      - everything ABOVE the `secrets:` line is preserved verbatim
        (comments included — keep your comments there);
      - the `secrets:` section itself is regenerated to match the refs.
    Returns silently when the two sides already agree.
    """
    config_path = CONFIG_DIR / f"pass_mgr.{env}.yaml"
    if not config_path.is_file():
        fail(f"no config for environment '{env}' ({config_path})")

    cfg = yaml.safe_load(config_path.read_text())
    current = set((cfg.get("secrets") or {}).keys())

    missing = sorted(required_paths - current)
    stale = sorted(current - required_paths)
    if not missing and not stale:
        return  # already canonical — say nothing

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

    lines = config_path.read_text().splitlines(keepends=True)
    cut_index = None
    for idx, line in enumerate(lines):
        if re.match(r"^secrets:\s*(#.*)?$", line):
            cut_index = idx
            break

    block = "secrets:\n" + "".join(f'  "{n}": {{}}\n'
                                   for n in sorted(required_paths))
    if cut_index is None:
        new_text = "".join(lines).rstrip("\n") + "\n\n" + block
    else:
        new_text = "".join(lines[:cut_index]) + block

    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text)
    os.replace(tmp, config_path)

    log_action("SYNC_MAPPING",
               f"pass_mgr.{env}.yaml regenerated: "
               f"added={[n for n in missing]}, removed={[n for n in stale]}")
    print("  [ok] Config mapping updated (commit this change).")


# --------------------------------------------------------------- sops I/O ---

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
        )
    except subprocess.TimeoutExpired:
        fail(f"sops did not respond within {SOPS_TIMEOUT_SECONDS}s — "
             f"verify the binary per the guide, then retry")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "(no output)"
        fail(f"sops exited with status {proc.returncode}: {detail}")
    return proc.stdout


def encrypt_tree(binary: Path, recipient: str, tree: dict) -> str:
    """Encrypt a nested secret tree, returning the ciphertext as a string.

    sops v3.13.3 cannot read plaintext from stdin ('-' is treated as a
    literal filename), so plaintext must pass through a short-lived temp
    file. Mitigations: created inside secrets/ (already gitignored),
    0600 permissions, .yaml suffix for mode sniffing, overwritten with
    garbage and unlinked immediately after encryption — all within the
    same process invocation.
    """
    plaintext_yaml = yaml.safe_dump(tree, sort_keys=True, default_flow_style=False)
    fd, tmp_name = tempfile.mkstemp(dir=SECRETS_DIR,
                                    prefix=".vault-plain-", suffix=".yaml")
    plaintext_file = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
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


def vault_header() -> str:
    """The human-readable warning prepended to every sealed vault file."""
    stamped = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# ============================================================================\n"
        "# AUTO-CREATED SECRET VAULT — DO NOT EDIT MANUALLY\n"
        "# ============================================================================\n"
        "# This file is machine-managed. Manage secrets ONLY via:\n"
        "#     ./vault.sh scan <env>   (add / rotate / delete secrets)\n"
        "#     ./vault.sh list <env>   (see which secret names exist)\n"
        "# Manual edits will be overwritten by the next scan and may\n"
        "# corrupt the vault. The contents below are ciphertext values.\n"
        f"# Last sealed: {stamped}\n"
        "# ============================================================================\n"
    )


def decrypt_to_tree(binary: Path, key_file: Path, vault_file: Path) -> dict:
    if not vault_file.is_file():
        return {}
    text = run_sops(
        binary,
        ["--decrypt", "--input-type", "yaml", "--output-type", "yaml",
         str(vault_file)],
        env_extra={"SOPS_AGE_KEY_FILE": str(key_file)},
    )
    return yaml.safe_load(text) or {}


def prune_backups() -> None:
    """Keep only the newest MAX_BACKUPS encrypted copies."""
    backups = sorted(BACKUP_DIR.glob("secrets.sops.yaml.*"))
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        try:
            oldest.unlink()
            log_action("PRUNE", f"deleted old backup: {oldest.name}")
        except OSError as e:
            log_action("PRUNE_WARN", f"could not delete {oldest}: {e}")


def create_backup(encrypted_file: Path) -> Path:
    """Timestamped encrypted backup of the current vault, then prune."""
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = BACKUP_DIR / f"secrets.sops.yaml.{timestamp}"
    shutil.copy2(encrypted_file, backup_path)
    prune_backups()
    log_action("BACKUP", f"created: {backup_path.name}")
    return backup_path


def seal_and_verify(binary: Path, key_file: Path, target: Path,
                    tree: dict) -> Path:
    """Encrypt tree, verify the candidate decrypts correctly, back up
    the old vault, THEN atomically replace it. The old vault survives
    any failure short of the final os.replace."""
    expected_names = set(flatten(tree))
    fd, tmp_name = tempfile.mkstemp(dir=target.parent,
                                    prefix=".vault-", suffix=".sops.tmp")
    candidate = Path(tmp_name)
    try:
        ciphertext = encrypt_tree(binary, recipient_from_identity(key_file), tree)
        with os.fdopen(fd, "w") as fh:
            fh.write(vault_header())
            fh.write(ciphertext)

        reopened = run_sops(
            binary,
            ["--decrypt", "--input-type", "yaml", "--output-type", "yaml",
             str(candidate)],
            env_extra={"SOPS_AGE_KEY_FILE": str(key_file)},
        )
        reopened_names = set(flatten(yaml.safe_load(reopened) or {}))
        if reopened_names != expected_names:
            raise RuntimeError(
                f"verification mismatch: expected {sorted(expected_names)}, "
                f"got {sorted(reopened_names)}")

        if target.is_file():
            create_backup(target)

        os.replace(candidate, target)
        log_action("WRITE", f"vault updated: {target}")
        return target
    except SystemExit:
        candidate.unlink(missing_ok=True)
        raise
    except BaseException:
        candidate.unlink(missing_ok=True)
        fail(f"failed while sealing vault; old vault left untouched: "
             f"{sys.exc_info()[1]}")


# -------------------------------------------------------------- commands ----

def cmd_list(env: str) -> None:
    log_action("LIST", f"environment={env}")
    binary, key_file, encrypted_file = sops_settings(load_env_config(env))
    tree = decrypt_to_tree(binary, key_file, encrypted_file)
    names = sorted(flatten(tree))
    if not names:
        print(f"(vault for '{env}' is empty)")
        return
    for name in names:
        print(name)


def cmd_scan(env: str, dry_run: bool = False) -> None:
    """Declarative sync: extractor configs are the source of truth; the
    vault (and the config's secrets mapping) are implementations.
    Flow: cross-check config mapping -> discover -> categorize -> prompt
    -> confirm -> backup -> write. Nothing is written to the vault until
    the final confirmation, so Ctrl-C during password entry is clean.
    """
    log_action("SCAN_START", f"environment={env}, dry_run={dry_run}")
    binary, key_file, encrypted_file = sops_settings(load_env_config(env))

    refs = discover_credential_refs()
    required_paths = set(r[0] for r in refs)

    if not required_paths:
        print("\nNo credential_ref entries found in config/extractors/*.yaml")
        print("Hint: under a user, add "
              "'credential_ref: <conn>.<user>.password'")
        return

    # Cross-check + optional regeneration of the config secrets mapping.
    sync_secret_mapping(env, required_paths, dry_run=dry_run)

    existing_tree = decrypt_to_tree(binary, key_file, encrypted_file)
    existing_paths = set(flatten(existing_tree).keys())
    if not existing_paths:
        log_action("SCAN", "no vault exists yet — first scan will create it")

    new_paths = sorted(required_paths - existing_paths)
    existing_but_stale = sorted(existing_paths - required_paths)
    existing_and_needed = sorted(required_paths & existing_paths)

    # Detect empty namespaces left by earlier deletions (cleanup candidates)
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
    print(f"Vault currently holds {len(existing_paths)} secret(s)")

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
        print(f"\n[DRY RUN] No changes will be made.")
        print(f"To proceed, run: ./vault.sh scan {env}")
        return

    # Interactive collection (nothing written to the vault yet)
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
                                  "current one — logging anyway.")
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

    # Summary and final confirmation
    if not (additions or updates or deletions or cleaned):
        print(f"\n{'='*60}")
        print("NO CHANGES REQUIRED")
        print(f"{'='*60}")
        print("All secrets in vault match the config file declarations.")
        return

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Additions:  {len(additions)}")
    print(f"Updates:    {len(updates)}")
    print(f"Deletions:  {len(deletions)}")
    print(f"Cleanup:    {len(cleaned)} empty namespace(s) removed")
    print(f"Unchanged:  {len(existing_and_needed) - len(updates)}")

    print(f"\nReady to write the reconciled vault.")
    print(f"A backup of the current vault will be saved to {BACKUP_DIR} "
          f"before the write.")
    resp = input("Proceed? (y/N) ").strip().lower()
    if resp != "y":
        log_action("SCAN_ABORT", "user declined final confirmation")
        print("Aborted. Nothing was written.")
        return

    seal_and_verify(binary, key_file, encrypted_file, final_tree)

    for path in deletions:
        log_action("DELETE", f"removed from vault: {path}")

    final_names = sorted(flatten(final_tree))
    print(f"\nFinal vault contains {len(final_names)} secret(s):")
    for name in final_names:
        print(f"  {name}")

    log_action("SCAN_COMPLETE",
               f"added={len(additions)}, updated={len(updates)}, "
               f"deleted={len(deletions)}, cleaned={len(cleaned)}, "
               f"final_count={len(final_names)}")


# ------------------------------------------------------------------ main ----

USAGE = """usage: vault.sh <command> <environment> [options]

commands:
  list  <env>                  print secret names (never values)
  scan  <env> [--dry-run]      sync vault to match config/extractors/*.yaml

notes:
  - 'scan' is the ONLY way to add, rotate, or delete secrets.
    New secrets are entered via a masked prompt; no plaintext
    fragment files are ever used.
  - 'scan' also cross-checks and regenerates the secrets: mapping in
    config/pass_mgr.<env>.yaml to match the extractors' credential_refs.
  - Always run with --dry-run first to preview the reconciliation plan.
  - A vault is created automatically on the first 'scan' if none exists.
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
