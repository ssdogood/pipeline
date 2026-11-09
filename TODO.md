## Post-v0.1.1 roadmap (2026-11-09)

Priority order set at close of v0.1.1 session. Design-first; no coding before architecture is agreed.

### 1. Daily email reporting (CRITICAL)
dpl must email extraction results daily, once all applications have run.
Design questions to answer before coding:
- Where is "all applications completed" decided (wrapper run exit path? new aggregation step?)
- Email content per application: status, row counts, attachments?
- SMTP credentials + connection settings: stored via pass_mgr/vault? (extends existing secret-handling patterns to SMTP)
- Failure semantics: email system down must not fail a successful pipeline run, and vice versa
- Module ownership: new email_report module per "one module owns the concern" rule?

### 2. Functional enhancements
List to be triaged (ruff-method: board everything, cluster, decide, one verified commit at a time).

### 3. Key-path policy redesign (carried over)
Accept key files OUTSIDE project tree, reject INSIDE (inverts current rule). 
Six design questions open:
    - scope? 
    - relative paths? 
    - symlinks? 
    - prod parity? 
    - test rewrites for old home-containment rule? 
vault_ops expanduser must agree with validator). setup_secrets.sh absolute-path bug (known issue, published in v0.1.1 notes) waits on this outcome.

## Issues
* Move oracle_dtype_to_comma_free from lib/generate_schemas.py to lib/ora_connect.py — Oracle driver/type knowledge must live in one place (connector layer).
* setup_secrets.sh absolute-path bug — the script writes expanded absolute key paths; resolve_relative_path rejects them (PassMgrConfigError). Known workaround (tilde form); proper fix is the script emitting tilde paths, coordinated with the policy redesign.
* Key-path policy redesign — accept key files outside the project, reject those inside (inverts current rule). The six design questions remain open, plus: rewriting the tests that encode the old home-containment rule, and making vault_ops.py ~line 133's expanduser() agree with the validator.
* Release Procedure doc fixes (three proven incidents from tonight): §6.1 "never copy a stale dist/ receipt"; §6.3 explicit asset names instead of dist/*; §6.2 lessons — angle brackets in notes get eaten by the Markdown renderer (alongside the v0.1.0 zsh-glob lesson). Optionally: have build_release.py sweep foreign files out of dist/ at build start, so the tool enforces what the doc preaches.
* Security Demo Runbook — its embedded demo code is stale after the import fixes.