# dpl/lib/schema_dispatch.py
"""Operator-declared schema ownership and the per-pipe dispatch matrix.

PT-034 doctrine, encoded in one place so no caller re-implements it:

  * run.py stays dumb. It never inspects connection types, generator
    fields, or the schema-ownership tag. It asks this module for a
    Decision and obeys it.
  * The type registry lives in dpl.lib.connectors (CONNECTORS). This
    module CONSUMES that registry; it never extends it.
  * Every connection in every extractor yaml must declare its schema
    ownership explicitly:  schema: auto | manual
      auto   - the operator wants this connection's schema master
               generated from live introspection.
      manual - the operator curates this connection's master CSV by
               hand. A deliberate, legitimate opt-out.
  * Missing or misspelled tags fail LOUDLY and TOGETHER: every offending
    connection is named in one message, so the operator fixes them in
    one pass (no whack-a-mole).
  * The dispatch matrix (connection declaration x registry entry):

      connection says | registry for that type      | result
      ----------------+------------------------------+------------------------------
      auto            | generator module present     | introspect (row 1)
      auto            | generator None / type absent | LOUD FAIL (row 2)
      manual          | generator present            | loud info-skip (row 3)
      manual          | generator None               | loud info-skip (row 4)

    Row 2 fails that pipe only; other pipes continue. This mirrors the
    extraction philosophy: a bad table fails its table loudly while
    other tables run.

Adding a new connector type (e.g. mssql in PT-002) changes NOTHING here:
register the type in CONNECTORS with its generator module, and the matrix
evaluates it automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_SCHEMA_TAGS = ("auto", "manual")


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one pipe against the dispatch matrix.

    Attributes:
        action:    "introspect" (call the type's generator module),
                   "skip" (loud info-skip, operator's choice), or
                   "fail" (loud error for this pipe; others continue).
        message:   Human-readable rationale, always logged by the caller.
        generator: Import path of the generator module when action is
                   "introspect", else None.
    """

    action: str
    message: str
    generator: str | None = None


def missing_schema_tags(connections: dict) -> list[str]:
    """Return the names of connections whose schema tag is absent or invalid.

    Aggregation point for the fail-loud-together rule: the caller reports
    EVERY offending connection in one message before anything runs.

    Args:
        connections: the `connections:` mapping from an extractor yaml
            (name -> config dict). Safe on an empty mapping: returns [].

    Returns:
        Sorted list of offending connection names. Empty means all
        connections declared a valid tag.
    """
    bad = []
    for name, cfg in connections.items():
        tag = (cfg or {}).get("schema")
        if tag not in VALID_SCHEMA_TAGS:
            bad.append(name)
    return sorted(bad)


def require_schema_tags(connections: dict) -> None:
    """Raise ValueError if ANY connection lacks a valid schema tag.

    The message names every offender and shows the fix, so a single
    edit session clears all of them. Called by generate_schemas() on
    its CLI path (run.py's path is gated by Decision beforehand).

    Raises:
        ValueError: with an aggregated, actionable message.
    """
    bad = missing_schema_tags(connections)
    if bad:
        raise ValueError(
            "connections missing required 'schema: auto|manual' tag: "
            f"{', '.join(bad)}. Declare ownership explicitly on every "
            "connection, e.g.  schema: auto  (generated) or  "
            "schema: manual  (curated by hand)."
        )


def evaluate(pipe_name: str, connector: dict | None, connections: dict,
             source_type: str) -> Decision:
    """Evaluate one pipe against the dispatch matrix. The ONLY decision point.

    Args:
        pipe_name:   registered pipe name, for messages only.
        connector:   the pipe's CONNECTORS entry (may be None defensively).
        connections: the pipe yaml's `connections:` mapping.
        source_type: the pipe's single validated connection type (from
                     the Pipe dataclass) - used for error clarity only.

    Returns:
        A Decision. Callers log decision.message at the severity implied
        by decision.action (info for skip, error for fail) and act:
        introspect -> call the generator module; skip/fail -> continue.
    """
    # Gate 0 - operator intent must be explicit and unambiguous.
    bad = missing_schema_tags(connections)
    if bad:
        return Decision(
            action="fail",
            message=(
                f"pipe '{pipe_name}': connections missing required "
                f"'schema: auto|manual' tag: {', '.join(bad)} - declare "
                "ownership explicitly; pipe not processed."
            ),
        )

    manual = sorted(n for n, c in connections.items()
                    if c.get("schema") == "manual")
    auto = sorted(n for n, c in connections.items()
                  if c.get("schema") == "auto")

    # Mixed intent in one pipe is ambiguous - refuse it.
    if manual and auto:
        return Decision(
            action="fail",
            message=(
                f"pipe '{pipe_name}': mixed schema ownership (auto: "
                f"{', '.join(auto)}; manual: {', '.join(manual)}) - one "
                "extractor yaml serves one connector type; split manual "
                "connections into their own pipe or align the tags."
            ),
        )

    generator = (connector or {}).get("generator")

    # Rows 3 & 4 - manual is a legitimate, respected opt-out. Loud info.
    if manual:
        return Decision(
            action="skip",
            message=(
                f"pipe '{pipe_name}': schema=manual for connection(s) "
                f"{', '.join(manual)} - hand-curated master, skipping "
                "schema generation (operator's declared opt-out)."
            ),
        )

    # Row 2 - operator expects generation, registry has no generator.
    if not generator:
        return Decision(
            action="fail",
            message=(
                f"pipe '{pipe_name}': schema=auto but no generator "
                f"registered for connector type '{source_type}' - "
                "master cannot be generated; register a generator in "
                "dpl/lib/connectors.py or declare schema: manual; pipe "
                "not processed, other pipes continue."
            ),
        )

    # Row 1 - clean path.
    return Decision(
        action="introspect",
        message=f"pipe '{pipe_name}': schema=auto, introspecting via {generator}.",
        generator=generator,
    )
