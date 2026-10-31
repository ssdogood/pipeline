# wrapper/run.py
"""Wrapper entry point: python -m dpl.wrapper.run [--profile NAME] [--tables a,b]

Runs from project root only (relative config paths, like every other
module). No args -> profile 'full'.

Exit codes (cron contract):
    0 - success
    1 - extraction failures (history recorded fine)
    2 - SQLite/history failure (data may have landed; repair with
        `python -m dpl.wrapper.update_history`)
    3 - config error (nothing ran)

Precedence when several things fail: 3 > 2 > 1. History failure wins
over extraction failure because recovering history awareness matters
more than re-alerting on an extraction failure the manifests already
record in detail.
"""

from __future__ import annotations

import argparse
import os
import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from dpl.lib.common import (
    load_config,
    load_settings,
    load_schema_master,
    reconcile_pending,
    prune_old_landing,
    prune_old_runs,
    make_run_id,
    get_run_logger,
    runs_dir,
)
from dpl.lib.logger import setup_logging
from dpl.wrapper.profiles import load_wrapper_config, resolve_profile
from dpl.wrapper.update_history import (
    open_db,
    update_history,
    validate_db_path,
)

EXIT_OK = 0
EXIT_EXTRACT = 1
EXIT_HISTORY = 2
EXIT_CONFIG = 3

