# pass_mgr Operations Guide

**Audience:** New joiners, security reviewers, and anyone restoring the tooling on a fresh machine.  
**Scope:** How to provision a machine from nothing, manage secrets, and recover from a missing `bin/`.  
**Policies:** Read verbatim — these are project rules, not suggestions.

---

## 1. Mental Model

Four principles, stated plainly:

1. **One vault per environment, one key per environment** — laptop secrets stay in a vault sealed with the laptop's age identity; prod secrets in a different vault sealed with prod's age identity. The laptop key cannot decrypt prod's vault (and vice versa). This containment limits any single-compromise blast radius to one environment.

2. **Keys live outside the project tree** — in `~/.dpl-keys/<env>/`. They never travel with the repository. The vault *can* travel with the tree (ciphertext, public recipient only), but the key must be provisioned per-machine via `setup_secrets.sh`.

3. **Config files are the source of truth; the vault is the implementation** — each connection/user that needs a password declares a `credential_ref` in its extractor config. `vault.sh scan` reconciles the vault to match. A secret that is declared but absent from the vault gets added (via masked prompt); a secret in the vault but no longer referenced gets offered for deletion.

4. **Passwords enter only through the masked prompt** — no human ever authors a plaintext secret file. `scan` is the only way to add, rotate, or delete secrets. The only plaintext that ever touches disk is a sub-second internal temp file that sops requires (see Policy Statements).

### File Inventory

| Location | Contents | Git-tracked? | Secret? |
|---|---|---|---|
| `bin/` | Pinned sops & age-keygen binaries + manifest | Yes | No (public binaries) |
| `config/pass_mgr.<env>.yaml` | pass_mgr environment config (paths, methods) | Yes | No |
| `config/extractors/*.yaml` | Connection configs incl. `credential_ref` | Yes | No |
| `secrets/secrets.sops.yaml` | Encrypted vault (ciphertext, auto-headered) | Yes | No (ciphertext is safe to commit) |
| `secrets/backups/` | Last 3 encrypted vault copies (auto-pruned) | **No** | No (ciphertext) |
| `secrets/logs/dpl_pass_mgr.log` | Audit log: actions, names, fingerprints | **No** | No (names + one-way hashes only) |
| `~/.dpl-keys/<env>/` | age identity + Fernet key | **No** | **Yes — do not commit!** |

The vault file itself carries a machine-generated warning header ("DO NOT EDIT MANUALLY") that is re-applied on every seal. Obey it: manage secrets only through `./vault.sh`.

---

## 2. Onboarding a New Machine — Full Dependency Chain

Complete this sequence once, **in order**, before doing any extraction work. Each step depends on the ones above it.

### Step 0 — Python environment and DPL_HOME (prerequisite for everything)

All tooling (`vault.sh`, the test suite, the pipeline) runs on the project's Python environment, and expects `DPL_HOME` to point at the project root.

```bash
# 1. Create/activate the project environment (example with conda)
conda create -n dpl python=3.11 pyyaml cryptography -y
conda activate dpl

# 2. Point DPL_HOME at the project root
export DPL_HOME=/Users/<you>/Workspace/Developer/pipeline

# 3. Optional but recommended: add both lines to your shell profile
#    (conda activate dpl; export DPL_HOME=...) so every new terminal is ready.
```

Why this comes first: `vault.sh` hunts for a Python that can `import yaml` — the `dpl` environment satisfies it. Without it, you get the "no Python found with PyYAML" error and nothing downstream works.

### Step 1 — Verify `secrets/` is gitignored

```bash
git check-ignore -v secrets/
```

Expected output: `.gitignore:<line>:secrets/*  secrets/`. If nothing prints, add the rule `secrets/*` to `.gitignore` and commit it before proceeding. This is the one irreversible mistake available in the whole procedure.

### Step 2 — Restore `bin/` (if binaries are missing)

See Section 4 below ("Restoring `bin/`") for the full download+verification ritual. The essential guarantee: **verify before trusting**. A downloaded binary that doesn't match the manifest is discarded.

### Step 3 — Run the certification test suite

```bash
./run_offline_tests.sh
```

Expect green: "GREEN — this machine's tree is certified". This runs the offline test suite with zero network. If this fails, do not proceed until it passes.

### Step 4 — Provision keys

```bash
./setup_secrets.sh laptop
```

Interactive prompts; defaults are the recommended locations. The script mints two keys and prints the two path lines to paste into the next step. It refuses to overwrite existing keys — safe to re-run.

### Step 5 — Create the pass_mgr environment config

