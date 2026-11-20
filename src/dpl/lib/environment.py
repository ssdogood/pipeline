# dpl/lib/environment.py
"""
Environment resolution: the single place that knows WHICH environment
this machine runs.

Design (2026-11-20 environment design):
  - config/environment.yaml is the ONE AND ONLY declaration of the
    active environment. The words development/test/production appear
    in the tree exactly here - every other file is environment-neutral
    by name; its environment comes from the folder it sits in.
  - Exactly one environment folder exists per machine (gitignored,
    machine-local): config/development/, config/test/ or
    config/production/. _templates/ (blank skeleton) and _example/
    (minimal runnable) are committed and NEVER selectable.
  - Every caller uses resolve_environment(); no module may hardcode
    an environment name or path (retires the settings.laptop.yaml
    pattern).

Leaf-ish module: imports nothing from dpl except paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from dpl.lib.paths import get_project_root

# Fixed vocabulary - never extended, never aliased. The environment
# folder name MUST equal the declared value.
VALID_ENVIRONMENTS = ("development", "test", "production")

# The one key the selector file accepts.
SELECTOR_KEY = "environment"

SELECTOR_FILENAME = "environment.yaml"


@dataclass(frozen=True)
class ResolvedEnvironment:
    """The active environment: its name and its config folder."""
    name: str
    folder: Path


def resolve_environment(
    selector_path: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
) -> ResolvedEnvironment:
    """Resolve the active environment from config/environment.yaml.

    Validation order (every step refuses loudly - never silent):
      1. selector file exists
      2. parses as YAML
      3. is a mapping with exactly one key: 'environment'
      4. value is one of development | test | production
         (exact, lowercase, full words)
      5. folder config/<value>/ exists on this machine

    The keyword arguments let tests point the resolver at a scratch
    tree; production callers use the defaults.

    Raises ValueError on every refusal; returns ResolvedEnvironment.
    """
    root = get_project_root()
    cfg_dir = Path(config_dir) if config_dir else root / "config"
    selector = (Path(selector_path) if selector_path
                else cfg_dir / SELECTOR_FILENAME)

    if not selector.is_file():
        raise ValueError(
            f"{selector} not found. This machine must declare its "
            f"environment. Create it with ONE key, e.g.\n"
            f"  environment: development\n"
            f"(valid values: {', '.join(VALID_ENVIRONMENTS)}), then copy "
            f"config/_templates/ to config/<environment>/ and fill in "
            f"the placeholders."
        )

    try:
        raw = yaml.safe_load(selector.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{selector}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"{selector}: must be a mapping with exactly one key, "
            f"'{SELECTOR_KEY}'. Got: {type(raw).__name__}."
        )

    extra = sorted(k for k in raw if k != SELECTOR_KEY)
    if extra:
        raise ValueError(
            f"{selector} accepts only the key '{SELECTOR_KEY}'. "
            f"Found extra key(s): {extra}. Remove them - the environment "
            f"is declared in exactly one place, this file."
        )

    value = raw.get(SELECTOR_KEY)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{selector}: '{SELECTOR_KEY}' must be one of "
            f"{', '.join(VALID_ENVIRONMENTS)} (got: {value!r})."
        )

    name = value.strip()
    if name not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"{selector}: environment '{name}' is invalid. Must be "
            f"exactly one of: {', '.join(VALID_ENVIRONMENTS)} - "
            f"lowercase, full words; 'dev'/'prod'/synonyms are refused "
            f"on purpose. '_templates' and '_example' are never "
            f"selectable."
        )

    folder = cfg_dir / name
    if not folder.is_dir():
        raise ValueError(
            f"Environment folder {folder} does not exist on this machine. "
            f"Copy config/_templates/ to {folder} and fill in the "
            f"placeholders, or fix {selector}."
        )

    return ResolvedEnvironment(name=name, folder=folder)


def format_banner(env: ResolvedEnvironment) -> str:
    """One-line run banner: makes the active environment visible in
    every log at a glance. Print/log this at the start of every run -
    a flipped or wrong selector value must be impossible to miss."""
    return f"RUNNING ENVIRONMENT: {env.name} (folder: {env.folder})"
