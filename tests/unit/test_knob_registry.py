# tests/unit/test_knob_registry.py
"""PT-040 Phase 2: the knob registry - unknown-key loud refusals.

Complements test_common.py's settings tests (which keep passing
unchanged: same types/ranges/defaults for known knobs). These tests
cover the NEW protection: unknown sections, unknown knobs, retired
sections - each refusing with the file, section and key named.
"""

import pytest
import yaml

from dpl.lib.common import (
    SETTINGS_SCHEMA,
    load_settings,
    validate_unknown_knobs,
)
from dpl.lib.knob_registry import SETTINGS_KNOBS, validate_knob_registry

# ---------------------------------------------------------------- fixtures

def _base_settings() -> dict:
    """The minimal known-good settings payload (matches the old
    test_common.py fixture's shape: mandatory paths + duckdb)."""
    return {
        "paths": {
            "landing_root": "data/staging",
            "scratch_root": "data/scratch",
        },
        "duckdb": {"memory_limit": "16GB"},
    }


def _write_settings(tmp_path, payload: dict) -> str:
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- registry self-check

def test_registry_self_check_passes():
    validate_knob_registry()


def test_registry_covers_every_live_section():
    assert set(SETTINGS_KNOBS) == {"polars", "paths", "duckdb", "writer",
                                   "extraction", "pause"}


def test_registry_defaults_match_old_schema():
    """Defaults must not drift from the retired hand-written schema."""
    polars = SETTINGS_SCHEMA.validate(_base_settings() | {
        "polars": {},
        "writer": {},
        "extraction": {},
        "pause": {},
    })
    assert polars["polars"]["batch_rows"] == 50000
    assert polars["polars"]["strict_cast"] is True
    assert polars["writer"]["compression"] == "zstd"
    # compression_level: deliberately no schema default (DuckDB applies
    # its own when absent) - absence is the faithful old behaviour
    assert "compression_level" not in polars["writer"]
    assert polars["writer"]["max_file_mb"] == 512
    assert polars["writer"]["keep_scratch_on_success"] is False
    assert polars["extraction"]["max_parallel_connections"] == 1
    assert polars["pause"]["stale_days"] == 14


# ---------------------------------------------------------------- known-knob sanity (old protection preserved)

def test_known_payload_still_loads_via_load_settings(tmp_path):
    settings = load_settings(_write_settings(tmp_path, _base_settings()))
    assert settings is not None
    # normalize_settings derived default applied as before
    assert settings["paths"]["runs_root"].endswith("/_runs")


def test_invalid_range_still_refused(tmp_path):
    # in-range values fine
    ok = _base_settings() | {"writer": {"compression_level": 22}}
    assert load_settings(_write_settings(tmp_path, ok)) is not None
    # out-of-range refused
    bad = _base_settings() | {"writer": {"compression_level": 23}}
    assert load_settings(_write_settings(tmp_path, bad)) is None


# ---------------------------------------------------------------- unknown section

def test_unknown_section_refuses_loudly(tmp_path):
    payload = _base_settings() | {"polars_extra": {"batch_rows": 1}}
    with pytest.raises(ValueError) as ei:
        validate_unknown_knobs(payload, "settings.yaml")
    msg = str(ei.value)
    assert "unknown settings section 'polars_extra'" in msg
    assert "knob_registry" in msg
    assert "polars" in msg and "duckdb" in msg          # legal sections named


def test_unknown_knob_in_known_section_refuses_naming_it(tmp_path):
    payload = _base_settings() | {"duckdb": {"memori_limit": "10GB"}}
    with pytest.raises(ValueError) as ei:
        validate_unknown_knobs(payload, "settings.yaml")
    msg = str(ei.value)
    assert "unknown knob 'memori_limit'" in msg
    assert "section 'duckdb'" in msg
    assert "memory_limit" in msg                        # legal knobs named


def test_misspelled_paths_knob_no_longer_silent(tmp_path):
    """THE headline regression: the old schema's catch-all silently
    ignored 'landing_roots'; the registry refuses it by name."""
    payload = {"paths": {
        "landing_roots": "data/staging",       # typo'd
        "scratch_root": "data/scratch",
    }, "duckdb": {"memory_limit": "16GB"}}
    with pytest.raises(ValueError) as ei:
        load_settings(_write_settings(tmp_path, payload))
    msg = str(ei.value)
    assert "unknown knob 'landing_roots'" in msg
    assert "section 'paths'" in msg


# ---------------------------------------------------------------- retired section

def test_retired_pass_mgr_section_explains_destination(tmp_path):
    payload = _base_settings() | {"pass_mgr": {"environment": "laptop"}}
    with pytest.raises(ValueError) as ei:
        load_settings(_write_settings(tmp_path, payload))
    msg = str(ei.value)
    assert "RETIRED section 'pass_mgr'" in msg
    assert "environment folder" in msg.lower()


# ---------------------------------------------------------------- version-skew framing

def test_version_skew_refusal_mentions_registration(tmp_path):
    """A config referencing a knob THIS code version doesn't know =
    code/config version skew - the refusal teaches where knobs are
    born, so a config travelling dev->test->prod names its fix."""
    payload = _base_settings() | {
        "writer": {"keep_scratch_on_succes": True}     # pre-PT-040 style typo
    }
    with pytest.raises(ValueError) as ei:
        load_settings(_write_settings(tmp_path, payload))
    assert "knob_registry" in str(ei.value)
