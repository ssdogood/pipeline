# wrapper/profiles.py
"""Profile resolution: wrapper.yaml -> ordered step list. Mapping only.

If a profile ever needs logic an extractor doesn't expose, the logic
goes INTO the extractor, not here (thin-wrapper rule, design note §5).
"""
from __future__ import annotations

from schema import And, Optional, Schema

STEP_NAMES = frozenset(
    ["generate_schemas", "reconcile_pending", "extract",
     "update_history", "housekeeping"])

WRAPPER_SCHEMA = Schema({
    "retention": {
        "landing_days": And(int, lambda n: n > 0),
        "manifest_days": And(int, lambda n: n > 0),
    },
    "run_id": {
        "prefix": And(str, len),
        "ts_format": And(str, len),
    },
    "profiles": {
        str: {                       # profile name -> definition
            "steps": [
                {
                    "name": And(str, lambda s: s in STEP_NAMES),
                    Optional(str): object,   # per-step args (force, ...)
                }
            ],
        },
    },
    Optional(str): object,
})


def load_wrapper_config(path: str) -> dict | None:
    """Load + validate wrapper.yaml. Returns None (logged) on failure."""
    import yaml
    from schema import SchemaError

    from dpl.lib.common import get_run_logger
    log = get_run_logger("dpl.wrapper")
    try:
        with open(path, "r") as fh:
            cfg = yaml.safe_load(fh)
        return WRAPPER_SCHEMA.validate(cfg)
    except FileNotFoundError:
        log.error("wrapper config not found: %s", path)
    except yaml.YAMLError as exc:
        log.error("invalid YAML in %s: %s", path, exc)
    except SchemaError as e:
        log.error("validation error in %s: %s", path, e)
    return None


def resolve_profile(wcfg: dict, profile_name: str) -> list[dict]:
    """Return the step list for a named profile; raise KeyError if absent."""
    profiles = wcfg["profiles"]
    if profile_name not in profiles:
        raise KeyError(
            f"profile '{profile_name}' not in wrapper.yaml "
            f"(available: {sorted(profiles)})")
    return profiles[profile_name]["steps"]
