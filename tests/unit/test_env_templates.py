# tests/unit/test_env_templates.py
"""PT-040 Phase 3: template-folder tests.

Two committed folders, two jobs, permanently enforced:
  - config/_templates/ : blank skeleton - EVERY registry knob documented
    (with placeholders), nothing real. Structure-truth.
  - config/_example/   : minimal runnable defaults. Behaviour-truth.

Parity tests make drift a SUITE FAILURE: a knob added to the registry
without documenting it here (or vice versa) fails CI. The template
folders are self-enforcing documentation - they can never go stale.
"""

import copy
from pathlib import Path

import yaml

from dpl.lib.common import load_settings
from dpl.lib.knob_registry import SETTINGS_KNOBS
from dpl.lib.paths import get_project_root

CONFIG = get_project_root() / "config"


# ---------------------------------------------------------------- helpers

def _load(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _commented_knobs(text: str) -> set[str]:
    """Knob names appearing ONLY as commented-out documentation."""
    found = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
            # 'knob: ...' or '# knob: ...' after further comment marks
            if ":" in stripped:
                found.add(stripped.split(":", 1)[0].strip())
    return found


def _active_knobs(settings: dict) -> dict[str, set[str]]:
    """section -> set of knob names actively present (uncommented)."""
    return {sec: set(vals or {}) for sec, vals in settings.items()
            if isinstance(vals, dict)}


# ---------------------------------------------------------------- _templates/settings.yaml parity

def test_templates_settings_documents_every_registry_knob():
    text = (CONFIG / "_templates" / "settings.yaml").read_text(encoding="utf-8")
    settings = _load(CONFIG / "_templates" / "settings.yaml")
    active = _active_knobs(settings)
    commented = _commented_knobs(text)

    missing = []
    for section, knobs in SETTINGS_KNOBS.items():
        present_here = active.get(section, set()) | commented
        for knob in knobs:
            if knob not in present_here:
                missing.append(f"{section}.{knob}")
    assert not missing, (
        "registry knob(s) undocumented in _templates/settings.yaml: "
        f"{missing} - a knob is born in knob_registry.py THEN documented "
        "here; the template is the operator's map of what exists"
    )


def test_templates_settings_has_no_unknown_knobs():
    settings = _load(CONFIG / "_templates" / "settings.yaml")
    with_labels: dict[str, set[str]] = {}
    for section, knobs in settings.items():
        with_labels[section] = set(knobs or {})
    unknown_sections = set(with_labels) - set(SETTINGS_KNOBS)
    assert not unknown_sections, (
        f"_templates/settings.yaml documents section(s) unknown to the "
        f"registry: {sorted(unknown_sections)} - registry first, "
        "template second"
    )
    for section, knobs in with_labels.items():
        unknown = knobs - set(SETTINGS_KNOBS.get(section, {}))
        assert not unknown, (
            f"_templates/settings.yaml documents unknown knob(s) "
            f"{sorted(unknown)} in section '{section}'"
        )


def test_templates_settings_mandatory_knobs_marked_present():
    """Required knobs must be ACTIVELY present (not only commented) - the
    skeleton must validate structurally if placeholders were replaced."""
    settings = _load(CONFIG / "_templates" / "settings.yaml")
    for section, knobs in SETTINGS_KNOBS.items():
        for knob, spec in knobs.items():
            if spec.get("required"):
                present = knob in (settings.get(section) or {})
                assert present, (
                    f"required knob {section}.{knob} missing from "
                    "_templates/settings.yaml"
                )


def test_templates_settings_placeholder_marker_is_absent():
    """Uncommented REPLACE_ME_* values are exactly what makes the
    skeleton copy-safe: a blind copy-paste produces REFUSALS at load,
    never a silent near-miss. Here: placeholders must EXIST in values
    (proving the skeleton is blank, not real-valued)... but also be
    replaceable (placeholders must be SYNTACTICALLY loadable)."""
    settings = _load(CONFIG / "_templates" / "settings.yaml")
    placeholders = []
    for section, knobs in settings.items():
        if not isinstance(knobs, dict):
            continue
        for knob, value in knobs.items():
            if isinstance(value, str) and value.startswith("REPLACE_ME"):
                placeholders.append(f"{section}.{knob}")
    assert placeholders, (
        "no REPLACE_ME_* placeholders found - the skeleton must be "
        "blank, not carry real-looking values"
    )
    # required-knob placeholders specifically (paths/duckdb) must exist:
    for want in ("paths.landing_root", "paths.scratch_root",
                 "duckdb.memory_limit"):
        assert want in placeholders, f"{want} must carry a placeholder"


# ---------------------------------------------------------------- _templates structural extras

def test_templates_run_registry_skeleton_shape():
    run = _load(CONFIG / "_templates" / "run.yaml")
    assert isinstance(run.get("pipes"), list)
    assert run["pipes"] and "REPLACE_ME" in str(run["pipes"][0])


def test_templates_pipe_skeleton_is_yaml_with_connections():
    skel = _load(CONFIG / "_templates" / "extractors" / "pipe_skeleton.yaml")
    assert isinstance(skel, dict) and skel
    first = next(iter(skel.values()))
    assert "connections" in first


# ---------------------------------------------------------------- _example parity

def test_example_settings_loads_strict():
    """THE behaviour-truth test: _example/settings.yaml must pass the
    strict registry validation exactly as a real environment would."""
    settings = load_settings(str(CONFIG / "_example" / "settings.yaml"))
    assert settings is not None, (
        "_example/settings.yaml failed strict validation - the minimal "
        "runnable baseline is broken"
    )
    # PT-046: separator-free (Windows yields backslashes)
    from pathlib import Path as _P
    assert _P(settings["paths"]["runs_root"]).name == "_runs"


def test_example_settings_spans_all_sections():
    """Sections in the example = mandatory ones (paths, duckdb) plus the
    tuning sections chosen for demonstration. All-optional sections
    (pause) are deliberately ABSENT from the minimal example - that
    absence is itself design proof (asserted in
    test_templates_parity.py). Never turn this into 'all sections'."""
    settings = _load(CONFIG / "_example" / "settings.yaml")
    assert {"polars", "paths", "duckdb", "writer",
            "extraction"} <= set(settings)


def test_example_pass_mgr_structurally_valid_without_real_values():
    pm = _load(CONFIG / "_example" / "pass_mgr.yaml")
    assert pm["default_method"] in ("fernet", "sops")
    methods = pm["methods"]
    assert {"fernet", "sops"} <= set(methods)
    assert all(isinstance(v, dict) for v in methods.values())


def test_example_run_registry_empty_pipe_list_is_deliberate():
    run = _load(CONFIG / "_example" / "run.yaml")
    assert run == {"pipes": []}


def test_example_wrapper_matches_committed_shape():
    """The example wrapper mirrors the canonical _templates/wrapper.yaml
    shape - same profiles, same step vocabulary. (PT-040 4c: the live
    anchor moved from top-level config/wrapper.yaml - machine-local env
    folders are NOT test anchors; the committed template is.)"""
    example = _load(CONFIG / "_example" / "wrapper.yaml")
    tpl = _load(CONFIG / "_templates" / "wrapper.yaml")
    assert set(example.get("profiles", {})) == set(tpl.get("profiles", {}))
    assert set(example["profiles"]["full"]["steps"][0]) == {"name", "force"}


# ---------------------------------------------------------------- registry self-review

def test_registry_copy_then_replace_semantics():
    """Simulated operator flow: copy _templates/settings.yaml, replace
    REPLACE_ME_* values, then strict-load. Must succeed - this is the
    copy-and-fill procedure an operator performs for a new environment."""
    raw = yaml.safe_load(
        (CONFIG / "_templates" / "settings.yaml").read_text(encoding="utf-8"))

    filled = copy.deepcopy(raw)
    replacements = {
        "REPLACE_ME_STAGING_PATH": "data/staging",
        "REPLACE_ME_SCRATCH_PATH": "data/scratch",
        "REPLACE_ME_HISTORY_DB_PATH": "data/staging/_history/dpl_history.db",
    }

    def _fill(node):
        if isinstance(node, dict):
            return {k: _fill(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_fill(v) for v in node]
        if isinstance(node, str) and node in replacements:
            return replacements[node]
        return node

    filled = _fill(filled)
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(yaml.safe_dump(filled))
        path = fh.name
    try:
        settings = load_settings(path)
        assert settings is not None, (
            "copy _templates/ -> replace placeholders -> load FAILED - "
            "the documented operator procedure is broken"
        )
    finally:
        Path(path).unlink(missing_ok=True)
