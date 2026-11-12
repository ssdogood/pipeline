"""Timestamp doctrine on the LOG plane: asctime must render UTC-Z.

The dpl doctrine (rev2) is ISO-8601 UTC, literal Z suffix, no
fractional seconds. lib/timefmt.py already enforces it for history
rows and email timestamps; these tests pin the LOG plane, which
logging.Formatter would otherwise render in naive LOCAL time.

Three surfaces, one truth:
  1. the UtcFormatter class renders canonical UTC-Z
  2. the built-in fallback config (_default_config) uses it
  3. config/logging.yaml (the file ops actually deploys) uses it

Surface 3 reads the real YAML from the repo so a future hand-edit
that silently reverts to local time fails a test, not a 3 AM debug
session.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from dpl.lib.logger import UtcFormatter, _default_config

_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"
_CANONICAL = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TOLERANCE_SEC = 2   # format() may straddle a second boundary vs gmtime()


def _probe_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="dpl.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg="probe", args=(), exc_info=None, func="test_asctime")


def test_utc_formatter_renders_canonical_z() -> None:
    f = UtcFormatter("%(asctime)s | %(message)s", datefmt=_DATEFMT)
    out = f.format(_probe_record())
    assert _CANONICAL.fullmatch(out.split(" | ")[0]), out
    # and it agrees with real UTC now (not localtime), within rollover
    stamped = datetime.strptime(out.split(" | ")[0], _DATEFMT)
    now = datetime.now(UTC).replace(tzinfo=None)
    assert abs((stamped - now).total_seconds()) <= _TOLERANCE_SEC, out


def test_fallback_config_uses_utc_formatter() -> None:
    std = _default_config()["formatters"]["standard"]
    assert std["()"] == "dpl.lib.logger.UtcFormatter"
    assert std["datefmt"] == _DATEFMT


def test_deployed_yaml_uses_utc_formatter() -> None:
    cfg = yaml.safe_load(
        Path("config/logging.yaml").read_text(encoding="utf-8"))
    std = cfg["formatters"]["standard"]
    assert std["()"] == "dpl.lib.logger.UtcFormatter"
    assert std["datefmt"] == _DATEFMT
