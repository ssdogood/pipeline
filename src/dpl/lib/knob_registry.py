# dpl/lib/knob_registry.py
"""
The knob registry: the single authority for every legal knob in
settings.yaml.

Born in PT-040 Phase 2 (2026-11-20), replacing the old design rule
"unknown keys are tolerated" - misspelled knobs were silently ignored
and hunts began in the wrong machine. The registry inverts that:
  - every legal knob is declared HERE first (with type + default),
  - anything else in a settings file refuses LOUDLY naming the
    file, section, and key,
  - _templates/ documents the same knobs with comments, and the
    registry/_templates parity test keeps them in lockstep.

A knob is born here, then in _templates/ - never the reverse.

NOT used for per-source config.yaml files (connections/tables) - the
connector registry owns those. This registry owns settings.yaml only.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Registry shape:
#   section -> knob -> {type, required, default, description}
# "type" is a python callable that the loaded yaml value must satisfy;
# validated by build_settings_schema() in common.py into a strict
# Schema (no Optional(str) catch-alls anywhere).
# ---------------------------------------------------------------------------

SETTINGS_KNOBS: dict[str, dict[str, dict[str, Any]]] = {

    # --- vault layer wiring (RETIRED in Phase 4 - lives in the env folder
    #     from then on). Kept as a tombstone section so the loud refusal
    #     can explain WHERE the key went instead of a bare "unknown key".
    #     empty = the section is refused if present; removal planned with
    #     Phase 4's vault rewiring.
    # "pass_mgr": {},     # RETIRED - see RETIRED_SETTINGS_SECTIONS

    "polars": {
        "batch_rows": {
            "type": int, "required": False, "default": 50000,
            "positive": True,
            "description": "rows per fetch/Polars batch in flight",
        },
        "strict_cast": {
            "type": bool, "required": False, "default": True,
            "description": "true: dtype overflow fails the table loudly; "
                           "false: emergency silent-coerce valve (logged)",
        },
    },

    "paths": {
        "landing_root": {
            "type": str, "required": True,
            "nonempty": True,
            "description": "final published data root "
                           "(<landing_root>/<date>/<folder>/<table>/)",
        },
        "scratch_root": {
            "type": str, "required": True,
            "nonempty": True,
            "description": "transient working area (DuckDB spill + staged "
                           "parts) - SAME filesystem as landing_root",
        },
        "runs_root": {
            "type": str, "required": False,
            "nonempty": True,
            "description": "manifest archive root; omitted -> derived as "
                           "<landing_root>/_runs by normalize_settings()",
        },
        "history_db": {
            "type": str, "required": False,
            "nonempty": True,
            "description": "SQLite history DB path; must not sit inside a "
                           "landing date-dir",
        },
    },

    "duckdb": {
        "memory_limit": {
            "type": str, "required": True,
            "nonempty": True,
            "description": "hard cap on DuckDB buffer usage per table "
                           "write, e.g. '10GB'",
        },
        "threads": {
            "type": int, "required": False,
            "positive": True,
            "description": "DuckDB parallelism; omitted -> all cores",
        },
    },

    "writer": {
        "compression": {
            "type": str, "required": False, "default": "zstd",
            "choices": ("zstd", "snappy"),
            "description": "Parquet codec: zstd (size/speed balance) or "
                           "snappy (faster writes, larger files)",
        },
        "compression_level": {
            "type": "zstd_level", "required": False,
            # NOTE: deliberately NO default - matches the retired schema;
            # downstream (write_parquet) applies DuckDB's own default
            # when the key is absent.
            "description": "zstd codec level 1-22 (DuckDB default: 3)",
        },
        "max_file_mb": {
            "type": int, "required": False, "default": 512,
            "positive": True,
            "description": "part-split threshold in MB (target ~90% of "
                           "this size)",
        },
        "keep_scratch_on_success": {
            "type": bool, "required": False, "default": False,
            "description": "retain the temp dir after success (debugging "
                           "only; never true in production)",
        },
    },

    "extraction": {
        "max_parallel_connections": {
            "type": int, "required": False, "default": 1,
            "min": 1,
            "description": "how many connection folders extract "
                           "simultaneously (renamed from "
                           "max_parallel_folders by PT-041)",
        },
        "keepalive_minutes": {
            "type": int, "required": False,
            "min": 1,
            "description": "Oracle expire_time probe interval in minutes; "
                           "below the firewall idle timeout",
        },
    },

    "pause": {
        "stale_days": {
            "type": int, "required": False, "default": 14,
            "min": 1,
            "description": "pause age (days) entering the once-per-night "
                           "stale-pause digest email",
        },
    },
}

# Sections that USED to exist in settings.yaml and moved elsewhere. A
# present tombstone section gets a louder, more specific refusal than a
# plain unknown section: it names the destination.
RETIRED_SETTINGS_SECTIONS = {
    "pass_mgr":
        "the vault layer now reads pass_mgr.yaml from the ACTIVE "
        "ENVIRONMENT FOLDER (see config/environment.yaml) - remove the "
        "section from settings.yaml",
}


def validate_knob_registry() -> None:
    """Self-check: the registry itself must be well-formed.

    Called by tests; a malformed registry is a developer error caught
    before any settings file ever reads it.
    """
    allowed_keys = {"type", "required", "default", "description",
                    "positive", "min", "choices", "nonempty"}
    for section, knobs in SETTINGS_KNOBS.items():
        if not isinstance(section, str) or not section:
            raise ValueError(f"registry: bad section name {section!r}")
        overlap = set(knobs) & set(RETIRED_SETTINGS_SECTIONS)
        if overlap:
            raise ValueError(f"registry: section(s) {overlap} are both "
                             f"live and retired - fix the registry")
        for knob, spec in knobs.items():
            extra = set(spec) - allowed_keys
            if extra:
                raise ValueError(f"registry: {section}.{knob}: unknown "
                                 f"spec key(s) {sorted(extra)}")
            if spec.get("required") and "default" in spec:
                raise ValueError(f"registry: {section}.{knob}: required knob "
                                 "cannot carry a default")
            if not spec.get("required") and "default" not in spec:
                # allowed (e.g. threads: 'omitted -> all cores') but the
                # schema builder must know to treat it as no-default.
                pass
