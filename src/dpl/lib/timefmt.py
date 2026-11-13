# dpl/lib/timefmt.py
"""
Canonical time formatting for the dpl pipeline (spec FR-20).

Doctrine: the application writes ONLY UTC, everywhere. This module is
the single owner of that rule:

  - now_utc_z()     -> the only sanctioned way to acquire a timestamp
  - require_utc_z() -> the gate at every persistence boundary

Canonical format (THE one format - no family of variants):

    YYYY-MM-DDTHH:MM:SSZ        e.g. 2026-11-10T14:22:33Z

  - always UTC, always the literal uppercase 'Z' suffix
  - no fractional seconds - one single format, not a near-family of them
  - no offsets (+05:30), no naive/local variants, no lowercase 'z'

Why strict: SQLite stores TEXT, and ISO-8601 UTC sorts chronologically
as text - but only if every row uses this exact format. require_utc_z()
makes drift impossible to persist silently: anything else raises loudly.

Usage:
    from dpl.lib.timefmt import now_utc_z, require_utc_z

    stamp = now_utc_z()                  # '2026-11-10T14:22:33Z'
    require_utc_z(stamp)                 # returns stamp, or raises
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import NoReturn

# Exact-shape check BEFORE calendar validation: strptime alone tolerates
# non-padded variants (e.g. '26-11-10'), which would violate the
# "one single format" doctrine. The regex admits only the exact shape.
_CANONICAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_CANONICAL_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _reject(value: object, reason: str) -> NoReturn:
    """Raise a loud, instructive error for a non-canonical timestamp.

    NoReturn (not None) tells the type checker execution never returns,
    which is what lets it narrow `value` to str after the guard in
    require_utc_z(). Never echoes secrets (timestamps cannot carry
    them) and always states the expected format so the fix is obvious
    from the message.
    """
    raise ValueError(
        f"non-canonical timestamp ({reason}); the dpl canonical format "
        f"is ISO-8601 UTC with 'Z' suffix, no fractions: "
        f"'YYYY-MM-DDTHH:MM:SSZ' (e.g. '2026-11-10T14:22:33Z') "
        f"-- got: {value!r}"
    )


def now_utc_z() -> str:
    """Return the current moment in the canonical dpl format (UTC, 'Z').

    The only sanctioned way to acquire a timestamp anywhere in dpl.
    Cannot produce local time by construction (UTC is pinned).
    """
    return datetime.now(UTC).strftime(_CANONICAL_FMT)


def require_utc_z(value: object) -> str:
    """Validate *value* as canonical UTC-'Z'; return it, or raise loudly.

    Gate for every persistence boundary (e.g. history DB writes, send
    results). Accepts exactly 'YYYY-MM-DDTHH:MM:SSZ' with a valid
    calendar date. Rejects: non-str input, naive strings, UTC offsets,
    fractional seconds, lowercase 'z', and shape-valid but
    calendar-invalid dates (e.g. month 13, February 30).
    """
    if not isinstance(value, str):
        _reject(value, f"expected str, got {type(value).__name__}")
    if not _CANONICAL_RE.fullmatch(value):
        _reject(value, "wrong shape; not 'YYYY-MM-DDTHH:MM:SSZ'")
    try:
        datetime.strptime(value, _CANONICAL_FMT)
    except ValueError as exc:
        _reject(value, f"shape matches but calendar invalid ({exc})")
    return value


__all__ = ["now_utc_z", "require_utc_z"]
