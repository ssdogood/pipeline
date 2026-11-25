# dpl/lib/asof.py
"""As-of semantics: lag parsing, anchor arithmetic, coverage statements.

PT-015 (design LOCKED). A connection declares its publishing lag:

    as_of: T-0 | T-1 | T-2      (connection level ONLY)

The system computes the ANCHOR date mechanically and records BOTH the
run date and the anchor in manifest and history:

    anchor = run_date - N        (N = lag in calendar days; T-2 -> 2)

The anchor claims the LATEST business date in the batch - never full
coverage. Coverage is a PER-TABLE statement driven by the table's
declared type (REQ-015-6/7):

    type: master       -> coverage = snapshot as-of <anchor>
                          (point-in-time; 'from' is deliberately None -
                          a snapshot is not a range)
    type: transaction  -> coverage = business dates
                          <anchor-(W-1)> .. <anchor>   (W = window_days)

window_days declares the width the QUERY actually pulls - NOT what the
source retains (retention lives in a yaml comment, informational only).
Declaring a wider window than the query pulls would make the manifest
lie; hence window_days is required whenever type: transaction is
declared, and no default exists.

Outage math (REQ-015-8, documented rule): a W-day window self-heals
outages of up to W-1 consecutive missed scheduled runs. Known CoB-holiday
limitation: the mechanical anchor OVERSTATES freshness by the number of
skipped CoBs (declared pattern vs actual consolidation); the batch's
true business dates remain discoverable from the data itself.

Pure module: stdlib only, no I/O, no execution - mirrors schedule.py.
Born pyright-strict (ratchet convention).
"""

from __future__ import annotations

import datetime as dt

__func_msg__ = {
    "name": "as-of",
    "invocation": "",
    "category": "extraction",
    "purpose": (
        "as-of semantics - lag validation, anchor arithmetic, per-table coverage:\n"
        "   1. validate as_of (T-0|T-1|T-2) and compute anchor = run_date - N,\n"
        "   2. build the manifest coverage statement per table type\n"
        "      (master: snapshot as-of anchor; transaction: anchor-(W-1)..anchor)"
    ),
}

AS_OF_LAGS: dict[str, int] = {"T-0": 0, "T-1": 1, "T-2": 2}
TABLE_TYPES = ("master", "transaction")
COVERAGE_SNAPSHOT = "snapshot-as-of"
COVERAGE_WINDOW = "window"


def validate_as_of(value: object, ctx: str) -> int:
    """Validate one as_of declaration; return its lag in days.

    Only T-0, T-1, T-2 exist in v1 (anything else is a loud refusal -
    a T-3-style lag must arrive as a designed extension, not a silent
    tolerance). Absence is NOT handled here: the extractor layer treats
    it as T-0 WITH a loud warning (REQ-015-3).
    """
    if isinstance(value, str) and value in AS_OF_LAGS:
        return AS_OF_LAGS[value]
    raise ValueError(
        f"{ctx}: 'as_of' must be one of {sorted(AS_OF_LAGS)}, got {value!r}")


def anchor(run_date: dt.date, lag_days: int) -> dt.date:
    """Anchor = run date - lag, calendar arithmetic (leap/year safe)."""
    return run_date - dt.timedelta(days=lag_days)


def validate_table_type(value: object, ctx: str) -> str:
    """Validate a table's type declaration; return it.

    Only master | transaction exist. The taxonomy has no safe default
    (a wrong guess would silently falsify coverage records), so ANY
    other value - including case variants - is refused loudly.
    """
    if isinstance(value, str) and value in TABLE_TYPES:
        return value
    raise ValueError(
        f"{ctx}: table 'type' must be one of {list(TABLE_TYPES)}, "
        f"got {value!r}")


def validate_window_days(value: object, ctx: str) -> int:
    """Validate window_days (required for transaction tables): int >= 1.

    Booleans are NOT ints here (True == 1 would sneak past a naive
    check - same doctrine as extraction_config._int_ge).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{ctx}: 'window_days' must be an integer >= 1, got {value!r}")
    if value < 1:
        raise ValueError(
            f"{ctx}: 'window_days' must be >= 1, got {value!r}")
    return value


def coverage_statement(table_type: str, anchor_date: dt.date,
                       window_days: int) -> dict[str, object]:
    """Build the manifest coverage statement for ONE table.

    master      -> {kind: snapshot-as-of, from: None, to: anchor}
                   ('from' is None BY DESIGN: a snapshot is a point in
                   time, not a range - recorded, not inferred)
    transaction -> {kind: window, from: anchor-(W-1), to: anchor}
                   (W = declared pull width; truthfulness of this claim
                   is the operator's query-authoring duty - REQ-015-9)
    """
    if table_type == "master":
        return {"kind": COVERAGE_SNAPSHOT, "from": None, "to": anchor_date}
    if table_type == "transaction":
        first = anchor_date - dt.timedelta(days=window_days - 1)
        return {"kind": COVERAGE_WINDOW, "from": first, "to": anchor_date}
    raise ValueError(
        f"coverage_statement: unknown table type {table_type!r} - "
        f"validate with validate_table_type first")