This step **precedes the vault** — `scan` reads this config to locate the binary, key, and vault. Create `config/pass_mgr.laptop.yaml`:

```yaml
version: 1
default_method: sops

methods:
  sops:
    binary: bin/sops-v3.13.3.darwin.arm64     # platform-appropriate binary
    manifest: bin/sops_manifest.txt
    age_key_file: ~/.dpl-keys/laptop/sops-age/age.key
    encrypted_file: secrets/secrets.sops.yaml
  fernet:
    key_file: ~/.dpl-keys/laptop/fernet/key
    encrypted_file: secrets/secrets.fernet.bin

secrets:
  connections.ora_db.user_name.password: {}
```

And `config/pass_mgr.prod.yaml` — same shape, with two differences that are **policy, not preference**:

```yaml
version: 1
default_method: sops

methods:
  sops:
    binary: bin/sops-v3.13.3.linux.amd64       # prod runs on RHEL
    manifest: bin/sops_manifest.txt
    age_key_file: ~/.dpl-keys/prod/sops-age/age.key
    encrypted_file: secrets/secrets.sops.yaml
  fernet:
    key_file: ~/.dpl-keys/prod/fernet/key
    encrypted_file: secrets/secrets.fernet.bin

secrets:
  connections.ora_db.user_name.password: {}
```

Rules for these files:

- **The `secrets:` section lists every secret name the pipeline consumes** — mirror your extractor configs' `credential_ref` paths here. The dispatcher validates against this list at init.
- **Prod must never contain a `plain` method** — the config validator rejects it (tested).
- **Key paths use `~/...` (allowed for key files only)** — vault and binary paths must stay relative and inside the tree.
- Commit these files. They contain no secrets.

### Step 6 — Declare credentials in the extractor configs

Each user that needs a password carries a `credential_ref` pointing at its vault path:

```yaml
# config/extractors/oracle_to_stg.yaml
connections:
  ora_db:
    type: oracle
    username: user_name
    host: serv.subd.maind.com
    port: 1523
    service: prd
    folder: etl
    users:
      user_name:
        credential_ref: connections.ora_db.user_name.password
```

Users **without** a `credential_ref` are skipped — you can declare a connection with no managed password. Commenting out a `credential_ref` retires it: the next scan flags the corresponding vault secret as stale and offers deletion.

### Step 7 — Create the vault (first scan)

```bash
./vault.sh scan laptop --dry-run    # ALWAYS dry-run first — preview the plan
./vault.sh scan laptop              # real run: masked prompts, confirm, write
```

The first scan detects no vault, walks you through entering each declared password (masked, confirmed by re-typing), and creates `secrets/secrets.sops.yaml` with the warning header on top. The vault is created *from the configs* — no fragment files, no plaintext files.

### Step 8 — Confirm end-to-end

```bash
./vault.sh list laptop
python demo/demo_get_secret.py
```

---

## 3. Using `vault.sh`

Two commands. Both operate on names and ciphertext only.

### `scan <env> [--dry-run]` — the ONLY way to change secrets

Declarative sync: the extractor configs declare what should exist; the scan reconciles the vault to match.

| What scan finds | What it does |
|---|---|
| Declared in config, **absent** from vault | Prompts for the password (masked, confirmed), adds it |
| In vault, **no longer referenced** | Offers deletion (y/n); deletes only with confirmation |
| Declared **and** present | Offers rotation (y/n); keeps existing password if declined |

Flow guarantees: nothing is written until password entry is complete and you confirm the final summary (`Proceed? (y/N)`). A backup of the current vault is copied to `secrets/backups/` at confirmation, then the new vault is written atomically after round-trip verification. Ctrl-C at any earlier point aborts cleanly — the old vault is untouched.

**Adding a new application** (the "fifth app" scenario): add its connection + `credential_ref` to the extractor config, add its name to the `secrets:` section of `pass_mgr.<env>.yaml`, run `scan laptop --dry-run` then `scan laptop`. You are prompted **only for the new password** — existing secrets pass through as data; you never re-enter them.

**Rotating a password** (the every-6-months case): run `scan laptop`, answer `y` at the rotate prompt for that user, type the new password twice. The audit log records one-way fingerprints of old and new values; if they match, you rotated to the same password and the log shows it.

### `list <env>`

Prints secret *names*, sorted. Never values. Answers "what does this environment's vault contain?" without being an extraction tool.

### The audit log — `secrets/logs/dpl_pass_mgr.log`

