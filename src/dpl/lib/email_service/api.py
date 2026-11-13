# dpl/lib/email_service/api.py
"""Public facade: the ONLY module pipeline code ever imports.

    from dpl.lib.email_service import send_email
    r = send_email(caller="daily_report",
                   data={"rows": 123}, run_id="RUN-42")
    # r is ALWAYS a SendResult - this call NEVER raises.

Contract (requirements spec + ratified 2026-11-11 decisions):
  - never raises, under any circumstance (failure isolation): config
    faults, typos, vault outages, SMTP deaths, and internal bugs all
    become structured failed SendResults
  - disabled caller: terminal 'skipped_disabled' result, one DEBUG
    log line, AND a history row (ratified - config-said-no must be
    auditable, distinguishable from a vanished email)
  - dry-run (server.dry_run=true): full compose, .eml in the outbox,
    history row, no socket ever opened
  - live: compose, then transport (retries, classification,
    finished_at, history)
  - every terminal outcome is recorded in history exactly once;
    a DB failure is logged loudly and swallowed (the isolation wall
    is transport._write_history, shared here)

Config is LOADED PER CALL, deliberately: the per-caller 'enabled'
toggle must be honored as of call time (spec Section 9), and email
volume is pipeline-scale, not loop-scale - a YAML edit takes effect
on the next send without restarting anything.

History-row honesty: 'mode' on early-terminal rows reflects the
ACTUAL configured mode (dry_run/live) whenever the config could be
loaded; only the bad_config path (config unreadable/invalid) falls
back to "live" as a conservative marker.

Two facade error classes (result.py): bad_caller (name not
configured) and bad_config (faults surfaced at call time). Config
validation remains load-time-only doctrine; the facade simply
treats a failed LOAD as a structured failure instead of letting it
escape.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path

from dpl.lib.email_service.composer import compose_email
from dpl.lib.email_service.config import ConfigError, load_email_config
from dpl.lib.email_service.result import (
    EC_BAD_CALLER,
    EC_BAD_CONFIG,
    EC_DISABLED,
    EC_INTERNAL,
    OUTCOME_COMPOSED,
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    SendResult,
)
from dpl.lib.email_service.transport import _write_history, deliver_email
from dpl.lib.logger import get_run_logger
from dpl.lib.timefmt import now_utc_z

log = get_run_logger("dpl.email.api")


def _stamp() -> tuple[str, str]:
    """Fresh send_id + created_at for early-terminal results that
    never reach the composer."""
    return f"S-{uuid.uuid4().hex[:12]}", now_utc_z()


def _early_terminal(caller: str, mode: str, outcome: str,
                    error_class: str, summary: str, run_id: str,
                    db_path: Path | None) -> SendResult:
    """Terminal result for anything decided BEFORE composition:
    skipped-disabled, unknown caller, config faults. Recorded in
    history like every terminal outcome (isolated against DB
    failure through the shared wall)."""
    caller = caller if isinstance(caller, str) else str(caller)
    send_id, created_at = _stamp()
    if outcome == OUTCOME_SKIPPED:
        log.debug("send skipped: caller=%s disabled by config "
                  "(send_id=%s)", caller, send_id)
    else:
        log.warning("send failed early: caller=%s class=%s: %s "
                    "(send_id=%s)", caller, error_class, summary,
                    send_id)
    result = SendResult(
        send_id=send_id, caller=caller, created_at=created_at,
        mode=mode, outcome=outcome, error_class=error_class,
        attempts_used=0, recipient_count=0, subject="",
        error_summary=summary, run_id=run_id, message=None,
        envelope_recipients=(),
        finished_at=now_utc_z())          # early paths are terminal NOW
    return _write_history(result, db_path)


def _mode_of(cfg_dry_run: bool) -> str:
    return "dry_run" if cfg_dry_run else "live"


def send_email(caller: str,
               data: Mapping[str, object] | None = None,
               run_id: str = "-",
               config_path: Path | str | None = None,
               db_path: Path | None = None) -> SendResult:
    """Send (or deliberately not send) one email as one caller.

    Args:
      caller:       configured caller name (config/email_service.yaml)
      data:         template values; merged over the caller's static
                    'data'; send-call values win
      run_id:       pipeline run identifier recorded in history
      config_path:  nondefault config file (tests/acceptance)
      db_path:      nondefault history DB (tests/acceptance); default
                    resolves per history_db.resolve_db_path precedence

    Returns a SendResult for EVERY path. Never raises.
    """
    try:
        # ---- load config (per call - see module docstring) ----------
        try:
            cfg = load_email_config(config_path)
        except ConfigError as exc:
            return _early_terminal(
                caller, "live", OUTCOME_FAILED, EC_BAD_CONFIG,
                "; ".join(getattr(exc, "errors", [str(exc)])),
                run_id, db_path)
        except OSError as exc:
            return _early_terminal(
                caller, "live", OUTCOME_FAILED, EC_BAD_CONFIG,
                f"config unreadable: {exc}", run_id, db_path)

        mode = _mode_of(cfg.server.dry_run)

        # ---- caller lookup ------------------------------------------
        c = cfg.callers.get(caller)
        if c is None:
            return _early_terminal(
                caller, mode, OUTCOME_FAILED, EC_BAD_CALLER,
                f"caller {caller!r} is not configured "
                f"(configured: {sorted(cfg.callers) or 'none'})",
                run_id, db_path)

        # ---- the ratified ops toggle, checked at call time -----------
        if not c.enabled:
            return _early_terminal(
                c.name, mode, OUTCOME_SKIPPED, EC_DISABLED,
                "caller disabled by config", run_id, db_path)

        # ---- compose -------------------------------------------------
        composed = compose_email(cfg.server, c, data, run_id)

        # ---- terminal-at-compose outcomes: record + return -----------
        if composed.outcome in (OUTCOME_DRY_RUN, OUTCOME_FAILED):
            return _write_history(composed, db_path)

        # ---- live: hand to transport (writes the final row) ----------
        assert composed.outcome == OUTCOME_COMPOSED
        return deliver_email(cfg.server, composed, db_path=db_path)

    except Exception as exc:
        # The last resort boundary. Nothing above should leak, but if
        # it ever does, the pipeline still must not feel it.
        log.exception("send_email internal error (caller=%s)", caller)
        send_id, created_at = _stamp()
        return _write_history(SendResult(
            send_id=send_id, caller=str(caller), created_at=created_at,
            mode="live", outcome=OUTCOME_FAILED,
            error_class=EC_INTERNAL, attempts_used=0,
            recipient_count=0, subject="",
            error_summary=f"facade internal error: "
                         f"{type(exc).__name__}: {exc}",
            run_id=run_id, message=None, envelope_recipients=(),
            finished_at=now_utc_z()), db_path)


__all__ = ["send_email"]
