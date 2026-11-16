# Connection Pool & Session Management

**Module:** `src/dpl/lib/session_pool.py` · **Applies to:** Oracle and AD extraction, schema generation
**Introduced:** v0.2.0 (Phase 2, parallel-extraction design v3) · **Status:** LOCKED

---

## 1. What this is

One long-lived database session per **folder**, shared by all of that folder's tables, with automatic health checking and configurable retries. This replaced the v0.1 behavior of opening a fresh connection for every table (and every retry attempt).

**Why it exists:**

| Before (v0.1) | After |
|---|---|
| One connection per attempt | One connection per folder, held open across tables |
| 1 hardcoded retry, 0.5s backoff | Retries/backoff configurable per connection |
| Hardcoded "retryable" substrings | Reviewed error classification: session vs. data |
| Transport drop = table lost | Transport drop = session reset + retry, table survives |

Opening an Oracle or AD session is expensive (authentication, TLS handshake, TNS resolution). With dozens of tables per connection, reusing one session removes that cost from every table after the first.

---

## 2. The FolderSession

`FolderSession` is the heart of the module: a lazily opened, reusable connection wrapper.

**Key behaviors:**

- **Lazy open.** No connection exists until the first `open()` call. A folder queued behind the parallelism cap holds *nothing* — sessions are only created when a table is actually about to run.
- **Reuse.** After `open()`, every subsequent `open()` returns the same live connection instantly.
- **Reset.** `reset()` performs a best-effort close and forgets the reference; the *next* `open()` reconnects. Best-effort on purpose: by the time a reset happens, the transport is usually already dead, and a secondary error while closing a corpse must never mask the original failure.
- **Guaranteed release.** Used as a context manager, the session always releases its connection on exit, even if the table loop explodes mid-run.
- **Counting.** `opens` records how many connects happened — watch it in logs to detect flapping networks.

**Architectural boundary:** `session_pool` never imports drivers. It receives a zero-argument *connect factory* (e.g. `partial(ora_connect.connect, cfg, "ora_db")`) that returns the connector's own context manager. Oracle specifics — including keepalive — are wired through the factory, not known here.

**Keepalive (per-driver policy):** Oracle connections are refreshed in the background via `expire_time` (see `settings: extraction.keepalive_minutes`) — keep it comfortably below the firewall's idle-connection timeout. **AD is deliberately different, by ruling:** no keepalive/expiry at all; an idle AD session dropped by the firewall is simply detected by the lane's health gate, classified session-class, and retried after a session reset. Preventive keepalive for AD was considered and rejected — reconnect-on-drop is simpler and equivalent in outcome. Future connectors (MSSQL) should state their keepalive policy in their connector module explicitly, the same way.

---

## 3. Checkout lanes (`table_concurrency`)

Each folder runs its tables through **lanes** — at present 1 or 2, a hard ceiling enforced by `extraction_config` validation:

- **`table_concurrency: 1`** — a single session serves the folder's tables serially. The default.
- **`table_concurrency: 2`** — two sessions ("lanes"), each with its own `FolderSession`, its own health gate, and its own retry budget.

Tables are pulled from **one shared queue** in config order. A slow table never blocks the next: whichever lane is free takes the next table. Outcomes are always written back by original position, so manifests list tables in config order **regardless of completion order** — manifest order is equivalence-sacred and protected by tests.

---

## 4. Error classification — the operator review table

Every task failure is classified before any retry decision is made. This is the signed-off table; changes to it are a release-review item, never a casual edit.

**SESSION-CLASS** — transport broke. The lane resets its session and retries the *same table*:
- `ORA-03113` end-of-file on communication channel
- `ORA-03114` not connected to ORACLE
- `ORA-03135` connection lost contact
- `ORA-12170` TNS: connect timeout occurred
- `ORA-12541` TNS: no listener
- `ORA-12571` TNS: packet writer failed
- `DPI-1080` oracledb: network connection was lost
- Exception **types**: `ConnectionError` family (reset/refused/aborted), `TimeoutError`, `EOFError`
- Substrings: `Network`, `Timeout`, `Timed out`, `Broken pipe`, `End-of-file`, `ConnectionReset`, `ConnectionRefused`, `LDAPSocketReceiveError`, `LDAPSocketSendError`, `LDAPSocketOpenError`

