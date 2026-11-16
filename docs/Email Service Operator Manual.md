# dpl Email Service — Operator Manual

Version 0.2.0 · November 2026

For operators of the dpl data pipeline. Written in plain language: every
command is copy-pasteable, every setting is explained with its rule and
its reason. Nothing here is aspirational — every behavior described is
enforced by the code and pinned by tests.

---

## 1. What the email service is

The email service sends operational mail about pipeline runs: failure
alerts, completion notices, start notices. It is a library inside the
pipeline (`dpl.lib.email_service`), called by the wrapper at three
well-defined moments of every run. It is **not** a general-purpose mail
tool: it sends only the events configured in `config/email_service.yaml`,
to the recipients configured there, and it refuses to send anything a
config author did not explicitly set up.

Three properties define everything else:

1. **The service never raises.** Whatever happens — bad config, dead
   relay, missing template — `send_email()` returns a structured result
   describing the outcome. A broken email stack can never damage a
   data run, and the run's exit code never depends on email (the
   wrapper wires this through a `_notify()` seam that swallows even
   a completely misbehaving email module).
2. **Every send is audited.** Each attempt, success or failure, writes
   one row into the `email_sends` table of the pipeline history DB,
   joined to the run by `run_id`. Silence in the inbox is therefore
   diagnosable from the DB.
3. **Safe by default.** The service ships with `dry_run: true`, meaning
   it composes real messages, writes them as `.eml` files into an
   outbox folder, records history rows — and never touches a network
   socket. Going live is a deliberate, one-line act.

## 2. Module map — who does what

All modules live in `src/dpl/lib/email_service/`:

| Module | Responsibility |
|---|---|
| `api.py` | The single entry point `send_email(caller, data=..., run_id=..., config_path=..., db_path=...)`, re-exported from the package `__init__`. Load-or-send errors become structured results here. |
| `config.py` | Loads and validates `config/email_service.yaml`. Collects *all* errors before failing. Enforces protected callers. |
| `mask.py` | Masks recipient addresses (first four characters and last four survive) and renders templates: `${placeholder}` substitution, reserved stamping, escaping in HTML. |
| `template.py` | Reads `.txt` / `.html` template files, extracts required placeholders. |
| `composer.py` | Builds the MIME message: multipart/alternative when an HTML twin is used, attachments, dry-run outbox writer. |
| `result.py` | The `SendResult` structure: outcome, send_id, message, history row fields. |
| `transport.py` | Opens the socket (SSL / STARTTLS / plain), logs in, delivers, classifies errors, retries transient ones, writes the history row. |
| `history_db.py` glue | Sends write through `lib/history_db.py` — the sole sqlite3 connector of the project. |

Supporting files elsewhere:

- `config/email_service.yaml` — the whole operator surface (server, gates, callers).
- `config/smtp/smtp.yaml` — *not read by the email service*. It is a
  pseudo-connection file consumed only by the vault scanner, so the
  scanner knows a secret named `smtp.password.secret` must exist
  (see §8).
- `config/email_templates/` — one `.txt` (and optionally one `.html`)
  per caller: `selftest`, `run_started`, `run_completed`, `run_failed`.
- `scripts/email_acceptance.py` — offline acceptance harness (§13).
- `tests/integration/test_email_live.py` — the live relay drill (§13).

## 3. How a send flows, end to end

1. The wrapper reaches an event point (`run_started`, `run_completed`,
   or `run_failed`) and calls `_notify(event, run_id, data)`.
2. `_notify` imports the email facade late (a damaged install cannot
   kill the wrapper at import time) and calls
   `send_email(caller=event, run_id=run_id, data=data)`. Any exception
   at all is logged and swallowed — email is never fatal.
3. The facade loads the config. **Config problems never send** and are
   reported as a structured `bad config` result with every violation
   listed; nothing partially-valid is attempted.
4. The caller (event) is looked up. Unknown caller → structured
   `bad caller` result. Caller disabled → `skipped`, with a history row
   recording the skip.
5. Template renders with the caller's static `data:` merged under the
   event's dynamic data; reserved stamps (`generated_at` and friends)
   are stamped automatically and cannot be supplied or collided with.
