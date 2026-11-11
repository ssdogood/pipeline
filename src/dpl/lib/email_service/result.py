# dpl/lib/email_service/result.py
"""Structured, never-raises send result (stage 4c).

THE currency of the email service: every public operation returns a
SendResult instead of raising (failure-isolation rule: an email
problem can never perturb the caller's run status). The composer and,
later, the transport fill these; callers read them and act (or ignore).

Field set intentionally mirrors the history_db.email_sends schema
(one module owns the concern - history_db - but the shapes must stay
in lockstep, enforced by tests comparing against
EMAIL_SEND_ROW_KEYS). `message` and `envelope_recipients` are
transport-stage extras: never persisted, never part of the row.

Outcome/error taxonomy: small and explicit so history queries are
machine-filterable (the promoted error_class rationale). Transport
classes (auth_failed, transport_failed, ...) join in stage 4d;
SKIPPED is produced by the public API when a caller is disabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage

from dpl.lib.history_db import EMAIL_SEND_ROW_KEYS

# outcomes -------------------------------------------------------------
OUTCOME_COMPOSED = "composed"        # ready; transport stage will finalize
OUTCOME_SENT = "sent"                # terminal: delivered to the server
OUTCOME_DRY_RUN = "dry_run"          # terminal: composed, logged, .eml
OUTCOME_FAILED = "failed"            # terminal: did not (and will not) send
OUTCOME_SKIPPED = "skipped_disabled"  # terminal: caller toggle is off

# error classes ---------------------------------------------------------
EC_NONE = "none"
EC_BAD_TEMPLATE = "bad_template_data"
EC_BAD_SUBJECT = "bad_subject"
EC_BAD_ATTACHMENT = "bad_attachment"
EC_OUTBOX = "outbox_write_failed"
EC_INTERNAL = "internal_error"
EC_DISABLED = "disabled"
# stage 4d reserves: EC_AUTH, EC_TRANSIENT, EC_REJECTED


@dataclass(frozen=True)
class SendResult:
    """One send attempt's full story. Immutable once created."""
    send_id: str
    caller: str
    created_at: str                 # ISO-8601 UTC with Z (FR-20)
    mode: str                       # "live" | "dry_run"
    outcome: str                    # OUTCOME_* constant
    error_class: str                # EC_* constant
    attempts_used: int
    recipient_count: int
    subject: str                    # "" when composition failed early
    error_summary: str | None
    run_id: str                    # pipeline run context, "-" if none
    message: EmailMessage | None           # present when outcome=COMPOSED
    envelope_recipients: tuple[str, ...]    # To+CC+BCC; BCC never in headers

    def ok(self) -> bool:
        """True when nothing needs attention (sent or dry-run or
        composed-ready). Disabled is NOT ok() - the caller asked for
        a send and did not get one, even though it is by design."""
        return self.outcome in (OUTCOME_SENT, OUTCOME_DRY_RUN,
                                 OUTCOME_COMPOSED)

    def to_history_row(self) -> dict[str, object]:
        """The exact row record_email_send() accepts - nothing more,
        nothing less (transport extras excluded by construction)."""
        return {
            "send_id": self.send_id,
            "caller": self.caller,
            "created_at": self.created_at,
            "mode": self.mode,
            "outcome": self.outcome,
            "error_class": self.error_class,
            "attempts_used": self.attempts_used,
            "recipient_count": self.recipient_count,
            "subject": self.subject,
            "error_summary": self.error_summary,
            "run_id": self.run_id,
        }

    @staticmethod
    def row_keys() -> frozenset[str]:
        """The schema contract this result must satisfy."""
        return frozenset(EMAIL_SEND_ROW_KEYS)


__all__ = [
    "SendResult",
    "OUTCOME_COMPOSED",
    "OUTCOME_SENT",
    "OUTCOME_DRY_RUN",
    "OUTCOME_FAILED",
    "OUTCOME_SKIPPED",
    "EC_NONE",
    "EC_BAD_TEMPLATE",
    "EC_BAD_SUBJECT",
    "EC_BAD_ATTACHMENT",
    "EC_OUTBOX",
    "EC_INTERNAL",
    "EC_DISABLED",
]