Every action is timestamped and recorded: scans, additions, rotations (old and new fingerprints), deletions, backups, writes. The log **structurally cannot contain a secret value** — only names, actions, and 16-character salted-SHA-256 fingerprints. It is safe to keep indefinitely, and it is the record of *which* passwords rotated *when* — keep it out of git (per-machine state) but do not delete it casually.

### Backups — `secrets/backups/`

Before every write, the current vault is copied to `secrets.backups.yaml.<timestamp>`. Only the newest 3 are kept (auto-pruned). Backups are ciphertext — safe to hold, and they are the rollback path if a scan ever produces a wrong result.

### Safety Guarantees

- **No plaintext files, ever** — passwords enter only via masked prompt into memory
- **Atomic replacement after verification** — candidate is decrypted and checked before the old vault is replaced
- **Backup before every write** — last 3 encrypted versions retained
- **Clean aborts** — no write occurs until the final confirmation

---

## 4. Restoring `bin/` on a New Machine

The pinned binaries are the external dependency that `pass_mgr` requires. The manifest is the committed trust anchor. Follow this ritual exactly.

### Table: Pinned Versions (as of this document)

| Tool | Version | Asset Filename (macOS ARM64) | Asset Filename (Linux AMD64) | Asset Filename (Windows AMD64) |
|---|---|---|---|---|
| sops | v3.13.3 | `sops-v3.13.3.darwin.arm64` | `sops-v3.13.3.linux.amd64` | `sops-v3.13.3.windows.amd64.exe` |
| age-keygen | v1.3.2 | `age-keygen-v1.3.2.darwin.arm64` | `age-keygen-v1.3.2.linux.amd64` | `age-keygen-v1.3.2.windows.amd64.exe` |

**Download URLs:**
- sops: `https://github.com/getsops/sops/releases/tag/v3.13.3`
- age-keygen: `https://github.com/FiloSottile/age/releases/tag/v1.3.2`

### Restoration Ritual

1. **Download the correct asset for your platform** — place in `bin/` with the exact upstream filename (do not rename).

2. **Verify checksum against the manifest:**

```bash
shasum -a 256 -c bin/sops_manifest.txt
```

Only the lines matching your platform should say "OK". If any line says "FAILED", **discard the binary, re-download, verify again**. A failed checksum means either network corruption or malicious substitution; treat both as equally fatal.

3. **Run the certification test suite:**

```bash
./run_offline_tests.sh
```

The green script also performs runtime version probes (independent of the staging checksum). If the binary is wrong, the tests fail with typed errors pointing back to this section.

### Version Discipline

- **New releases exist; they are not adopted automatically.** Upgrading means: download a candidate version, compute its SHA-256, update the manifest, commit the change, propagate to all machines.
- **Until then, only the pinned version is sanctioned.** If a test fails complaining about drift, the fix is restoration from the manifest, not disabling the check.
- **The runtime version probe is the second line of defense** — a drifted binary that somehow passed staging still fails at first decryption attempt.

---

## 5. Policy Statements (verbatim)

Read these lines aloud if you need justification. They are project rules, not suggestions.

> **"No human ever authors a plaintext secret file."**  
> Passwords enter the system only through the masked prompt of `./vault.sh scan`. There are no fragment files, no init/add commands, no temporary plaintext a person creates or deletes.

> **"Decrypted passwords are never documented anywhere."**  
> The ops guide describes procedures and names only. Fixture values in tests are synthetic (`DEMO-...` prefix). Real production credentials exist nowhere but inside the encrypted vault and in memory during `get_secret()` calls.

> **"No extraction tool exists, ever."**  
> There is no `extract_secret.sh` or equivalent. The only consumer of plaintext is `get_secret(name)` inside the pipeline process itself. Listing tools (`vault.sh list`) show names only — never values.

> **"The only plaintext on disk is the sops temp file — sub-second, 0600, gitignored."**  
> sops v3.13.3 cannot read plaintext from stdin, so the sealing step writes one short-lived temp file inside `secrets/` (gitignored, permissions 0600), overwrites it with garbage, and unlinks it immediately — all within the same process, lifetime under a second. No person reads or writes this file; it is generated and destroyed by the tool.

---

## 6. Failure-Message Catalog

Each error maps to an operational remedy.

