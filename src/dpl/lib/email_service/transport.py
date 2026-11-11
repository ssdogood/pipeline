# dpl/lib/email_service/transport.py
"""SMTP transport with error classification and retry (stage 4d).

The last mile: turns a composed message into an actual SMTP
transaction, classifies failures as transient or non-transient,
retries transients per config (default 2, linear backoff
backoff_sec * attempt), writes the history row through
history_db.record_email_send(), and NEVER raises - every failure
lands in a SendResult.

Error classification map (ORDER MATTERS - see classify_error):
  1. SMTPAuthenticationError          -> auth_failed, no retry
  2. SMTPSenderRefused / SMTPRecipientsRefused / SMTPDataError /
     SMTPHeloError                    -> rejected, no retry (5xx)
  3. SMTPResponseException 4xx        -> transient, retried
  4. any other SMTPResponseException  -> rejected, no retry (5xx)
  5. SMTPConnectError / ConnectionResetError / TimeoutError
                                     -> transient, retried
  6. OSError (ECONNRESET/EPIPE/ETIMEDOUT and, conservatively,
     everything else)               -> transient, retried
  7. anything unrecognized            -> internal_error, no retry

  NOTE: ALL smtplib exceptions derive from OSError (CPython defines
  SMTPException(OSError)). The specific branches MUST precede the
  generic OSError branch or permanent 5xx refusals would be retried
  (the bug test_5xx_response_is_rejected caught in review).

auth_scheme honored at the wire: 'basic' -> credentials resolved via
pass_mgr (the only path - literals are load-time errors) then
smtp.login(); 'none' -> NO vault fetch, NO LOGIN command.

mode: plain emits exactly ONE WARNING per send (pre-loop, ratified),
fixed wording - a retried send never stacks warning lines.

finished_at doctrine (ratified 2026-11-11): stamped now_utc_z() at
EVERY terminal resolution - sent, failed-after-retries, and
pre-transport faults (vault outage, incomplete compose). Together
with created_at it makes retry cost queryable: finished_at minus
created_at on a history row is the total time the send spent.

History row written on EVERY terminal outcome; a DB failure is
logged loud and swallowed - the send outcome stands (failure
isolation, FR-17).

Tests patch smtplib classes; this module stays stdlib-pure.
"""
from __future__ import annotations

import errno
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from dpl.lib.email_service.config import ServerConfig
from dpl.lib.email_service.result import (
    EC_AUTH_FAILED,
    EC_INTERNAL,
    EC_NONE,
    EC_REJECTED,
    EC_TRANSIENT,
    OUTCOME_FAILED,
    OUTCOME_SENT,
    SendResult,
)
from dpl.lib.history_db import record_email_send
from dpl.lib.logger import get_run_logger
from dpl.lib.timefmt import now_utc_z

log = get_run_logger("dpl.email.transport")

PLAIN_WARN = ("sending over an unencrypted connection (mode: plain)")


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------

class TransportFailure(Exception):
    """Carries classification + detail up to the retry loop."""

    def __init__(self, error_class: str, summary: str,
                 transient: bool) -> None:
        super().__init__(summary)
        self.error_class = error_class
        self.summary = summary
        self.transient = transient