6. The composer builds the message. With `dry_run: true` it writes a
   `.eml` file into `logs/email_outbox/` and finishes. With
   `dry_run: false` the transport opens a connection, delivers with
   retries, and classifies any failure.
7. One `email_sends` row is written either way — sent, failed, skipped,
   or composed-dry.

## 4. The three events and their tiers

| Event | Tier | When it fires | Default state | Can be disabled? |
|---|---|---|---|---|
| `run_failed` | 1 — always-on | Once, on any non-zero exit after start | enabled | **No.** `enabled: false` on `run_failed` is rejected at config load. |
| `run_completed` | 2 — opt-in | Once, on clean exit (exit 0) | enabled | Yes |
| `run_started` | Tier 3 — dormant-by-default | Once, at run_id creation | enabled | Yes |

The tier system is a **mail budget doctrine**: one caller per event,
commented in the YAML with its tier and budget. Tier 1 mail arrives even
if an operator fat-fingers the config — that protection lives in code
(`PROTECTED_CALLERS` in `config.py`), is proven by the acceptance script,
and pinned by two regression tests. Setting `enabled: false` on
`run_failed` produces a loud load error listing the protected caller by
name; there is no config path to silence failure alerts.

Data keys each event receives (supplied by the wrapper, available as
`${placeholders}` in templates):

- `run_started`: `run_id`, `profile`, `tables`
- `run_completed`: `run_id`, `profile`, `pipes`, `status`
- `run_failed`: `run_id`, `profile`, `status`, `exit_code`, `failed_pipes`

`run_id` is the join key: the same identifier that names the landing/runs
directories and the history rows for that execution.

**Early aborts send no email — by design.** If the run dies before it
starts (config error, exit 3; vault unavailable), no email fires. The
reasons: if the vault is down, email credentials are down too (circular);
and pre-run misconfiguration is a console/stderr problem — someone just
typed a command. The mails protect *runs*, not shells. A restart-check
watchdog (planned) will close the remaining gap with a heartbeat scheme:
regular "still alive" mail where silence is the alarm.

## 5. Config reference — every key, its rule, its reason

Top level (`email:`):

| Key | Rule | Meaning |
|---|---|---|
| `host` | required, string | Relay hostname. Ships as CHANGE-ME. |
| `port` | required, integer 1–65535 | Ships 465. Defaults per mode exist (ssl→465, starttls→587, plain→25) but the key is validated if present. |
| `mode` | one of `ssl` \| `starttls` \| `plain` | `plain` is opt-in and **warns on every send** — the warning is doctrine, not noise: an unencrypted submission is a deliberate, visible decision. |
| `auth.scheme` | `basic` \| `none` | `none`: no username, no refs — anything else on the auth block is rejected. `basic`: needs `password_ref`, plus exactly one of `username` (literal) or `username_ref` (vault) — never both. |
| `auth.username` | string | Public identity. A literal is acceptable *because it is public*: knowing the username gains an attacker nothing without the password. |
| `auth.password_ref` | vault reference | Mandatory under `basic`. The password itself never enters this file — a literal `password:` key is a loud load error. |
| `sender` | required, valid address | The From address. Ships as CHANGE-ME. |
| `allow_html` | bool | Global capability ceiling for HTML mail. Both this AND the caller's `use_html` must be true for HTML; otherwise plain. |
| `allow_bcc` | bool, default false | Capability gate. BCC entries on any caller are rejected while false. |
| `allowed_recipient_domains` | list | `[]` = any domain. Non-empty restricts every To/CC/BCC address to those domains — a typo-protection guard, since `ops@yourdomain.org` should never silently mail `ops@yourdomain.com`. |
| `attachments.allowed_extensions` | list | Whitelist: csv, xlsx, log, txt, parquet. Anything else fails loudly at compose time. |
| `attachments.max_total_mb` | number, default 10 | Total cap across all attachments of one send. Exceeding fails the send (loudly, before the socket opens). |
| `retries` | small integer, default 2 | Retries for *transient* errors only (timeouts, resets, 4xx). Non-transient errors (auth refused, sender refused, 5xx) fail fast — retrying a permanent refusal only wastes time and fills the relay's logs. |
| `timeout_sec` | number, default 30 | Socket timeout per attempt. |
| `dry_run` | **required key — no default** | `true` = outbox mode. The absence of the key is an error precisely so that going live is a deliberate act. |
| `templates_dir` | required | Path to templates. Missing directory fails at load. |
| `outbox_dir` | required | Where dry-run `.eml` files land. |

