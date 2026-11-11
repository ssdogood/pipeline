# dpl/lib/email_service/__init__.py
"""Email service package (feature: email notification).

Public API (final stage): send_email(caller, data=..., attachments=...)
-> SendResult. Never raises; failure isolation per FR-17.

Stage 4a: configuration layer (config.py) - parsing and validating
config/email_service.yaml, failing loudly with ALL errors collected.

Stage 4b: mask.py (deterministic recipient masking for logs/history)
and template.py (minimal ${key} renderer with reserved-key stamping,
collect-all TemplateError, single-pass injection resistance).
"""
