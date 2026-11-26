# Schedules & As-Of — Operator Guide

**Scope:** PT-032 (scheduled-day execution profiles) + PT-015 (staging as-of semantics T-0/T-1/T-2).
**Audience:** operators of the DPL wrapper (`python -m dpl.wrapper.run`).
**Companions:** `docs/Pause Tables Operator Guide.md`, `docs/Connection Scoping Operator Guide.md`.
**Status:** branch `pt-032-015-schedules-asof` — pending office validation; not yet merged to main.

---

## 1. The one idea

The nightly job still fires **every day**. What changed is who works that day: each
connection's `schedule:` decides whether it extracts *today*, and each connection's
`as_of:` decides *how far back* its data window reaches. A connection with no schedule
extracts every day — exactly as before.

| Question | Key at connection level | Answer |
| --- | --- | --- |
| Does this connection work **today**? | `schedule:` | yes/no, per calendar day |
| How far back is "today's data"? | `as_of:` | T-0 / T-1 / T-2 |

Both keys live at the **connection level only**. A schedule or as_of placed at table
level is refused loudly at validation — the run does not start.

---

## 2. The `schedule:` grammar

```yaml
# weekdays (three-letter, lower case)
schedule:
  on: [tue, wed, thu]

# calendar days of the month (integers 1-31, and the special token `last`)
schedule:
  on: [15, last]

# BOTH kinds mixed in one list is fine
schedule:
  on: [tue, 15, last]

# or: everything EXCEPT these days
schedule:
  except: [sun, mon]
```

**Rules, all enforced at validation (exit 3, nothing runs):**

- `on:` and `except:` are **mutually exclusive** — having both is refused loudly.
- Weekday tokens: `mon tue wed thu fri sat sun` (lowercase only).
- Month-day tokens: integers `1`–`31`, or the literal word `last` (the last day of the
  month — handles 28/29/30/31 automatically). `15` and `"15"` (quoted) are both
  accepted; autotyping traps were tested deliberately.
- A day token like `31` simply doesn't match in short months — no error, no extraction.
- **Unknown keys are refused forever.** If you write `months:` inside a schedule block
  the run refuses with a message pointing at the reserved future key — nothing silently
  ignored. (`months:` is a *reserved hint*, not a working feature today.)
- **No schedule key at all = due every day** (backward compatible: old yamls behave
  exactly as they did before PT-032).

### What "not scheduled today" looks like

The run happens, the connection is skipped quietly (no email noise, no error), and the
run manifest records it: every table carries status `not_scheduled` (or `paused` if the
table is also paused — **pause wins the status word**), and the folder summary counts
them under `tables_not_scheduled`. History gets the same picture. Skipping is a normal,
recorded outcome — not a failure, not an alert.

### Checking tomorrow's due-set without running anything

`python -m dpl.wrapper.run --dry-run` prints the plan as usual, and when any connection
carries a schedule, an extra block appears:

```bash
schedule (for 2026-11-25):
  due      : ora_db
  not due  : ad_main
  (connections without a schedule are due every day - not listed above)
```

The banner uses the **same decider the extractors use at run time**, so it can never
disagree with what the night actually does. The date shown is the run date.

---

## 3. Off-schedule pulls: `--ignore-schedule`

Some days you need data the schedule didn't order — an audit, a re-pull, a recovery.
`--ignore-schedule` forces extraction of a **named** target despite its schedule:

```bash
python -m dpl.wrapper.run --connection ora_db --ignore-schedule
python -m dpl.wrapper.run --tables ACCT_OPENING,CUST_OPENING --ignore-schedule
```

**Rules:**

- It is **Option B**: always a deliberate, named act. The bare flag without
  `--connection` or `--tables` is **refused** — "force everything" is never an accident
  you want.
- The forced pull is **stamped loudly**: the log warns, and the manifest carries
  `schedule_overridden: true`.
- A connection that was *due today* anyway behaves normally; only genuinely
  off-schedule connections are stamped.
- The override **never silences the missed-day report** (next section) — it repairs
  data, it doesn't repair the record.

---

## 4. Missed days and the recovery flow

**What is a miss:** a day the connection was **scheduled** to extract, on which no
successful run happened (machine off, job dead, run failed).

**Behavior:**

- Every run, each scheduled connection checks its own history: days between its last
  successful extraction and today that were due but ran nowhere are **missed days**.
- Misses are reported in **one email per run** (`schedule_missed` caller) listing every
  affected folder and its missed dates — alongside the manifest and history, which
  record the same facts machine-readably.