Per-caller keys (`callers.<name>`): `enabled` (required), `to` (required,
non-empty), `cc`, `bcc` (rejected if `allow_bcc: false`), `template`
(file must exist, or load fails naming the caller), `use_html`, `data`
(static substitutions; `subject` belongs here — single source of truth),
`attachments` (paths, subject to whitelist + cap). Unknown keys at either
level are rejected — typos fail at load, never vanish silently.

## 6. Templates and HTML

Each caller reads `config/email_templates/<template>` — a `.txt` file.
Substitution is `${name}`. The renderer:

- Rejects a template referencing a `${name}` nobody supplied, naming the
  missing key (and collects *all* missing keys, not the first).
- Stamps reserved keys automatically (e.g. `generated_at`, canonical
  UTC-Z). Supplying or colliding with a reserved key is an error.
- Ignores unused supplied keys; accepts only scalar values.

**HTML mail** is a twin-body design: if the caller has
`use_html: true` (and the global `allow_html: true`), the composer looks
for a sibling `.html` file next to the `.txt` template and builds a
multipart/alternative message — one envelope, both bodies, the mail
client picks. Rules:

- The `.txt` twin is **mandatory**. If a caller requests HTML and no
  `.html` twin exists, the send falls back to plain **with a WARNING**
  — a soft fallback, because a missing decoration must never silence a
  failure alert. Both missing = loud compose failure.
- Every substituted *data value* is HTML-escaped mechanically in the
  HTML body; the template's own markup stays raw. This means data from
  the pipeline (table names, statuses, anything) can never smuggle
  markup into a message.
- The fixed banner styles are inline (no external CSS — most clients
  strip it) and the banner text feeds from `${subject}`, making the
  YAML's `data.subject` the single source of the alert's headline.
- Gates: HTML is used only when global `allow_html` AND caller
  `use_html` are both true. An existing twin is ignored otherwise.

## 7. Attachments

Attachments are per-caller lists of file paths in the caller stanza.
They are checked **before** delivery: extension must be whitelisted
(csv, xlsx, log, txt, parquet), file must exist, and the total size
must be under `max_total_mb`. Any violation fails the send loudly — the
operator gets a structured failure with the reason, never a truncated
or attachment-less silent send. `run_failed` currently renders failed
*pipes* in the message body; failed *tables* as an attachment is a
planned v2 feature driven by a reserved seam (§12).

## 8. Credentials and the vault — the asymmetric doctrine

The rule: **the username may be public, the password is sacred.**

