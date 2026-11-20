# Connection Scoping — `--connection` Reference

**Scope:** PT-039 — running one connection's tables without editing `config/run.yaml`.
**Audience:** operators of the DPL wrapper (`python -m dpl.wrapper.run`).
**Design locked:** 2026-11-20. Companion to `docs/Pause Tables Operator Guide.md`.

---

## 1. What `--connection` does

`--connection NAME` narrows the run to the tables of **one connection**, identified by its
bare name in the extractor yamls (`ora_db`, `ad_main`, … — never the `pipe::name` form).

Three selection modes exist, and they combine by **intersection**:

| You run | What extracts |
|---|---|
| `--connection ora_db` | all of `ora_db`'s tables — nothing else |
| `--tables ACCT_OPENING,CUST_OPENING` | the named tables, any connection (unchanged behavior) |
| `--connection ora_db --tables ACCT_OPENING` | **intersection** — the named tables *that belong to* `ora_db` |

Two names from two different connections? Drop `--connection` and use `--tables` alone —
name-based selection is connection-agnostic and covers the cross-connection case.

### Validation, up front and loud

- A `--connection` name **not declared on any registered pipe** → **exit 3** before anything
  runs (Tier 1 config error, naming the checked pipes). A typo never becomes a silent
  zero-table night.
- A connection name valid in the registry but **absent from one particular pipe** → that pipe
  contributes nothing, quietly (another pipe owns it).
- **Dry-run ignores `--connection`** (as it ignores `--tables`): it validates configs and
  prints the full plan; scoping applies to real runs.

---

## 2. Interaction with pauses (the full matrix)

The pause rules of PT-014 apply unchanged — `--connection` only *selects*; it never lifts
anything. The four outcomes to know:

| Scenario | Without `--paused-table` | With `--paused-table` |
|---|---|---|
| Named **table** carries a live table-level pause | loud refusal naming the table(s), those excluded; other named tables proceed | **table extracts** (lift applies for this run; pause resumes tomorrow) |
| Named **connection** carries a live folder-level pause | loud refusal naming the connection, **zero jobs** for that pipe | selection proceeds, but folder pauses are **never liftable** — every table reports `paused` in the manifest; nothing extracts; exit 0 |
| Combined `--connection X --tables A,B`, A table-paused | A refused loudly and excluded; B extracts | both extract (A lifted) |
| Combined, connection folder-paused | refusal line(s) fire; **zero jobs** | connection proceeds; all its tables report `paused`; nothing extracts; exit 0 |

Rules worth memorizing:

1. **Refusal empties the selection → zero jobs, exit 0** — never "everything instead" (the
   pinned 2026-11-18 collapse rule; the wrapper logs `extract: 0 folder job(s)`).
2. **`--paused-table` alone = exit 3** (it modifies a selection: `--tables` or `--connection`).
3. **Folder-level pauses are never liftable, anywhere** — with the switch the run becomes a
   quiet `paused` manifest rather than a loud refusal. Removing the tag in config is the only
   real remedy (by design; see the Pause guide §6).
4. Refusals are **loud but non-fatal**: refused items are excluded; the run continues with
   what remains.

---

## 3. Intersection outsides (likely typos)

With both switches given, a named table **not on the named connection** is refused loudly
(`table(s) X are not on connection 'Y' — check the name(s) (typo?) or drop --connection`)
and excluded. If **all** named tables were outsiders, the selection collapses to zero jobs.

| Command (assuming GHOST is not on ora_db) | Result |
|---|---|
| `--connection ora_db --tables ACCT,GHOST` | GHOST refused loudly, excluded; ACCT extracts |
| `--connection ora_db --tables GHOST1,GHOST2` | both refused; collapse → zero jobs, exit 0 |

---

## 4. The ten-scenario table (as verified in session, 2026-11-20)

Assume `ACCT_OPENING`, `CUST_OPENING` on `ora_db`; `ora_db`'s pause state varies by row.

| # | Command | Behavior |
|---|---|---|
| 1 | `--connection ora_db` | all ora_db tables extract; other pipes idle |
| 2 | `--tables ACCT_OPENING` | ACCT_OPENING extracts (connection-agnostic, unchanged) |
| 3 | `--connection ora_db --tables ACCT_OPENING` | clean intersection → ACCT_OPENING extracts |
| 4 | `--connection ora_db` (folder paused) | loud refusal naming ora_db; zero jobs; with switch: quiet `paused` manifest, nothing extracts |
| 5 | `--tables ACCT,CUST` (folder paused) | both refused (governed by folder pause) → zero jobs; with switch: quiet `paused` manifest |
| 6 | both given (folder paused) | table + connection refusal lines fire → zero jobs; with switch: as row 4 |
| 7 | both given (ACCT **table**-paused) | ACCT refused → zero jobs; with switch: **ACCT extracts** (lift applies) |
| 8 | both given, two tables, ACCT table-paused | ACCT refused and excluded; **CUST extracts**; with switch: both extract |
| 9 | both given, GHOST not on ora_db | GHOST refused loudly (typo guard); ACCT extracts; all-outsiders → zero jobs |
| 10 | two tables, two different connections | `--tables` alone covers it — `--connection` is single-connection by design |

---

## 5. Quick reference

| You want | You run |
|---|---|
| Everything on one connection | `python -m dpl.wrapper.run --connection ora_db` |
| One table from one connection | `--connection ora_db --tables ACCT_OPENING` |
| Tables across different connections | `--tables A,B` (no `--connection`) |
| A paused connection's tables, once | `--connection ora_db --paused-table` (table-level pauses lift; folder-level never do) |
| Why did nothing run? | grep the log for `requested without --paused-table` / `are not on connection` — refusals always name themselves |

*Committed with PT-039. File: `docs/Connection Scoping Operator Guide.md`.*
