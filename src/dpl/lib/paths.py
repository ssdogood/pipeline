# src/dpl/lib/paths.py
"""Project-root resolution. Leaf module - imports nothing from dpl."""

from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    """Locate the pipeline root (owns config/, secrets/, staging/, logs/).

    Order:
      1. DPL_HOME env var - explicit, used by cron on the prod box
      2. Walk up from this file for a dir holding pyproject.toml + config/
    """
    env = os.environ.get("DPL_HOME")
    if env:
        return Path(env).expanduser().resolve()

    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists() and (parent / "config").is_dir():
            return parent

    raise RuntimeError(
        "Cannot locate pipeline root: set DPL_HOME to the project directory "
        "containing config/ and secrets/."
    )