- **No automatic catch-up.** The pipeline detects, records, and emails — recovery is
  the operator's decision. Deliberate design: silent re-pulls of old days are how
  audits get lost.
- The look-back is capped at 30 days; older gaps are reported as
  `truncated` with the cap date stated. A first-ever run (no history) reports nothing —
  there is no baseline to miss.
- Pauses are your own act and are **not** counted as misses (the pause system already
  records them).

**Recovery, when the source still holds the data:**

```bash
python -m dpl.wrapper.run --connection <name> --ignore-schedule
```

If the missed days fall **outside** the connection's normal pull window (see §5), also
widen the query window in a *temporary* config copy for the recovery pull, then delete
the temp config. (A tracker-proposed improvement exists for safer temp-config handling —
not part of this release.)

---

## 5. `as_of:` — how far back "today" means

Sources close their books at different hours. Some are final at extraction time
(T-0), some only tomorrow morning (T-1), some two mornings later (T-2). Declare it per
connection:

```yaml
connections:
  ora_db:
    ...
    as_of: T-2        # T-0 (default when key absent) | T-1 | T-2
```

**Semantics:**

- **Anchor date = run date − N.** Mechanical calendar offset, no business-day logic,
  no holiday skipping. If a CoB day doesn't run (holiday), the anchor shifts one day
  later on the next actual run — a known, documented limitation, handled by pausing
  with reason "CoB not run".
- **Coverage (the manifest's coverage fields):**
  - `master` tables (snapshots): coverage *as of* the anchor — `coverage_from: null`,
    `coverage_to: anchor`.
  - `transaction` tables: coverage is the pull window — `coverage_from: anchor −
    (window_days − 1)` through `coverage_to: anchor`.
- **Every table must declare `type: master | transaction`** (PT-015-6, enforced
  universally since B2): masters are full snapshots (`window_days` on a master is
  refused), transactions carry `window_days` (the pull width — NOT the source's
  retention; keep it as tight as the 3-day self-heal allows, or wider if recovery
  pulls need it).
- **Absent `as_of` = T-0 with a loud warning each run** — the old behavior, never a
  silent guess.
- **Date filtering is operator-authored SQL** in the table queries (e.g. for T-2, plan
  `SYSDATE − 5`; T-1 → `SYSDATE − 4`; T-0 → `SYSDATE − 3`). The pipeline computes and
  records anchors and coverage; it does not rewrite your SQL.

### The outage math, in operator language

Suppose the machine is off for two nights. With a **3-day pull window**
(`window_days: 3`), the next successful run re-pulls the last three days of
transactions — the two missed days plus the current one — so a gap of **up to 2 missed
runs self-heals** with no manual action. Longer outages self-heal partially: you
recover the remainder via the §4 flow.

---

## 6. The full decision order per table, per day

1. Is the **connection** scheduled today? No → `not_scheduled` (recorded, skipped).
2. Was `--ignore-schedule` used with a named target? Yes → extract anyway, loud stamp.
3. Is the **table** paused? Yes → `paused` (recorded, skipped). Pause wins the status
   word if both would apply.
4. Otherwise: extract, with the connection's anchor and the table's coverage recorded.

Manifest statuses are additive: old manifests simply lack the newer keys, and history
ingests them with NULLs.

---

## 7. Quick reference

| You want | You do |
| --- | --- |
| Connection extracts only Tue/Wed/Thu | `schedule: on: [tue, wed, thu]` |
| Connection rests Sunday + Monday | `schedule: except: [sun, mon]` |
| Month-end extract | `schedule: on: [last]` (or mix: `on: [15, last]`) |
| See tonight's due-set | `python -m dpl.wrapper.run --dry-run` |
| Force an off-schedule pull | `--connection NAME --ignore-schedule` |
| Recover missed days | same as above; widen query window in a temp config if needed |
| Declare "this source is a day behind" | `as_of: T-1` (connection level) |
| Check a recovery email's facts | manifest `missed_days` + history `folder_runs.missed_days` |

---

## 8. Setup checklist for a new scheduled connection

1. Add `type: master | transaction` to **every table** (required, run refuses otherwise).
2. Add `window_days` to every transaction table (required on transactions, refused on
   masters).
3. Add `as_of:` at the connection if the source lags (default T-0, warned).
4. Add `schedule:` at the connection if it shouldn't run daily (absent = daily).
5. Author the query SQL with the right SYSDATE arithmetic for your as_of.
6. Run `--dry-run` — the schedule banner confirms the due-set before the first night.
7. Ensure `scripts/init_history_db.py` has been run on the machine (missed-day
   detection needs the v2.2 history columns; detection degrades gracefully — never
   blocks extraction — if history is missing).
