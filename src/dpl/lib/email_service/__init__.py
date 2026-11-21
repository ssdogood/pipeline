# dpl/lib/email_service/__init__.py
"""Email service package (feature: email notification).

Public API (final stage): send_email(caller, data=..., attachments=...)
-> SendResult. Never raises; failure isolation per FR-17.

Stage 4a: config.py - parsing/validating config/email_service.yaml,
collect-all-errors, capability gates, vault-only auth refs.

Stage 4b: mask.py - deterministic recipient masking for logs/history.
          template.py - minimal ${key} renderer, reserved-key stamping
          with the UTC-Z gate, single-pass injection resistance.

Stage 4c: result.py - the structured never-raises SendResult, aligned
          with the history email_sends schema. composer.py - MIME
          assembly via EmailMessage only (BCC envelope-only, subject
          sanitized, attachments gated at compose time), dry-run .eml
          outbox writer.
"""
from dpl.lib.email_service.api import send_email

__func_msg__ = {
    "name": "email service",
    "invocation": "",
    "category": "notifications",
    "purpose": (
        "Email notification with failure isolation:\n"
        "   1. vault-only SMTP credentials, deterministic recipient masking,\n"
        "   2. ${key} templates rendered with injection resistance,\n"
        "   3. templated alert emails per run (never raises on send)"
    ),
}

__all__ = ["send_email"]