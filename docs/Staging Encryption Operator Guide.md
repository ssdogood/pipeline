# Staging Encryption Operator Guide

*Delivered: 2026-11-27*

## What PT-048 changed

Every parquet file written to the staging area is now **encrypted**. An
operating-system administrator who copies a staging file off the server
gets unreadable ciphertext, not customer data. This is always-on — there
is no toggle, and a missing key aborts the run loudly (exit 3) rather
than writing anything in the clear.

**Daily impact on the operator: none.** Keys are created, used, verified,
and retired entirely by the machine.

## Keys — one per connection, per day

- Each connection gets a fresh encryption key for each run day.
- Keys are machine-generated and live in a dedicated encrypted bin:
  `secrets/stg_keys-<environment>.bin`, unlocked by its own master key
  file. They are **never shown** by `vault.sh list`, never prompted for,
  and never touched by `vault.sh scan`. The password vault and the key
  bin are two separate locks — compromising one does not expose the other.
- The bin keeps **five daily backups** (`backups/` beside the bin), taken
  once per day after the nightly housekeeping.
- A key's lifetime equals its folder's lifetime: when the 10-day
  retention removes a day-folder, the same pass deletes that day's key.

## Setup (one time per machine)

Run the existing `setup_secrets.sh` ceremony. It mints **both**
master keys in one pass: the password-vault key and the staging
key-bin key. It refuses to overwrite an existing key file. A machine
whose key-bin master is missing aborts every run at startup — run the
setup script there before the first night.

## Verification — every file proves it can be read back

After each table's files land, the writer re-opens them with the key in
a fresh session and checks row counts plus a checksum over the first
text column. Two new fields appear in manifests:

- `encryption_key` (envelope root): the key that encrypted the folder —
  the downstream reader's ground truth.
- `verification_passed` (per table): `true` / `false`. **Absent** means
  nothing was published (skipped, paused, not scheduled, or zero rows) —
  absence is not a failure.

If verification fails, the bad parts are deleted immediately (staging
holds verified files or nothing) and the table is re-extracted once
through the ordinary retry machinery. A second failure marks the table
failed — email fires, the run continues.

## The staging README — read before touching anything

The first run on any staging area creates a `README.md` at its root. It
exists because of one hazard: **a folder's key dies when the folder
disappears from disk** — including when the folder was merely *moved*.
Moving a day-folder out and back makes its data permanently unreadable;
recovery is a full re-extract from source. The README's rules:

- Do not hand-delete a date-folder unless absolutely necessary.
- Never move data out of staging — copy it instead.
- If a folder is missing by accident: **stop tonight's run
  immediately** — the key survives only until the next run's
  housekeeping completes.

The README is never overwritten by later runs; operator edits persist.

## Performance

Encryption slows only the parquet write/read steps (about 2.2× on those
steps per DuckDB's documentation) — not extraction, which dominates the
night. The first fully encrypted night is measured against the 48.5-minute
baseline before this is called settled.  

## Runbook — recovery scenarios

| Scenario | Action | Evidence |
| --- | --- | --- |
| Key bin corrupted | Restore the most recent good daily backup from `backups/` beside the bin; at worst the current day re-extracts | Loud decryption refusal at run start |
| Key bin lost (all copies) | Re-extract from source for the affected window; recreate the bin via `setup_secrets.sh` | Unreadable window; no key material anywhere |
| A day key compromised | Nothing to rotate — the key dies with its folder at most 10 days out; consider rotating the bin master key file via setup ceremony | Audit log entry naming the key |
| Bin master key file compromised | Regenerate via `setup_secrets.sh` (delete old file first — it refuses overwrite), then restore bin from the newest backup *sealed under the old master is unreadable*: re-extract the window instead | n/a |
| Suspected accidental folder deletion | Stop tonight's run BEFORE housekeeping; the key may still exist | Run log: sweep line naming deleted keys |

