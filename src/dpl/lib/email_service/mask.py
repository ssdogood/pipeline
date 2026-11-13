# dpl/lib/email_service/mask.py
"""Deterministic masking of email addresses (privacy in logs/history).

Decision log Q3 (requirements spec v0.5): addresses appearing in LOG
entries and in history error rows are masked, leaving the first 4 and
last 4 characters visible, with a floor for short addresses. This
module implements that rule in exactly one place - every other module
calls mask_address(), nobody hand-rolls its own masking.

Thresholds (documented rationale, not magic numbers):
  - len >= 12 : first 4 + '***' + last 4   (the standard rule)
  - 8..11     : first 2 + '***' + last 2  (floor: revealing 4 of 8
                                           chars is half the address)
  - len < 8   : '****'                     (fully masked - micro
                                           addresses leak almost
                                           everything if any chars
                                           survive)

SCOPE (agreed 2026-11-10): config load-time DIAGNOSTICS quote real
addresses (they came from the operator's own config file, and masking
them would make fixing the config harder). Everything written by the
send path - log lines, error summaries, history rows - masks.

Deterministic: same input, same output, always - so masked values are
stable across log rotation and grep-able as a unit.
"""
from __future__ import annotations

_KEEP_LONG = 4        # chars kept at each end for len >= 12
_THRESHOLD_LONG = 12
_KEEP_SHORT = 2       # chars kept at each end for 8 <= len < 12
_THRESHOLD_SHORT = 8
_FULL_MASK = "****"


def mask_address(value: str) -> str:
    """Mask an email address (or any short identifier) deterministically.

    mask_address("ops.team@example.org") -> "ops.***.org"
    mask_address("abcdefgh")            -> "ab***gh"
    mask_address("a@b.co")              -> "****"

    Raises TypeError for non-strings - masking a silent None into
    '****' would hide a coding bug; loud is better.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"mask_address expects a string, got {type(value).__name__}")
    n = len(value)
    if n >= _THRESHOLD_LONG:
        return f"{value[:_KEEP_LONG]}***{value[-_KEEP_LONG:]}"
    if n >= _THRESHOLD_SHORT:
        return f"{value[:_KEEP_SHORT]}***{value[-_KEEP_SHORT:]}"
    return _FULL_MASK


__all__ = ["mask_address"]
