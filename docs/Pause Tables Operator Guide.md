# Pause Tables — Operator Guide

**Scope:** PT-014 — pausing table extractions without touching the schedule.
**Audience:** operators of the DPL wrapper (`python -m dpl.wrapper.run`) and extractors.
**Design locked:** 2026-11-18. Single source of pause truth: `src/dpl/lib/pause.py`.

---

## 1. What "paused" means

A paused table is **skipped by every nightly run** while remaining fully declared in
config. Nothing is deleted, nothing is commented out, and the pause **arms and expires
by itself** from the dates you write. The table returns to normal service the day after
`end:` — or immediately when you remove the tag.

Two levels exist:

| Level | Where declared | Effect |
|---|---|---|
| **Table-level** | a `tables:` entry | pauses that one table |
| **Folder-level** | a `connections:` entry | pauses **every** table on that connection |

**A folder block dominates.** If a connection carries a `pause:` block, every table on
it is governed by that block — any table-level blocks beneath it are moot (still
validated, so a typo in a moot block still fails loudly).

---

## 2. The grammar (Shape A)

Presence of the block **is** the declaration. Absence = active. Three sub-keys are
mandatory:

```yaml
# table-level
  - connection: ora_db
    schema: ETL
    name: ACCT_OPENING
    query: SELECT * FROM ETL.ACCT_OPENING
    pause:
      start: 2026-11-10
      end: 2026-11-30        # blank = indefinite
      reason: year-end close
```

- `start` — `YYYY-MM-DD`. Today or future. **Future starts are allowed and useful:**
  the pause arms itself on the start date ("stageable").
- `end` — `YYYY-MM-DD` or blank. Blank = **indefinite** until you remove the tag.
- `reason` — non-blank free text. Goes into logs, manifests, and history; write it for
  the person who greps six months from now.

Dates may be written bare (`2026-11-10`) or quoted (`"2026-11-10"`) — both are accepted.

### Validation — what is refused, and where

| Problem | Detected | Result |
|---|---|---|
| Malformed block (missing keys, bad dates, blank reason) | yaml load (`pause_block_ok`) and again in `prepare_run` (`validate_pause_declarations`) | **exit 3** — nothing runs; named owner in the message |
| `end` before `start` | same two places | **exit 3** |
| `end` in the past | **not** an error | the tag is simply expired (see §4) |

Typos are always loud, even in moot table blocks under a folder-level pause.

---

## 3. The four states (evaluated on the run's local date)

```
today < start           ARMED    (active tonight; arms itself on start date)
start <= today <= end   PAUSED   (skipped by every nightly run)
end < today             EXPIRED  (behaves ACTIVE again; tag inert)
no pause block          ACTIVE
```

Every run logs the **arithmetic** for every declared pause:

- ARMED: `pause pending: arms in N day(s) (START): REASON`
- PAUSED: `paused - N day(s) elapsed since START, M day(s) left until END: REASON`
  (indefinite: `paused - N day(s) elapsed since START, no end date: REASON`)
- EXPIRED: `WARNING: pause expired N day(s) ago (END) - remove or extend the tag in config: REASON`

---

## 4. Self-healing — the three clock behaviors

1. **Arms itself.** A future start needs no second edit; the pause takes effect on the
   start date.
2. **Expires itself.** The day after `end`, the table extracts again — no complaint,
   no config error, no halt. An expired pause behaves exactly like an active table.
3. **Nags until cleaned.** While EXPIRED, every run logs the day-count WARNING above.
   Delete the tag when the nag appears (that is its purpose).

If a blank-`end` (indefinite) pause sits **past the staleness threshold**
(settings knob `pause.stale_days`, default 14), the nightly run sends one
**digest email** listing every stale pause with its reason and day count. One email per
day, one line per pause — not one email per pause. *(Digest email lands with Commit 2;
the threshold and per-day digest behavior are part of the locked design.)*

---

## 5. Recording — manifests and history

A paused table stays **in the run's task list** (stable `N/M` numbering preserved), but:

- manifest/history status is the **distinct value `paused`** — not `skipped`;
- it additionally records `pause_start`, `pause_end`, `pause_reason`, and
  `days_elapsed`;
