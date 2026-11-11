# dpl/lib/email_service/template.py
"""Minimal ${key} template renderer (stage 4b).

The whole v1 template engine, deliberately: find ${key} placeholders
in a template text and substitute scalar values, failing loudly with
ALL faults collected. No loops, no conditionals, no filters - that
is the 'full engine' backlog item, not v1.

House rules implemented here:

  RESERVED KEYS (architecture 'reserved-keys rule'): generated_at,
  send_id and caller are stamped by the service at compose time via
  stamp_reserved(). Caller data using those names is a COLLISION and
  fails loudly with the hint to pick another key - a well-formed but
  false generated_at is worse than an obvious error, so no value
  validation is attempted: ownership, not content, is checked.

  FR-20 GATE: stamp_reserved() runs require_utc_z() on generated_at -
  provenance timestamps enter templates only through the canonical
  UTC-Z door.

  COLLECT-ALL: collisions, non-scalar values and missing keys are
  gathered into ONE TemplateError (house validation style), with
  .collisions / .non_scalars / .missing exposed for programmatic use.

  INJECTION RESISTANCE: substitution is SINGLE-PASS - a data VALUE
  containing '${something}' lands in the output as literal text and is
  never re-expanded. (Header injection is impossible by construction:
  this renderer only ever produces BODY text; the composer sets all
  headers through the email library.)

  Unused data keys are ignored harmlessly (agreed design). Placeholders
  whose names are invalid (${1abc}, ${}) are treated as literal text,
  matching the placeholder grammar in config.py.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from dpl.lib.email_service.config import RESERVED_TEMPLATE_KEYS
from dpl.lib.timefmt import require_utc_z

#: placeholder grammar - MUST stay identical to the one in config.py
#: (config extracts placeholders at load; this substitutes at render).
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_SCALAR_TYPES = (str, int, float, bool)


class TemplateError(Exception):
    """Rendering faults, collected together - never first-fault-only."""

    def __init__(self, collisions: list[str], non_scalars: list[str],
                 missing: list[str]) -> None:
        self.collisions = collisions
        self.non_scalars = non_scalars
        self.missing = missing
        parts: list[str] = []
        if collisions:
            parts.append(
                f"reserved key(s) {collisions} in caller data - reserved "
                f"keys are stamped by the service at compose time; use a "
                f"different name (e.g. data_as_of) for domain values")
        if non_scalars:
            parts.append(
                f"non-scalar value(s) for key(s) {non_scalars} - data "
                f"must be str/int/float/bool")
        if missing:
            parts.append(
                f"missing key(s) {missing} - supply them via the send "
                f"call or the caller config 'data' section")
        super().__init__("template rendering failed: " + "; ".join(parts))


def stamp_reserved(caller: str, send_id: str, generated_at: str) -> dict[str, str]:
    """Build the reserved key set stamped by the service at compose time.

    The ONLY sanctioned way these three values enter a template. The
    UTC-Z gate here means a non-canonical generated_at cannot reach an
    email body, whatever the caller tried to do upstream.
    """
    require_utc_z(generated_at)          # loud FR-20 gate
    return {"generated_at": generated_at, "send_id": send_id,
            "caller": caller}


def render_template(text: str, caller_data: Mapping[str, object],
                    reserved: Mapping[str, str]) -> str:
    """Substitute ${key} placeholders in `text`. Returns rendered body.

    caller_data : the send-call values merged with caller-config data
                  (scalars only; collisions with reserved keys fatal)
    reserved    : the mapping built by stamp_reserved()

    Raises TemplateError listing every collision / non-scalar /
    missing key; raises TypeError if either mapping argument is not a
    Mapping (internal bug - loud is correct at module level; the
    public API boundary converts everything to a SendResult).
    """
    if not isinstance(text, str):
        raise TypeError(
            f"template text must be a string, got {type(text).__name__}")
    if not isinstance(caller_data, Mapping):
        raise TypeError(f"caller_data must be a Mapping, got "
                        f"{type(caller_data).__name__}")
    if not isinstance(reserved, Mapping):
        raise TypeError(f"reserved must be a Mapping, got "
                        f"{type(reserved).__name__}")

    collisions = sorted(
        {k for k in caller_data
         if k in reserved or k in RESERVED_TEMPLATE_KEYS})
    non_scalars = sorted(
        k for k, v in caller_data.items()
        if not isinstance(v, _SCALAR_TYPES))

    merged: dict[str, object] = {**caller_data, **reserved}
    missing: list[str] = []
    seen: set[str] = set()
    for m in PLACEHOLDER_RE.finditer(text):
        key = m.group(1)
        if key not in merged and key not in seen:
            seen.add(key)
            missing.append(key)

    if collisions or non_scalars or missing:
        raise TemplateError(collisions, non_scalars, missing)

    # Single pass: values are never re-scanned, so '${...}' inside a
    # value cannot expand (injection resistance by construction).
    return PLACEHOLDER_RE.sub(
        lambda m: str(merged[m.group(1)]), text)


__all__ = ["PLACEHOLDER_RE", "TemplateError", "render_template",
           "stamp_reserved"]