- `auth.username` may be a literal in config (it's public identity), or
  `username_ref` (vault), or under `scheme: none` absent entirely — but
  literal + ref together is rejected.
- `auth.password_ref` names the vault slot holding the password. A
  literal `password:` anywhere in the config is a **load error by
  design** — the file must never become a place where a password
  accidentally accumulates.
- The vault slot for the SMTP password is registered for the scanner by
  `config/smtp/smtp.yaml`, a pseudo-connection entry. This file is
  consumed only by `./vault.sh scan` — the email service never reads
  it. **Do not delete or comment out its entry** even when auth is
  `none`: the scanner would stop registering the slot, pruning it from
  the machine-managed secrets mapping, and the future-proof slot
  (for the day the relay demands authentication) would quietly vanish.
  Dormant registration costs nothing.

**Auth modes in practice:**

- Authenticated relay (`basic`): the password enters the vault via the
  masked prompt during `./vault.sh scan <env>`; the scanner discovers
  the need from `config/smtp/smtp.yaml`. At send time the transport
  fetches it by ref through `pass_mgr`. A vault outage is classified as
  internal (not an auth failure) and never crashes the run.
- Unauthenticated internal relay (`none`): no username, no refs, no
  vault involvement at send time. This is the KNIME-verified reality of
  many site relays (submission trusted by network location). With
  `mode: plain` on port 25, every send logs the unencrypted-connection
  WARNING — accepted trade on an internal LAN, kept visible on purpose.

## 9. Dry-run and the outbox

With `dry_run: true` (shipping default):

- Messages are fully composed and written as `.eml` files into
  `logs/email_outbox/`, one per send, named `S-<send_id>.eml`. Any mail
  client (or `cat`) can open them: subject, headers, bodies, all real.
- A history row is still written (outcome `dry_run`/`composed`), so
  the audit trail exists before the first live send.
- No socket is opened. This is the state in which every template tweak,
  recipient change, and config experiment should be rehearsed.

Going live is the deliberate flip: `dry_run: false`, after §10's
checklist. Because the key is required with no default, there is no
state between "never sends" and "clearly live".

## 10. Going-live checklist (deployment time)

1. Confirm InfoSec/deployment approvals. The live relay drill is a
   **deployment-time activity by design** — offline certification cannot
   cover a site-specific relay.
2. Fill the real values in `config/email_service.yaml`:
   - `host`, `port`, `mode` (ssl+465 recommended; starttls+587; plain+25
     only with the WARNING doctrine consciously accepted)
   - `auth.scheme` (+ `username`/`password_ref`, or `none`)
   - `sender`
   - every caller's `to:` recipients
3. If `basic`: run `./vault.sh scan <env>` first and type the password
   at the masked prompt.
4. Keep `dry_run: true` and run one full pipeline execution; open the
   `.eml` files in the outbox and verify rendering (especially HTML
   twins for `run_failed` and `selftest`).
5. Flip `dry_run: false`, run the drill:
   `pytest -m integration` — the drill self-disarms gracefully
   (skips with a pointer) when its premises don't hold (no live config,
   still dry-run), and fails loudly on config faults.
6. Run a real `python -m wrapper.run` and confirm the inbox receives the
   tier mail; verify the `email_sends` rows join to the run via
   `run_id` (queries in §11).
7. The shipped git config stays the **CHANGE-ME starter**: real relay
   hostnames and internal addresses are per-machine edits, deliberately
   never committed — the release bundle and repo history stay free of
   internal infrastructure details.

## 11. History and auditing

Every attempt writes one row to `email_sends` in the history DB
(`data/staging/_history/dpl_history.db` by default). Join to the run by
`run_id`. Interim read-side tool until a viewer exists — the sqlite3
one-liners:

```sh
# Last 20 sends, newest first
sqlite3 -header -column data/staging/_history/dpl_history.db \
  "SELECT send_id, ts, caller, outcome, substr(message,1,60) AS msg
   FROM email_sends ORDER BY ts DESC LIMIT 20;"

# All mail for one run (the join the audit actually needs)
sqlite3 -header -column data/staging/_history/dpl_history.db \
  "SELECT * FROM email_sends WHERE run_id = '<RUN_ID>';"

# Outcome census — what has the service been doing?
sqlite3 -header -column data/staging/_history/dpl_history.db \
  "SELECT outcome, COUNT(*) AS n FROM email_sends GROUP BY outcome;"
```

Column semantics worth knowing: `finished_at` is NULL while a send is in
progress and is stamped (canonical UTC-Z) on every terminal outcome,
including failures — a send stuck with NULL `finished_at` across restarts
is itself a diagnostic. All timestamps everywhere (DB, email bodies, log
asctime) are ISO-8601 UTC with literal `Z`, no fractional seconds;
`scripts/check_timestamps.py` verifies the DB read-only (exit 0 clean /
2 violations found / 3 cannot check) and should be run after any
suspicion of timestamp trouble.

Known row-shape protection: the history writer rejects malformed rows
(exact-key guard), non-UTC timestamps, and duplicate send_ids — auditing
records can't drift or overwrite each other.

## 12. Error handling, retries, and what never happens

The error taxonomy, enforced by tests:

- **Transient** (retry up to `retries`, with the plain-mode WARNING
  logged once per send despite retries): timeouts, connection resets,
  4xx relay responses.
- **Non-transient** (fail fast, no retry): auth refused, sender refused,
  recipients refused, data errors, 5xx refusals. Retrying a permanent
  refusal changes nothing and buries the real signal.