def classify_error(exc: Exception) -> TransportFailure:
    """Map any exception to (error_class, transient?). Tested directly.

    Branch order is load-bearing: smtplib exceptions ARE OSErrors, so
    every specific SMTP branch must fire before the generic OSError
    fallback."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return TransportFailure(
            EC_AUTH_FAILED, f"authentication refused: {exc}",
            transient=False)
    if isinstance(exc, (smtplib.SMTPSenderRefused,
                        smtplib.SMTPRecipientsRefused,
                        smtplib.SMTPDataError,
                        smtplib.SMTPHeloError)):
        return TransportFailure(
            EC_REJECTED, f"server rejected: {exc}", transient=False)
    if isinstance(exc, smtplib.SMTPResponseException) and \
            400 <= exc.smtp_code < 500:
        return TransportFailure(
            EC_TRANSIENT, f"temporary server condition ({exc.smtp_code})",
            transient=True)
    if isinstance(exc, smtplib.SMTPResponseException):
        # reached only with a 5xx-class code (subclasses matched above):
        # a permanent refusal - retrying wastes attempts and annoys the
        # relay
        return TransportFailure(
            EC_REJECTED, f"server refused ({exc.smtp_code})",
            transient=False)
    if isinstance(exc, (smtplib.SMTPConnectError,
                        ConnectionResetError,
                        TimeoutError)):
        return TransportFailure(
            EC_TRANSIENT, f"network/connection error: {exc}",
            transient=True)
    if isinstance(exc, OSError):
        if exc.errno in (errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT):
            return TransportFailure(
                EC_TRANSIENT,
                f"network error (errno {exc.errno}): {exc}",
                transient=True)
        return TransportFailure(
            EC_TRANSIENT, f"os-level error: {exc}", transient=True)
    return TransportFailure(
        EC_INTERNAL, f"unexpected transport error: {exc}",
        transient=False)


# --------------------------------------------------------------------------
# credential resolution (vault refs, never literals)
# --------------------------------------------------------------------------

def _credentials(server: ServerConfig) -> tuple[str, str]:
    """Resolve credentials for auth_scheme 'basic' via pass_mgr - the
    ONLY path (literal username:/password: in YAML are load-time
    errors by config validation). For auth_scheme 'none': no vault
    fetch at all - returns ("", ""), no LOGIN command is sent.

    A vault outage is NOT an auth failure (the relay is innocent) and
    NOT transient (retrying in 30s won't fix the vault): classified
    internal_error with a pointed summary."""
    if server.auth_scheme != "basic":
        return "", ""
    u_ref, p_ref = server.username_ref, server.password_ref
    if u_ref is None or p_ref is None:
        # config validation should make this unreachable; loud anyway
        raise TransportFailure(
            EC_INTERNAL,
            "auth_scheme 'basic' but username_ref/password_ref "
            "missing from config", transient=False)
    from dpl.lib import pass_mgr  # local import: keeps cold-start light

    try:
        user = pass_mgr.get_secret(u_ref)
        pw = pass_mgr.get_secret(p_ref)
    except Exception as exc:
        raise TransportFailure(
            EC_INTERNAL,
            f"credential fetch from vault failed: {exc}",
            transient=False) from exc
    return user, pw


# --------------------------------------------------------------------------
# one attempt: connect, (login), send
# --------------------------------------------------------------------------

def _send_once(server: ServerConfig, user: str, pw: str,
               msg: EmailMessage, recipients: tuple[str, ...]) -> None:
    """One full attempt. Raises on failure (classified upstream).
    The plain-mode WARNING lives in deliver_email (once per send),
    NOT here - retries must not stack warning lines."""
    smtp: smtplib.SMTP
    if server.mode == "ssl":
        smtp = smtplib.SMTP_SSL(
            host=server.host, port=server.port,
            timeout=server.timeout_sec,
            context=ssl.create_default_context())
    elif server.mode == "starttls":
        smtp = smtplib.SMTP(host=server.host, port=server.port,
                            timeout=server.timeout_sec)
        smtp.starttls(context=ssl.create_default_context())
    else:                                  # plain - validated at load
        smtp = smtplib.SMTP(host=server.host, port=server.port,
                            timeout=server.timeout_sec)
    try:
        if server.auth_scheme == "basic":
            smtp.login(user, pw)
        smtp.send_message(msg, from_addr=server.sender,
                          to_addrs=list(recipients))
    finally:
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError) as exc:
            # a quit failure after a clean send is noise, but never silent
            log.debug("smtp.quit() ignored after send: %s", exc)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def deliver_email(server: ServerConfig, composed: SendResult,
                  db_path: Path | None = None,
                  backoff_sec: float = 30.0) -> SendResult:
    """Finalize a composed SendResult into a terminal one. Never raises.

    - plain mode: ONE WARNING per send, before the first attempt
      (ratified - retries never repeat it)
    - retries transient failures up to server.retries times
      (linear backoff: backoff_sec * attempt; tests pass 0)
    - auth failures, permanent refusals, and unknown errors fail
      immediately
    - finished_at stamped at every terminal resolution
    - history row written on every terminal outcome through
      history_db.record_email_send(row, db_path=...) honoring the
      resolve_db_path precedence; a DB failure never demotes a send
    """
    if composed.message is None or not composed.envelope_recipients:
        return _finalize(composed, EC_INTERNAL,
                         "deliver_email received an incomplete compose "
                         "result (message or recipients missing)",
                         attempts=0, db_path=db_path)

    try:
        user, pw = _credentials(server)
    except TransportFailure as tf:
        return _finalize(composed, tf.error_class, tf.summary,
                         attempts=0, db_path=db_path)

    if server.mode == "plain":
        log.warning("%s", PLAIN_WARN)      # exactly once per send

    max_attempts = server.retries + 1           # first try + retries
    attempts = 0
    last: TransportFailure | None = None
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            _send_once(server, user, pw, composed.message,
                       composed.envelope_recipients)
            return _write_history(SendResult(
                send_id=composed.send_id, caller=composed.caller,
                created_at=composed.created_at, mode=composed.mode,
                outcome=OUTCOME_SENT, error_class=EC_NONE,
                attempts_used=attempts,
                recipient_count=composed.recipient_count,
                subject=composed.subject, error_summary=None,
                run_id=composed.run_id, message=None,
                envelope_recipients=composed.envelope_recipients,
                finished_at=now_utc_z()),          # terminal: sent
                db_path)
        except Exception as exc:  # noqa: BLE001 - never-raises boundary
            last = classify_error(exc)
            log.warning("send attempt %d/%d failed (caller=%s, "
                        "send_id=%s, class=%s): %s",
                        attempt, max_attempts, composed.caller,
                        composed.send_id, last.error_class, last.summary)
            if not last.transient or attempt == max_attempts:
                break
            time.sleep(backoff_sec * attempt)

    assert last is not None
    return _write_history(SendResult(
        send_id=composed.send_id, caller=composed.caller,
        created_at=composed.created_at, mode=composed.mode,
        outcome=OUTCOME_FAILED, error_class=last.error_class,
        attempts_used=attempts,
        recipient_count=composed.recipient_count,
        subject=composed.subject, error_summary=last.summary,
        run_id=composed.run_id, message=None,
        envelope_recipients=composed.envelope_recipients,
        finished_at=now_utc_z()),              # terminal: failed
        db_path)


# --------------------------------------------------------------------------
# history write (failure-isolated) + pre-transport finalize
# --------------------------------------------------------------------------

def _write_history(result: SendResult,
                   db_path: Path | None) -> SendResult:
    """Record the terminal outcome; never let DB issues corrupt it."""
    try:
        record_email_send(result.to_history_row(), db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - isolation wall, by design
        log.error("HISTORY WRITE FAILED for send_id=%s outcome=%s: %s "
                  "(send outcome stands; history is incomplete)",
                  result.send_id, result.outcome, exc)
    return result


def _finalize(composed: SendResult, error_class: str, summary: str,
              attempts: int, db_path: Path | None) -> SendResult:
    """Terminal failed result for a pre-transport fault; finished_at
    stamped here too (every terminal outcome is a history outcome)."""
    return _write_history(SendResult(
        send_id=composed.send_id, caller=composed.caller,
        created_at=composed.created_at, mode=composed.mode,
        outcome=OUTCOME_FAILED, error_class=error_class,
        attempts_used=attempts,
        recipient_count=composed.recipient_count,
        subject=composed.subject, error_summary=summary,
        run_id=composed.run_id, message=None,
        envelope_recipients=composed.envelope_recipients,
        finished_at=now_utc_z()),              # terminal: pre-transport
        db_path)


__all__ = ["deliver_email", "classify_error", "TransportFailure",
           "PLAIN_WARN"]
