# dpl/lib/logger.py
"""
Centralized logging for the data pipeline (dpl), configured via dictConfig.

Location: PROJECT_ROOT/src/dpl/lib/logger.py
Logs:      PROJECT_ROOT/logs/ (override with DPL_LOG_DIR env var)

Logger tree (children inherit handlers automatically via propagation):
    dpl              <- configured here: console + rotating file + error file
      dpl.ora        <- lib/ora_connect.py
      dpl.writer     <- lib/write_parquet.py
      dpl.schema     <- lib/generate_schemas.py
      dpl.oracle_to_stg  <- extractors/oracle_to_stg.py
    (lib/common.py uses the root "dpl" name itself)

Run-context fields: every log line carries %(run_id)s and %(table)s.
Formatter defaults render them as '-' for plain loggers; the extractor
wraps its per-table scope in a LoggerAdapter (get_run_logger) that
injects the real values, so `grep` on table/run_id isolates one table's
story across a fault-tolerant run.

Usage:
    from dpl.lib.logger import setup_logging, get_run_logger

    setup_logging()                          # once, at process start
    log = get_run_logger("dpl.schema")       # component adapter
    tlog = get_run_logger("dpl.oracle_to_stg", run_id=run_id, table=table)

"""

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path

import yaml

from dpl.lib.paths import get_project_root

# lib/ is one directory below the project root
PROJECT_ROOT = get_project_root()
LOG_DIR = Path(os.environ.get("DPL_LOG_DIR", PROJECT_ROOT / "logs"))
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "logging.yaml"

# Fields injected by LoggerAdapter in run scopes; defaults make them
# safe for every plain logger that shares the format string.
CONTEXT_DEFAULTS = {"run_id": "-", "table": "-"}

ROOT_LOGGER_NAME = "dpl"


def _load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load logging config from YAML; fall back to built-in defaults if missing."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return _default_config()


def _default_config() -> dict:
    """Fallback config mirroring config/logging.yaml."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s | %(name)s | %(funcName)s | "
                          "%(levelname)-8s | run_id=%(run_id)s | "
                          "table=%(table)s | %(message)s",
                "defaults": CONTEXT_DEFAULTS,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "standard",
                "filename": str(LOG_DIR / "dpl_pipeline.log"),
                "maxBytes": 5242880,       # 5 MB
                "backupCount": 30,
                "encoding": "utf-8",
            },
            "errors": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "standard",
                "filename": str(LOG_DIR / "dpl_errors.log"),
                "maxBytes": 5242880,
                "backupCount": 30,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            ROOT_LOGGER_NAME: {
                "handlers": ["console", "file", "errors"],
                "level": "DEBUG",
                "propagate": False,   # children propagate HERE; stop at dpl
            },
        },
        "root": {
            "handlers": ["console"],
            "level": "WARNING",
        },
    }


def setup_logging(config_path: Path | None = None) -> None:
    """Configure the dpl logger tree. Call once at process start.

    Safe to call again (e.g. by a module's standalone smoke-test entry):
    dictConfig replaces the configuration rather than duplicating handlers.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    config = _load_config(Path(config_path) if config_path else DEFAULT_CONFIG_PATH)
    # Ensure parent dirs exist for any file handlers declared in YAML
    # Anchor relative handler filenames to LOG_DIR (the YAML stays portable),
    # and ensure parent dirs exist for any file handlers declared anywhere.
    for handler in config.get("handlers", {}).values():
        filename = handler.get("filename")
        if not filename:
            continue
        p = Path(filename)
        if not p.is_absolute():
            p = LOG_DIR / p.name
            handler["filename"] = str(p)
        p.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(config)



def get_run_logger(name: str = ROOT_LOGGER_NAME,
                   run_id: str = "-",
                   table: str = "-") -> logging.LoggerAdapter:
    """
    Run-scoped logger adapter: injects run_id and table into every record
    emitted through it, guaranteeing the formatter's context fields exist
    (dictConfig formatter `defaults` alone proved unreliable).

    `name` selects the component logger ("dpl.ora", "dpl.schema",
    "dpl.writer", "dpl.oracle_to_stg", ...). Children of "dpl" inherit its
    handlers via propagation, so no per-child handler wiring is needed.
    Defaults to the "dpl" root itself.

    Use for the extractor's per-table loop (and anywhere a dynamic scope
    changes mid-run). Create a FRESH adapter per table when tables are
    processed in parallel — the adapter instance is what carries context.
    """
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, {"run_id": run_id, "table": table})

