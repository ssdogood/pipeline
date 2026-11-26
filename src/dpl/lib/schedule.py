# dpl/lib/schedule.py
"""Scheduled-day execution profiles: grammar, validation, due-today decider.

PT-032 (design LOCKED). A connection MAY declare a `schedule:` block that
decides, per calendar day, whether its tables are extracted:

    schedule:
      on: [tue, wed, thu, fri, sat]     # due ONLY these days
    # or
      schedule:
        except: [sun, mon]             # due every day EXCEPT these

Token grammar (identical for on/except):
  - weekday words: mon tue wed thu fri sat sun (lowercase, 3 letters)
  - day of month:  integers 1..31 (calendar-strict: 31 never matches a
                   30-day month; nothing shifts)
  - `last`:        the last day of the month, whatever its number
A list may MIX kinds; a day is due if ANY token matches.

Validation doctrine (matches extraction_config.py):
  - absent schedule block  -> due EVERY day (safe-upgrade, REQ-032-5)
  - on+except together      -> loud refusal (contradiction)
  - unknown key in block   -> loud refusal FOREVER (future keys such as
                             `months:` must arrive as designed, validated
                             features - never tolerated as typos)
  - bad token / type / empty list -> loud refusal naming the offender
Nothing here executes anything: no connections, no threads, no I/O.
pure stdlib only (datetime/calendar).

Precedence ruling (REQ-032-9): schedule lives at CONNECTION level only;
a table-level `schedule:` is refused by the config validator (see
extraction_config integration in the extractor's prepare-run), never
silently resolved here.

Pause x schedule (design 2.3): orthogonal - pause (temporary, dated)
and schedule (standing pattern) may coexist on one connection; pause
status wins when both would skip (recorded elsewhere; this module only
answers "is today a scheduled day").
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping
from typing import cast

__func_msg__ = {
    "name": "schedule",
    "invocation": "",
    "category": "extraction",
    "purpose": (
        "Scheduled-day execution profiles - grammar validation and due-today decider:\n"
        "   1. parse+validate a connection's schedule block (on/except, tokens),\n"
        "   2. is_due() decides whether a date is a scheduled extraction day"
    ),
}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LAST = "last"
_ALLOWED_KEYS = ("on", "except")
# python: mon=0 ... sun=6 (date.weekday())


def _token_error(ctx: str, token: object) -> ValueError:
    return ValueError(
        f"{ctx}: invalid schedule token {token!r} - allowed: "
        f"weekday words {WEEKDAYS[0]}..{WEEKDAYS[-1]}, integers 1..31, "
        f"'{_LAST}'"
    )


def _validate_tokens(tokens: object, key: str, ctx: str) -> list[str | int]:
    """Validate one token list; return it normalized (digit-strings -> int)."""
    if not isinstance(tokens, list):
        raise ValueError(
            f"{ctx}: schedule '{key}' must be a list of tokens, got {tokens!r}"
        )
    if not tokens:
        raise ValueError(f"{ctx}: schedule '{key}' must not be empty")
    normalized: list[str | int] = []
    tokens_typed: list[object] = cast("list[object]", tokens)
    token: object
    for token in tokens_typed:
        if isinstance(token, bool):
            raise _token_error(ctx, token)
        if isinstance(token, int):
            if not 1 <= token <= 31:
                raise _token_error(ctx, token)
            normalized.append(token)
            continue
        if isinstance(token, str):
            if token == _LAST or token in WEEKDAYS:
                normalized.append(token)
                continue
            if token.isdigit():
                # YAML hands integers through natively, but a quoted
                # "15" or an environment templating stringified digit is
                # still an acceptable day-of-month IF in range - and is
                # NORMALIZED to int here so the decider sees one type.
                value = int(token)
                if not 1 <= value <= 31:
                    raise _token_error(ctx, token)
                normalized.append(value)
                continue
            raise _token_error(ctx, token)
        raise _token_error(ctx, token)
    return normalized


def validate_schedule_block(block: object, ctx: str) -> dict[str, list[str | int]]:
    """Validate one `schedule:` block; return {'on'|'except': tokens}.

    ctx names the pipe+connection (or file) for loud error messages.
    Unknown keys are refused FOREVER: the reserved future key `months:`
    (quarterly/annual cadences) must be introduced as a designed,
    validated feature - never tolerated silently as a typo.
    """
    if not isinstance(block, dict):
        raise ValueError(
            f"{ctx}: 'schedule' must be a mapping with exactly one of "
            f"{list(_ALLOWED_KEYS)}, got {block!r}"
        )

    block_typed: dict[object, object] = cast("dict[object, object]", block)
    unknown: list[str] = sorted(str(k) for k in block_typed if k not in _ALLOWED_KEYS)
    if unknown:
        hint = ""
        if "months" in unknown:
            hint = (
                " ('months' is a reserved future key for monthly "
                "cadences - not yet a feature)"
            )
        raise ValueError(
            f"{ctx}: unknown key(s) in 'schedule': {unknown}{hint} - "
            f"allowed keys: {list(_ALLOWED_KEYS)}"
        )

    if "on" in block and "except" in block:
        raise ValueError(
            f"{ctx}: schedule declares BOTH 'on' and 'except' - that is a "
            f"contradiction, not a combination; declare exactly one"
        )

    for key in _ALLOWED_KEYS:
        if key in block_typed:
            return {key: _validate_tokens(block_typed[key], key, ctx)}

    raise ValueError(f"{ctx}: 'schedule' must declare 'on' or 'except', got {block!r}")


def is_due(block: dict[str, list[str | int]], date: dt.date) -> bool:
    """Decide whether `date` is a scheduled day for ONE validated block.

    The block MUST already be validated (validate_schedule_block) - this
    function never re-validates, mirrors, or forgives.

    Matching rules:
      - weekday token:  date.weekday() position matches the token
      - integer token:  date.day == token (calendar-strict; a 31 never
                        matches a 30-day month - no silent shifting)
      - 'last':         date.day == calendar.monthrange(y, m)[1]
    """
    today_weekday = date.weekday()
    today_dom = date.day
    last_day = calendar.monthrange(date.year, date.month)[1]

    def _matches(token: str | int) -> bool:
        if isinstance(token, str):
            if token == _LAST:
                return today_dom == last_day
            return WEEKDAYS.index(token) == today_weekday
        return token == today_dom

    if "on" in block:
        return any(_matches(t) for t in block["on"])
    return not any(_matches(t) for t in block["except"])


def is_due_connection(conn_cfg: Mapping[str, object], date: dt.date) -> bool:
    """Due decider for one RAW connection dict (validated elsewhere).

    Absent/None schedule block -> True (every day - REQ-032-5).
    Present block -> validated first (loud; config errors must never
    slip through the decider as silent every-day).
    """
    block = conn_cfg.get("schedule")
    if block is None:
        return True
    ctx = f"connection '{conn_cfg.get('folder', '<unknown>')}'"
    return is_due(validate_schedule_block(block, ctx), date)


IGNORE_SCHEDULE_FLAG = "--ignore-schedule"


def validate_ignore_schedule_pairing(
    ignore_schedule: bool,
    only_tables: list[str] | None,
    only_connection: str | None,
    has_connection_flag: bool = False,
) -> str | None:
    """Validate Option-B pairing for --ignore-schedule.

    A bare flag would silently broaden the run to EVERY connection -
    exactly the Sunday-stale-data mistake the flag exists to
    prevent. So the flag REQUIRES a --tables and/or --connection
    selection; bare --ignore-schedule refuses with the corrective
    message. `has_connection_flag` selects the correct hint: the
    wrapper owns --connection, the extractor CLIs do NOT (there the
    target must come via --tables). Returns the error text, or None
    when the pairing is legal.
    """
    if not ignore_schedule:
        return None
    if not (only_tables or only_connection):
        if has_connection_flag:
            return (
                f"{IGNORE_SCHEDULE_FLAG} modifies --tables/--connection "
                "selections only - rerun as: --tables <names> "
                f"{IGNORE_SCHEDULE_FLAG} (or --connection <name> "
                f"{IGNORE_SCHEDULE_FLAG})"
            )
        return (
            f"{IGNORE_SCHEDULE_FLAG} modifies --tables selections only - "
            "rerun as: --tables <names> "
            f"{IGNORE_SCHEDULE_FLAG}"
        )
    return None