- the folder envelope gains a `tables_paused` count, and the summary line reads
  `extracted, skipped, paused, failed`.

So "why didn't X load last night?" is answered from the history DB alone, forever.

---

## 6. The `--paused-table` switch (manual runs only)

`--paused-table` is a **pure permission flag**: it does not select tables — `--tables`
owns selection. It lifts **table-level** pauses only, for that run only.

```bash
# normal manual pull of two tables (fails for ACCT_OPENING while it is paused)
python -m dpl.wrapper.run --tables ACCT_OPENING,CUST_OPENING

# same pull, ACCT_OPENING's pause lifted for THIS run only
python -m dpl.wrapper.run --tables ACCT_OPENING,CUST_OPENING --paused-table
```

Rules, all enforced:

1. **Without the switch:** naming a paused table produces a loud refusal listing
   every named paused table + remedy; those tables are **excluded**; other named
   tables proceed normally.
2. **With the switch:** named paused tables extract (their pause stays in config,
   active again tomorrow).
3. **Refusal can empty the selection** — if every named table is paused and no
   switch is given, **zero tables are scheduled** (never "everything instead";
   fixed 2026-11-18 after the live-run finding).
4. **Switch alone = exit 3** (config error, nothing ran): it modifies `--tables`
   selections only. Same in each extractor's own CLI.
5. **Folder-level pauses are never liftable** from the CLI — the operator edits
   config or waits out the window. Pause also **outranks profile force**.

### Cron never pauses anything accidentally

The nightly cron invokes the wrapper **without** `--paused-table`. Paused tables are
simply excluded from every scheduled run; the switch exists only for deliberate
manual pulls.

---

## 7. Schema generation and pauses

On **scheduled** runs, paused tables are skipped **silently** by the schema generator.
This is deliberate (cite `pause.py`'s loudness doctrine before "fixing" it): the pause
is already announced loudly in the extract logs and recorded in manifest/history — a
third announcement would be noise.

- A **partially** paused pipe: paused tables skipped, active tables introspected
  normally.
- A **fully** paused pipe: the run exits **0**, the master CSV is left unchanged, and
  **no connection is opened at all**. "No complaints."

On **manual** `--paused-table` runs, the generator returns to full normal service for
the lifted tables (fresh introspection, loud discrepancy highlighting) — a manual pull
of a paused table deserves current schema truth.

---

## 8. Worked example

Monthly close: finance needs `ACCT_OPENING` frozen from Nov 10 to Nov 30, but
`CUST_OPENING` and `FUND_TRANSFERS` must keep flowing. And on Nov 21, an auditor asks
for ACCT_OPENING *right now*.

```yaml
  - connection: ora_db
    schema: ETL
    name: ACCT_OPENING
    query: SELECT * FROM ETL.ACCT_OPENING
    pause:
      start: 2026-11-10
      end: 2026-11-30
      reason: year-end close (finance freeze)
```

Every night Nov 10–30, the log shows ACCT_OPENING with its elapsed/left-days line and
status `paused` in manifest and history; the other two tables extract normally. On
Nov 21:

```bash
python -m dpl.wrapper.run --tables ACCT_OPENING --paused-table
```

extracts it once, without touching the pause. On Dec 1 the pause expires silently and
the table resumes nightly extraction; the WARNING nag reminds you to delete the tag.

---

## 9. Quick reference

| You want | You do |
|---|---|
| Pause one table | add `pause:` block with start/end/reason to its `tables:` entry |
| Pause a whole connection | add `pause:` block to the `connections:` entry |
| Stage a pause ahead of time | set `start:` in the future — it arms itself |
| Resume before `end` | remove the block (or raise `end`) |
| Resume after `end` | nothing — it self-expires; then delete the tag when nagged |
| Pull a paused table once | `--tables NAME --paused-table` (table-level only) |
| List all live pauses | check manifest/history `paused` status + reason fields |
| Change stale-indefinite alert age | settings knob `pause.stale_days` (default 14) |

*File to save:* `docs/Pause Tables - Operator Guide.md` — committed with the feature.