**DATA-CLASS** — everything else, **including all unknown errors**: SQL/syntax errors, missing tables, permissions, cast errors, schema drift. The table is marked failed immediately, with **no retry**, and its siblings continue.

> Why unknown errors are data-class: an unpredicted error at 2 a.m. must not become a silent retry storm. It fails one table, loudly and immediately — precisely today's semantics.

A failed **connect attempt** is naturally session-class (`ConnectionRefused` family) and enters the same retry loop; connect attempts count as attempts.

Note the deliberate exclusion: `OSError` is *not* session-class — disk-full is a data-class problem, and resetting the network cannot cure a full disk.

---

## 5. Retry semantics (`run_with_retry`)

For each table, a fresh task is invoked on the live session:

1. Attempt the table. Success → return `(result, attempts)`.
2. On a **session-class** failure: reset the session, wait `retry_backoff_seconds`, retry — up to `retries` additional times. If exhaustion hits, the last exception is raised and the table is recorded as failed.
3. On a **data-class** failure: raise immediately, `attempts == 1`.

**Critical contract — fresh-per-call:** the task closure must build a new writer and a new batch generator for every invocation. This is the `write_parquet` retry contract: *a retry is simply a new call*. Retrying a half-drained generator or a closed writer would corrupt output; tests enforce it.

**Defaults:** `retries: 2`, `retry_backoff_seconds: 5`, overridable per connection in the extractor YAML (validated by `extraction_config`, which is the sole enforcement point).

---

## 6. Lane isolation

If a table ultimately fails after its retry budget, the lane records the failure and **keeps serving the queue**. One bad table never starves the rest of its folder — and, one level up, one bad folder never disturbs its siblings (see the *Parallel Extraction* document). On the failure path, `attempts` counts task invocations, flooring at 1 (a failed connect still counts as an attempt).

---

## 7. Who uses the pool

- **`extractors/oracle_to_stg.py` and `ad_to_stg.py`** — per-folder extraction through `run_folder_tables`, with lanes, knobs, and manifests.
- **`lib/generate_schemas.py`** (since v0.3.0) — schema introspection rides the identical mechanism: one session per *connection name* (introspection groups by connection, not folder), `table_concurrency` pinned to 1 since metadata calls gain nothing from parallel lanes. Previously the generator opened one connection per table — 136 logons per run; now it is one per connection.

---

## 8. Reading the logs

Every line carries `run_id=` and lane context, so one table's story can be isolated with grep:

```
[etl/lane0] opening session (open #1)     ← first connect for this lane
[etl 1/1] extracting...                   ← per-table start (config position)
[etl/lane0] session-class failure (attempt 1/3), resetting session and retrying in 5s: ...
[etl] table 'ACCT_OPENING' failed after 3 attempt(s) - continuing queue
```

Diagnostic fingerprints worth knowing:

- **A table that fails within the same second the session opens, with no `[folder N/M] extracting...` line** — the session *open* itself failed (e.g. network unreachable from home). The "1 attempt" is bookkeeping, not a real extraction attempt.
- **Repeated `opening session (open #N)` with rising N** — network flapping; the lane keeps resetting and reconnecting.

---

## 9. Configuration reference

```yaml
# settings (global) — the extractor reads keepalive from here
extraction:
  max_parallel_folders: 4      # see Parallel Extraction doc
  keepalive_minutes: 10       # Oracle expire_time; keep below firewall idle timeout

# extractor config (per connection)
connections:
  ora_app1:
    type: oracle
    folder: etl_app1
    table_concurrency: 2      # lanes: 1 or 2 (hard ceiling)
    retries: 2                # extra attempts after the first
    retry_backoff_seconds: 5  # pause before a retry
```

Absent keys fall back to the defaults above; **present-but-invalid values always fail loudly** at startup, naming the connection, key, and allowed range.

---

## 10. Testing

The module is fully offline-testable: factories inject connectors, sleepers inject the clock, no module-level driver imports. Coverage is 100%, exercised by `tests/unit/test_session_pool.py` and `tests/unit/test_session_folder_worker.py`. Any behavior change to classification, retry order, or outcome shape must keep that suite green — manifest equivalence depends on it.
