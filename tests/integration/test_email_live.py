"""Office SMTP drill — LIVE integration test for the email service.

Deselected by default (pyproject addopts '-m not integration');
run deliberately in the office, from the project root:

    pytest tests/integration/test_email_live.py -m integration -v

Design decisions (ratified 2026-11-11):
  - CONFIG vs CREDENTIAL separation, by construction:
      * PUBLIC config (sender, recipients, username, host) is NEVER
        patched by this test (option b). Anything missing or still a
        CHANGE-ME placeholder -> FAIL with the exact lines to edit.
        The drill validates the REAL config or nothing.
      * PASSWORD: the vault is tried FIRST (real pass_mgr, real
        fernet store - no prompt if the scan already stored it). Only
        if the vault cannot serve the value does the test prompt via
        getpass (masked), injecting it through the pass_mgr seam.
        The prompted value NEVER touches a config file, never
        persists, and dies with the test process (ratified).
  - GRACEFUL DECLINE: empty input, EOF, or Ctrl+C at the prompt ->
        pytest.skip("declined"), no send attempted.
  - CANNED MESSAGE: the real 'selftest' caller from the real
        config/email_service.yaml; the test never modifies subject,
        data, or recipients.
  - dry_run:true means the feature is intentionally not-live yet -
        that is an ops state, so the test SKIPS with a pointer. But
        dry_run:false with leftover placeholders CLAIMS live while
        unfinished - that is a config fault, and FAILS loudly.

The send is recorded in the REAL history DB under run_id 'SMTP-DRILL'
(the table exists via scripts/init_history_db.py); the test asserts
the row before passing, so the drill proves the full chain: config ->
facade -> composer -> real TLS -> relay -> history.

For the port matrix (465 ssl / 587 starttls / 25 plain): edit mode
and port in config/email_service.yaml between runs; plain mode logs
its unencrypted-connection WARNING per send - watching for it is part
of that leg of the drill. Check the receiving mailbox manually: the
canned subject is '[dpl] email service selftest'.
"""

from __future__ import annotations

import getpass
import logging

import pytest

from dpl.lib import pass_mgr
from dpl.lib.email_service.api import send_email
from dpl.lib.email_service.config import load_email_config
from dpl.lib.history_db import open_db, resolve_db_path

pytestmark = pytest.mark.integration

_LOG = logging.getLogger(__name__)

CALLER = "selftest"
RUN_ID = "SMTP-DRILL"

_HINTS = {
    "auth_failed": "credential problem - the relay refused LOGIN; "
    "rerun and re-enter the password at the prompt",
    "rejected": "relay refused the envelope (5xx) - check sender, "
    "recipient addresses, and relay submission policy",
    "transient_error": "network/TLS problem - check host, port, mode, "
    "connectivity, firewall",
    "internal_error": "fault before the wire (vault/config) - see the "
    "dpl log for the exact cause",
}


def _fail_config(faults: list[str]) -> None:
    pytest.fail(
        "LIVE DRILL ABORTED - public config incomplete. This drill "
        "never patches public config (option b, ratified 2026-11-11): "
        "edit config/email_service.yaml, then rerun:\n  - " + "\n  - ".join(faults)
    )


def _inject_password(monkeypatch: pytest.MonkeyPatch, login_label: str) -> None:
    """Vault-first password resolution with a masked-prompt fallback.

    The real pass_mgr.get_secret is tried first - if the office scan
    already stored the password, no prompt appears at all. Only on
    vault failure does getpass prompt; the value lives in memory
    inside this test only. Decline (empty/EOF/Ctrl+C) -> skip."""
    real_get_secret = pass_mgr.get_secret
    prompted: dict[str, str] = {}

    def vault_or_prompt(ref: str) -> str:
        try:
            return real_get_secret(ref)
        except Exception as exc:  # noqa: BLE001 - any vault
            _LOG.debug(
                "vault shortfall for %r (%s) - prompting", ref, exc
            )  # shortfall -> prompt path
        if ref in prompted:
            return prompted[ref]
        try:
            value = getpass.getpass(
                f"SMTP password for {login_label} (vault has none for {ref!r}): "
            )
        except (EOFError, KeyboardInterrupt):
            pytest.skip("declined at the password prompt - no live send performed")
        if not value:
            pytest.skip("declined (empty input) - no live send performed")
        prompted[ref] = value
        return value

    monkeypatch.setattr(pass_mgr, "get_secret", vault_or_prompt)


def test_live_smtp_drill(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1 - the real config must load at all ------------------------------
    try:
        cfg = load_email_config()
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        pytest.fail(f"config/email_service.yaml does not load: {exc}")

    srv = cfg.server

    # 2 - dry_run:true is an intentional not-live state -> skip ---------
    if srv.dry_run:
        pytest.skip(
            "email.dry_run is TRUE - set dry_run: false in "
            "config/email_service.yaml for the live drill"
        )

    # 3 - public-config completeness -> FAIL with exact edits ----------
    faults: list[str] = []
    if "change-me" in srv.host.lower() or not srv.host:
        faults.append(f"email.host is still a placeholder: {srv.host!r}")
    if srv.auth_scheme == "basic":
        user = srv.username or ""
        if "change-me" in user.lower() or not user:
            faults.append("email.auth.username is still a placeholder")
        if srv.password_ref is None:
            faults.append(
                "email.auth.password_ref is missing (vault reference required)"
            )
    if "change-me" in srv.sender.lower() or not srv.sender:
        faults.append(f"email.sender is still a placeholder: {srv.sender!r}")
    c = cfg.callers.get(CALLER)
    if c is None or not c.enabled:
        faults.append(f"caller {CALLER!r} is missing or disabled")
    elif not c.to:
        faults.append(f"caller {CALLER!r} has no To: recipients")
    if faults:
        _fail_config(faults)

    # 4 - password: vault first, masked prompt as fallback --------------
    if srv.auth_scheme == "basic" and srv.password_ref is not None:
        login = srv.username or (srv.username_ref or "?")
        _inject_password(monkeypatch, f"{login}@{srv.host}")
    # 5 - THE SEND: real relay, real TLS, real history DB ---------------
    print(
        f"\n[LIVE DRILL] mode={srv.mode} port={srv.port} "
        f"host={srv.host} sender={srv.sender} -> "
        f"{len(c.to) if c else 0} recipient(s)"
    )
    r = send_email(caller=CALLER, run_id=RUN_ID)

    if r.outcome != "sent":
        pytest.fail(
            f"LIVE SEND FAILED: outcome={r.outcome} "
            f"class={r.error_class} attempts={r.attempts_used}\n"
            f"  summary: {r.error_summary}\n"
            f"  hint: {_HINTS.get(r.error_class, 'see the dpl log')}"
        )
    assert r.error_class == "none"
    assert r.attempts_used >= 1

    # 6 - the real history row must exist (audit chain proven) ----------
    conn = open_db(resolve_db_path())
    try:
        row = conn.execute(
            "SELECT outcome, error_class, finished_at FROM email_sends "
            "WHERE send_id = ?",
            (r.send_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "no history row for a SUCCESSFUL live send - was "
        "scripts/init_history_db.py run against this DB?"
    )
    assert row[0] == "sent" and row[1] == "none" and row[2] is not None

    print(
        f"[LIVE DRILL] SENT: send_id={r.send_id} attempts="
        f"{r.attempts_used} finished_at={r.finished_at}"
    )
    print(
        "[LIVE DRILL] history row verified. Now check the receiving "
        "mailbox for subject '[dpl] email service selftest'."
    )