- **Internal** (reported, never crash): vault outage during a send,
  a malformed result, a completely unexpected exception — the facade
  wraps everything so the caller sees a structured result.

What never happens, by construction: the run's exit code changing
because of email; a send attempting delivery with a partially valid
config; a template error producing an empty-but-"successful" mail; a
failure alert being silently disabled; a skipped caller vanishing
without a history row.

## 13. Operating procedures

**Offline acceptance** — after any email-service code or config change,
before committing:

```
python scripts/email_acceptance.py
```

Exercises the service against fixture configs: known-good load, every
fault class (bad port, literal password, protected caller disabled,
missing template, …), dry-run compose, structured-result discipline.
Exit codes: 0 = all checks pass; 2 = acceptance failure (something
regressed); 3 = harness fault (the script itself couldn't run its
scenario). Any fault class removed from the code (say, the protected-
caller guard) surfaces here first.

**Live relay drill** — deployment time only:

```
pytest -m integration
```

The drill (`tests/integration/test_email_live.py`) resolves the password
vault-first via the pass_mgr seam, fails loudly on config faults,
gracefully declines (pytest.skip, with a pointer to the reason) when the
site isn't configured for live sending yet, and verifies the `email_sends`
row lands under a recognizable drill run-id.

**Verifying a send actually happened** — the §11 queries. The DB is
truth; the inbox is a downstream consequence.

## 14. Troubleshooting matrix

| Symptom | Likely cause | Fix |
|---|---|---|
| No mail, no history row | Event never fired (early abort / run never started) | Check wrapper log: exit-3 pre-run faults send no email by design. |
| History row `skipped` | Caller `enabled: false` (or unknown caller name) | Enable the caller / fix the caller name in the call site. |
| History row `bad config` with a list of errors | Config validation failed | Read the listed violations — every one is collected and named; fix all, not just the first. |
| `.eml` in outbox but no real mail | `dry_run: true` | Intentional! Flip to false only per §10. |
| WARNING "plain mode" on every send | `mode: plain` | Expected while plain is used; decide with the relay owners whether STARTTLS is available. |
| Auth failure, no retry | Credentials wrong or scheme mismatch | Verify vault slot via vault list; confirm scheme basic/none matches the relay's reality. |
| Recipients refused, no retry | Domain gate or genuinely bad address | Check `allowed_recipient_domains` and the caller's `to:`; refused recipient lists appear in the result message. |
| HTML expected but mail arrived plain | Gate off (global or caller) or `.html` twin missing (soft fallback + WARNING) | Set both gates true; ensure the twin file exists. |
| Attachment failed loudly | Extension not whitelisted / file missing / over 10 MB | Fix path/extension, or split; the whitelist is a doctrine, not a hint. |

## 15. Testing and the safety net

The offline suite carries 151 email-focused tests across seven files
(api, compose, config, mask/template, transport, timefmt, logger-UTC),
plus run-notify and timestamp-verifier tests — the service cannot ship
with a regression its suite catches. Integration tests (live drill, LDAP,
Oracle) are deselected by default (`pytest` runs `-m 'not integration'`);
the current precondition is **351 passed, 7 deselected**. The receiver's
test kit runs the same suites minus SOPS-binary-dependent ones: GREEN
(FERNET-ONLY), 326 passed, 25 skipped.

Known, accepted imperfections: the package docstring's quick-start
signature lags the real one (the real one is §2's); one deliberate
pyrefly warning guards the never-raises boundary; priority mail headers
(KNIME parity item) are deferred to a future release.

## 16. Roadmap seams already reserved

- **v2 attachment providers** — the `run_failed` stanza carries a
  commented, reserved seam (`attachment_providers: [failed_tables_csv]`).
  Unknown provider names are a loud config error today; v2 will implement
  `failed_tables_csv` and `failed_tables_json`, generating attachments
  at send time from the history DB for the failing run_id.
- **Restart-check watchdog** — heartbeat mail where silence is the
  alarm; closes the early-abort coverage gap for unattended runs.
- **Priority headers** — per-caller urgency (`run_failed` high),
  deferred deliberately until after the live drill stabilizes.

---

*End of manual. Corrections belong in this file's own history: if
behavior and text disagree, the code wins — and the text gets fixed.*