| Error | Cause | Fix |
|---|---|---|
| `VAULT ERROR: no Python found with PyYAML` | Environment not activated | `conda activate dpl` (or `conda install -n dpl pyyaml`) — see Step 0 |
| `VAULT ERROR: no config for environment '<env>'` | Typo in env, or config missing | Check `config/pass_mgr.<env>.yaml` exists — see Step 5 |
| `VAULT ERROR: age identity file missing at ~/.dpl-keys/...` | Keys not provisioned | Run `./setup_secrets.sh <env>`, confirm config path (Step 4) |
| `VAULT ERROR: sops binary missing` | `bin/` not restored | Follow Section 4 ritual |
| `VAULT ERROR: sops exited with status 100` | Bad sops invocation / corrupt vault | If persistent, restore vault from `secrets/backups/` |
| `VAULT ERROR: sops did not respond within 10s` | sops hung | Verify binary integrity (Section 4), retry |
| `DecryptError: Wrong key for this vault` | Vault sealed to a different identity | Verify the correct environment's key is configured |
| `DecryptError: Vault integrity check failed` | Ciphertext modified | Replace vault from `secrets/backups/` |
| `ConfigError: key path ... escapes the home directory` | Tilde path traversing outside `~` | Correct the path in `pass_mgr.<env>.yaml` |
| `ConfigError: Unknown method 'plain' for production` | Prod config has insecure method | Remove it — prod allows sops/fernet only |
| `'list' needs an environment` | Missing argument | `./vault.sh list laptop` |

---

## 7. Common Scenarios

### Scenario A — Adding a New Connection/User

1. Add the connection + `credential_ref` to `config/extractors/<file>.yaml`
2. Add the secret name to `secrets:` in `config/pass_mgr.<env>.yaml`
3. `./vault.sh scan laptop --dry-run` → `./vault.sh scan laptop`
4. Enter only the new password at the prompt; existing secrets are carried over untouched

### Scenario B — Rotating a Password (every 6 months)

```bash
./vault.sh scan laptop        # answer 'y' at the rotate prompt, type new password twice
```

The audit log records `FINGERPRINT: <name>: old=<hash> new=<hash>`. Identical hashes mean the password didn't actually change.

### Scenario C — Retiring a Connection

1. Remove (or comment out) the `credential_ref` — or the whole user/connection — from the extractor config
2. Remove the secret name from `pass_mgr.<env>.yaml`
3. `./vault.sh scan laptop --dry-run` — the secret now appears under STALE
4. `./vault.sh scan laptop` — confirm deletion at the prompt

The vault write is backed up first; the backup retains the last 3 versions if you need to undo.

### Scenario D — Lost Private Key (honest version)

If the age identity for an environment is lost, **the age-sealed vault for that environment is unreadable**. There is no recovery from ciphertext without the key — that is what encryption means. The realistic paths, in order of preference:

1. **Fernet backup vault** — if the Fernet key survives, the same secrets exist in `secrets/secrets.fernet.bin` (when kept in sync); they can be loaded in memory and re-sealed to a newly minted age identity.
2. **Encrypted backups** — `secrets/backups/` holds ciphertext only; these help against vault corruption, **not** against key loss.
3. **Re-collection** — if both keys are gone, the credentials must be re-obtained from their sources (DBA resets the password, etc.) and re-entered via a fresh scan.

Mitigation to decide *before* this happens: store one offline copy of each environment's age identity in a physically secured location (sealed envelope, password manager, safe). The blast radius of a lost key is exactly one environment — which is the containment principle doing its job.

### Scenario E — Migrating a Legacy Plaintext Credential

1. Add the connection + `credential_ref` to the extractor config (Steps 5–6 of onboarding)
2. `./vault.sh scan laptop` — type the existing password at the masked prompt
3. Verify the pipeline connects via `get_secret()`
4. **Delete the plaintext file** (`rm -P secrets/db.ora.sec`) — this deletion is the milestone: the last plaintext credential leaves disk

---

## 8. Roadmap: Fernet Backup Vault

The Fernet method is implemented and tested (provider, envelope format, failure modes — 16 tests), and `setup_secrets.sh` already provisions its key. What remains is operational: keeping `secrets/secrets.fernet.bin` synchronized with the sops vault so it functions as a genuine second decryption path (see Scenario D). Planned work: a `sync-fernet` operation in the vault tooling that reads the sops vault in memory and seals the same tree with the Fernet key — same no-plaintext-file rules, same verification-before-write. Until that lands, treat the Fernet vault as provisioned-but-not-populated.

---

## 9. Change Log (Document History)

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-11-03 | Initial release — onboarding, vault operations, bin/ restoration, policies |
| 1.1 | 2026-11-03 | Fragments removed — scan is the only write path; masked-prompt policy; warning header; audit log with fingerprints; backups to secrets/backups (keep 3); logs to secrets/logs; onboarding reordered (Python/DPL_HOME → gitignore → bin → tests → keys → configs → first scan); config file templates added; lost-key scenario rewritten honestly; Fernet roadmap section |

---

*End of Document.*
