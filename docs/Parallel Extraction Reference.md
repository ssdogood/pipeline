# Parallel Extraction

**Modules:** `src/dpl/lib/extraction_config.py` (validation & plan) · `src/dpl/lib/folder_runner.py` (scheduler) · `src/dpl/lib/session_pool.py` (execution — see companion doc)
**Introduced:** v0.2.0 (Phases 1–4, design v3, LOCKED) · **Status:** LOCKED

---

## 1. The two-tier model

Extraction runs on two levels of concurrency:

```
Tier 1 — FOLDERS in parallel      (ThreadPoolExecutor, capped by max_parallel_folders)
   └─ Tier 2 — TABLES within a folder   (1–2 session lanes per folder, checkout-queue model)
```

- **Folders** are the unit of scheduling: one connection per folder, extracted independently. Up to `max_parallel_folders` folders are in flight at once.
- **Within a folder**, tables flow through 1–2 long-lived sessions ("lanes") sharing one queue — described in the companion *Connection Pool & Session Management* document.

Both tiers use plain threads, not async: extraction is I/O-bound (socket waits release the GIL), so a bounded thread pool is the right tool.

**Hard session ceiling:** worst-case simultaneous sessions = `max_parallel_folders × max(table_concurrency)`. The ceiling is approved infrastructure capacity (8 = 4 × 2); `table_concurrency` values above 2 are **rejected at validation**, not merely discouraged, so no configuration can ever exceed it.

---

## 2. Knobs and where they live

| Knob | Lives in | Meaning |
|---|---|---|
| `max_parallel_folders` | `settings.yaml: extraction:` | folders in flight simultaneously |
| `keepalive_minutes` | `settings.yaml: extraction:` | Oracle expire_time refresh (see pool doc) |
| `table_concurrency` | extractor YAML, per connection | lanes per folder: 1 or 2 |
| `retries` | extractor YAML, per connection | retry attempts on session-class failures |
| `retry_backoff_seconds` | extractor YAML, per connection | pause before a retry |
| `serial_group` | extractor YAML, per connection | membership in a never-parallel chain |

**Safe-upgrade rule:** a settings file *without* an `extraction:` block yields `max_parallel_folders=1` — byte-for-byte today's sequential behavior — plus a loud warning to set the block explicitly. Operating defaults live in the shipped settings file, never hidden in code. The connector config schemas accept unknown keys by design, so `extraction_config` is the **sole enforcement point**: absent keys fall back to defaults; present-but-invalid values abort the run, naming the pipe, connection, key, and allowed range (booleans are never accepted as ints).

---

## 3. Serial groups — one physical box, never in parallel

Connections sharing a `serial_group` name are two schemas that live on **one physical server** and must never be hit simultaneously. The scheduler folds them into a single **chain job**: members run strictly one after another, in config order, while the chain as a whole occupies one slot in the parallel pool.

Group names form a **global namespace across all extractor YAML files** — the way an Oracle folder and an MSSQL folder on the same box express their coupling even though they live in different config files.

---

## 4. The run plan and its dry-run

`extraction_config.build_plan()` folds validated connections into the schedulable plan; `format_plan()` renders it as the operator-facing dry-run block. What the plan states, up front:

- every connection with its folder, type, concurrency, retries, backoff, serial group
- which folders are free-standing vs. chained
- the number of schedulable tasks (free folders + chains)
- the **worst-case session count** — the number we can state to infrastructure as a hard ceiling

Duplicate folders across pipes are refused by the plan builder (two connections writing one folder would interleave parquet publishes). The dry-run table's column widths adapt to actual names, so long connection or folder names cannot break alignment.

---

## 5. Failure doctrine

Carried from table level up to folder level — **continue-on-failure everywhere**:

- A **table** failure is recorded, its siblings in the queue continue (pool doc, §6).
- A **folder** that raises is recorded as `("failed", exc)` and **never disturbs its siblings or the run**. Within a chain, one folder dying must not starve its box-mates of their turn: the next step in the chain still runs.
- The scheduler itself never propagates task exceptions; every outcome is recorded. A bad pipe loses *its* night — healthy pipes still run to completion.
- Results are keyed by **folder name**, which the loader guarantees globally unique (1:1 connection:folder), so no key collisions are possible.

The outcome contract of a scheduled job: `{folder: ("ok", task_result) | ("failed", exception)}` — nothing is silently dropped.

---

## 6. Ordering guarantees

- Tables appear in manifests in **config order**, regardless of lane completion order.
- Chains preserve member order **as declared in the config**.
- The scheduler module itself knows nothing about Oracle, LDAP, or drivers — jobs receive plain callables, which is what makes the whole machinery offline-testable.

---

## 7. Summary for operators

If you read nothing else: the nightly run may open at most `max_parallel_folders × table_concurrency` database sessions at once (currently 8). Servers grouped with `serial_group` are never touched in parallel. Any folder or table can fail without taking the run down — failures are visible in logs, manifests, and the history DB, each with attempt counts and error text. Adding parallelism is a settings-file change (`extraction:` block); removing it is deleting the block, which safely degrades to the sequential v0.1 behavior.

Related reading: *Connection Pool & Session Management* (sessions, classification, retries) and the extractor module docstrings in `oracle_to_stg.py` / `ad_to_stg.py`.