CONFIG_PATH = "config/extractors/oracle_to_stg.yaml"
SETTINGS_PATH = os.environ.get("DPL_SETTINGS", "config/settings.laptop.yaml")
WRAPCFG_PATH = "config/wrapper.yaml"
SCHEMA_MASTER_PATH = "config/extractors/schema_master_oracle_to_stg.csv"


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrapper.run",
        description="DPL orchestration wrapper (default profile: full)")
    parser.add_argument("--profile", default="full",
                        help="profile name from wrapper.yaml (default: full)")
    parser.add_argument("--tables", default=None,
                        help="comma-separated table filter (testing/targeted)")
    args = parser.parse_args(argv)

    setup_logging()
    log = get_run_logger("dpl.wrapper")
    load_date = date.today()

    # ------------------------------------------------------------------
    # Tier 1: validate ALL configs before any step runs -> exit 3
    # ------------------------------------------------------------------
    wcfg = load_wrapper_config(WRAPCFG_PATH)
    settings = load_settings(SETTINGS_PATH)
    cfg = load_config(CONFIG_PATH)
    if wcfg is None or settings is None or cfg is None:
        log.error("config validation failed — aborting before any step")
        return EXIT_CONFIG

    try:
        steps = resolve_profile(wcfg, args.profile)
    except KeyError as e:
        log.error("%s", e)
        return EXIT_CONFIG

    history_db = Path(settings["paths"]["history_db"])
    try:
        validate_db_path(history_db, settings)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_CONFIG          # badly configured path = config error

    manifest_days = wcfg["retention"]["manifest_days"]

    # ------------------------------------------------------------------
    # Run identity: ONE run_id for logs, manifests, and DB rows
    # ------------------------------------------------------------------
    rid = wcfg["run_id"]
    run_id = make_run_id(prefix=rid["prefix"], ts_format=rid["ts_format"])
    log = get_run_logger("dpl.wrapper", run_id=run_id)

    only_tables = ([t.strip() for t in args.tables.split(",")]
                   if args.tables else None)
    log.info("wrapper start: profile=%s run_id=%s tables=%s",
             args.profile, run_id, only_tables or "all")

    # ------------------------------------------------------------------
    # History: insert the 'running' row so a crash leaves a visible stub
    # ------------------------------------------------------------------
    conn = None
    history_failed = False
    try:
        conn = open_db(history_db)
        conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, profile, started_at, finished_at, status, exit_code) "
            "VALUES (?,?,?,?,?,NULL)",
            (run_id, args.profile, _utc_now_iso(), None, "running"))
        conn.commit()
    except sqlite3.Error as exc:
        log.error("history db unreachable: %s — continuing without history",
                  exc)
        conn = None
        history_failed = True

    # ------------------------------------------------------------------
    # Execute steps: record-and-continue semantics
    # ------------------------------------------------------------------
    extract_failed = False
    manifests: dict[str, dict] = {}

    for step in steps:
        name = step["name"]
        try:
            if name == "generate_schemas":
                from dpl.lib.generate_schemas import (
                    generate_schemas as gen,
                    CONFIG_FILE as gen_config,
                    OUTPUT_FILE as gen_output,
                )
                rc = gen(config_path=gen_config,
                         output_path=gen_output,
                         force=step.get("force", True),
                         dry_run=False)
                if rc != 0:
                    log.warning(
                        "generate_schemas returned %s — continuing "
                        "(partial introspection failures; affected tables "
                        "will fail loudly at extraction if it matters)", rc)

            elif name == "reconcile_pending":
                master = load_schema_master(SCHEMA_MASTER_PATH)
                resolved = reconcile_pending(master)
                log.info("reconciled pending entries: %s",
                         resolved if resolved else "none")

            elif name == "extract":
                from dpl.extractors.oracle_to_stg import extract_all
                manifests, any_failed = extract_all(
                    cfg, settings, load_date,
                    force=step.get("force", False),
                    only_tables=only_tables,
                    run_id=run_id,
                    profile=args.profile)
                extract_failed = any_failed

            elif name == "update_history":
                ingested, skipped = update_history(
                    settings, history_db, days=manifest_days)
                log.info("history: %s ingested, %s up to date",
                         ingested, skipped)

            elif name == "housekeeping":
                prune_old_landing(
                    Path(settings["paths"]["landing_root"]),
                    days=wcfg["retention"]["landing_days"])
                prune_old_runs(
                    Path(settings["paths"]["runs_root"]),
                    days=manifest_days)

            else:
                # profiles.py validates the vocabulary; unreachable
                log.error("unknown step '%s' — skipping", name)

        except Exception as exc:                    # noqa: BLE001 — deliberate
            log.error("step '%s' failed: %s", name, exc, exc_info=True)
            if name == "extract":
                extract_failed = True
            elif name == "update_history":
                history_failed = True

    # ------------------------------------------------------------------
    # Exit code (precedence: history failure outranks extraction failure)
    # ------------------------------------------------------------------
    if history_failed:
        exit_code = EXIT_HISTORY
    elif extract_failed:
        exit_code = EXIT_EXTRACT
    else:
        exit_code = EXIT_OK

    # ------------------------------------------------------------------
    # Append wrapper final state to each envelope (design note rev2 §7).
    # Runs AFTER exit_code is known; the residual gap (wrapper dies
    # before this rewrite) is the documented accepted caveat — a manual
    # `python -m wrapper.update_history` repairs the DB side.
    # ------------------------------------------------------------------
    for folder in manifests:
        try:
            mdir = runs_dir(settings["paths"]["runs_root"], load_date, folder)
            mfile = mdir / f"_manifest_{run_id}.json"
            if mfile.exists():
                env = json.loads(mfile.read_text(encoding="utf-8"))
                env["wrapper"] = {
                    "exit_code": exit_code,
                    "run_finished_at": _utc_now_iso(),
                }
                mfile.write_text(json.dumps(env, indent=2),
                                 encoding="utf-8")
        except OSError as exc:
            log.error("could not append wrapper state to %s manifest: %s",
                      folder, exc)

    # ------------------------------------------------------------------
    # Close out: final state in the DB, then re-ingest manifests so the
    # wrapper fields land in history too.
    # ------------------------------------------------------------------
    if conn is not None:
        try:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, exit_code=? "
                "WHERE run_id=?",
                (_utc_now_iso(),
                 "partial_failure" if extract_failed
                 else ("history_error" if history_failed else "success"),
                 exit_code, run_id))
            conn.commit()
        except sqlite3.Error as exc:
            log.error("final history write failed: %s", exc)
            history_failed = True
            exit_code = EXIT_HISTORY
        finally:
            conn.close()
    # re-ingest AFTER conn.close(): single writer at a time. The upsert
    # CASE in update_history will not demote our final 'runs' row.
    if not history_failed or manifests:
        try:
            update_history(settings, history_db, days=manifest_days)
        except (sqlite3.Error, OSError, ValueError,
                json.JSONDecodeError) as exc:
            log.error("post-run history ingest failed: %s — repair with "
                       "`python -m wrapper.update_history`", exc)
            history_failed = True
            exit_code = EXIT_HISTORY

    log.info("wrapper done: run_id=%s profile=%s exit=%s%s",
             run_id, args.profile, exit_code,
             " (run `python -m wrapper.update_history` to repair history)"
             if exit_code == EXIT_HISTORY else "")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
