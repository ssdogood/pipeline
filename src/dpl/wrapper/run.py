# dpl/wrapper/run.py
"""Wrapper entry point: python -m dpl.wrapper.run [--profile NAME] [--tables a,b] [--connection NAME]

Runs from project root only (relative config paths, like every other
module). No args -> profile 'inc'.

WHICH PIPES RUN is configured, never discovered-from-disk:
    config/run.yaml lists the pipes (yaml stems in config/extractors/)
    that the wrapper executes, in that order. Unregistered yamls in the
    extractor folder are ignored (and logged as such). A registered name
    whose yaml is missing, a name registered twice, or a yaml whose
    connection type has no connector in lib/connectors.py aborts at
    Tier 1 with a named error and exit 3 - never a silent skip.

    Add a pipe:    drop the yaml, add one line to config/run.yaml.
    Add a connector: lib connector + extractor module + ONE entry in
        lib/connectors.py. This file never changes.

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
import importlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from dpl.lib.common import (
    get_run_logger,
    load_config,
    load_schema_master,
    load_settings,
    make_run_id,
    prune_old_landing,
    prune_old_runs,
    reconcile_pending,
    runs_dir,
    validate_secrets_layout,
)
from dpl.lib.connectors import CONNECTORS
from dpl.lib.extraction_config import (
    build_plan,
    format_plan,
    validate_global_extraction,
    validate_pipe_connections,
)
from dpl.lib.folder_runner import jobs_from_plan, run_jobs
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

SETTINGS_PATH = "config/settings.laptop.yaml"      # env override: DPL_SETTINGS
EXTRACTOR_DIR = Path("config/extractors")
RUN_REGISTRY = Path("config/run.yaml")
WRAPCFG_PATH = "config/wrapper.yaml"


# ----------------------------------------------------------- run registry ---
@dataclass
class Pipe:
    """One registered extraction pipe, fully self-describing."""
    name: str          # yaml stem, e.g. 'oracle_to_stg' - from run.yaml
    path: str          # the extractor yaml (relative path)
    source_type: str   # the ONE shared type across all its connections
    master: str        # schema_master_<name>.csv (naming convention)
    connector: dict    # CONNECTORS[source_type] entry
    connections: dict  # parsed connections: mapping (name -> cfg) - PT-034 dispatch


def _load_run_registry(path: Path = RUN_REGISTRY) -> list[str]:
    """Parse config/run.yaml; raise ValueError with named causes."""
    if not path.is_file():
        raise ValueError(
            f"run registry not found: {path}. Create it listing the "
            f"pipes (yaml stems in {EXTRACTOR_DIR}) that the wrapper "
            f"should execute, e.g.\npipes:\n  - oracle_to_stg")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    pipes = (raw or {}).get("pipes")
    if not isinstance(pipes, list) or not pipes:
        raise ValueError(
            f"{path}: must contain a non-empty 'pipes:' list of yaml "
            f"stems, e.g.\npipes:\n  - oracle_to_stg")
    seen: set[str] = set()
    for entry in pipes:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{path}: every pipes: entry must be a "
                            f"yaml stem string; got {entry!r}")
        if entry in seen:
            raise ValueError(f"{path}: pipe '{entry}' registered twice")
        seen.add(entry)
    return pipes


def _route_pipe(name: str, extractor_dir: Path) -> Pipe:
    """Validate ONE registered pipe against disk + connector registry.
    Raises ValueError naming the pipe and the exact problem."""
    yml = extractor_dir / f"{name}.yaml"
    if not yml.is_file():
        raise ValueError(
            f"run.yaml registers pipe '{name}' but {yml} does not exist "
            f"- remove the registration or restore the file")

    try:
        raw = yaml.safe_load(yml.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{yml}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict) or \
            not isinstance(raw.get("connections"), dict) or not raw["connections"]:
        raise ValueError(
            f"{yml}: no connections: block - a pipe yaml must declare at "
            f"least one connection")

    types = {
        str(conn.get("type")).lower()
        for conn in raw["connections"].values()
        if isinstance(conn, dict) and conn.get("type")
    }
    if not types:
        raise ValueError(
            f"{yml}: connections declare no 'type' - cannot route this "
            f"pipe to a connector")
    if len(types) > 1:
        raise ValueError(
            f"{yml}: mixed source types {sorted(types)} - one extractor "
            f"yaml serves exactly one connector type; split the file")

    stype = types.pop()
    connector = CONNECTORS.get(stype)
    if connector is None:
        raise ValueError(
            f"{yml}: unknown source type '{stype}' - no connector "
            f"registered in dpl/lib/connectors.py (known types: "
            f"{sorted(CONNECTORS)})")

    return Pipe(
        name=name,
        path=str(yml),
        source_type=stype,
        master=str(extractor_dir / f"schema_master_{name}.csv"),
        connector=connector,
        connections=raw["connections"],
    )


def load_registered_pipes(registry_path: Path = RUN_REGISTRY,
                          extractor_dir: Path = EXTRACTOR_DIR) -> list[Pipe]:
    """THE pipe list: exactly the run.yaml registrations, in order.

    Raises ValueError on any registry/routing problem (caller converts
    to exit 3). Registered-but-unmatched yamls in the extractor dir are
    logged as unregistered visibility notes, never errors - dropping an
    experimental yaml in the folder must never break the run.
    """
    registered = _load_run_registry(registry_path)
    pipes = [_route_pipe(n, extractor_dir) for n in registered]

    # visibility (not validation): yamls present but intentionally not run
    on_disk = {p.stem for p in extractor_dir.glob("*.yaml")}
    unregistered = sorted(on_disk - set(registered))
    if unregistered:
        log = get_run_logger("dpl.wrapper")
        log.info("unregistered yaml(s) present, ignored: %s "
                 "(register in config/run.yaml to run them)",
                 ", ".join(unregistered))
    return pipes


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _notify(event: str, run_id: str, data: dict[str, object]) -> None:
    """Fire one email event. NEVER affects the run's exit code (FR-17).

    Failure isolation runs both ways: a broken email stack must not
    damage a data run. The late import keeps a damaged install from
    killing the wrapper at import time; the broad except guards even
    a misbehaving facade.
    """
    nlog = get_run_logger("dpl.wrapper", run_id=run_id)
    try:
        from dpl.lib.email_service import send_email
        r = send_email(caller=event, run_id=run_id, data=data)
        nlog.info("email %s: %s (%s)", event, r.outcome, r.send_id)
    except Exception as exc:              # noqa: BLE001 - email is never fatal
        nlog.error("email event %s could not fire: %s", event, exc)


# --------------------------------------------- merged scheduler (C2) ------
#
# Option B (design v3): ONE scheduler for all pipes. The wrapper merges
# every pipe's folder jobs into a single plan and executes them through
# folder_runner. These two pure functions are the merge; both are
# covered by tests/unit/test_wrapper_merge.py.

def _namespaced_connections(
    pipe_conns: list[tuple[str, dict[str, dict]]],
    folders_with_work: set[str] | None = None,
) -> dict[str, dict]:
    """Merge per-pipe validated connections into ONE plan namespace.

    Keys become '<pipe>::<connection>' so equal connection names in
    different pipes can never collide silently (the old dry-run merged
    by bare name - an Oracle and an MSSQL connection both named
    'ora_db' would have overwritten each other). folders_with_work is
    the union of every pipe's ctx['by_folder'] - folders that actually
    have tables after any --tables filter; an idle folder must never
    enter the scheduler.
    """
    merged: dict[str, dict] = {}
    for pipe_name, conns in pipe_conns:
        for cname, summary in conns.items():
            if folders_with_work is not None \
                    and summary["folder"] not in folders_with_work:
                continue
            merged[f"{pipe_name}::{cname}"] = summary
    return merged


def _folder_owners(ready: list[tuple[Any, Any, dict, dict]]
                   ) -> dict[str, tuple[Any, Any, dict]]:
    """Map every prepared folder to its owning (pipe, cfg, ctx).

    Folder names are globally unique across pipes (build_plan raises
    on duplicates), so this is a strict 1:1 routing table from the
    scheduler's folder jobs back to the pipe that extracts them.
    """
    return {summary["folder"]: (pipe, cfg, ctx)
            for pipe, cfg, conns, ctx in ready
            for summary in conns.values()}


def _validate_connection_name(pipes: list[Pipe], name: str) -> str | None:
    """PT-039: --connection must name a connection declared on at
    least one REGISTERED pipe. Returns None when valid; a loud
    one-line error naming the remedy otherwise (caller converts to
    exit 3). Pure - unit-tested in test_prepare_selection.py."""
    holders = [p.name for p in pipes if name in (p.connections or {})]
    if not holders:
        return (f"connection '{name}' is not declared on any "
                f"registered pipe (checked: "
                f"{', '.join(p.name for p in pipes) or 'none'}) - fix "
                f"the name or register its pipe in config/run.yaml")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrapper.run",
        description="DPL orchestration wrapper (default profile: inc)")
    parser.add_argument("--profile", default="inc",
                        help="profile name from wrapper.yaml (default: inc)")
    parser.add_argument(
        "--tables", default=None,
        help="comma-separated table names - extract ONLY these "
             "(default: all tables)")
    parser.add_argument("--paused-table", action="store_true",
                        help="with --tables naming paused table(s), "
                             "lift their TABLE-level pause for this run "
                             "(folder-level pauses are never liftable)")
    parser.add_argument("--connection", default=None,
                        help="run ONLY this connection's tables "
                             "(combine with --tables for intersection; "
                             "naming a paused connection without "
                             "--paused-table is refused)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate configs and print the extraction "
                             "plan (connections, serial groups, worst-case "
                             "sessions); run NOTHING")
    parser.add_argument("--settings", default=None,
                        help="path to settings yaml "
                             "(default: config/settings.laptop.yaml; "
                             "env override DPL_SETTINGS)")
    args = parser.parse_args(argv)

    setup_logging()
    log = get_run_logger("dpl.wrapper")
    load_date = date.today()

    # ------------------------------------------------------------------
    # Tier 1: validate ALL configs before any step runs -> exit 3
    # ------------------------------------------------------------------
    settings_path = args.settings or _settings_path_from_env()
    wcfg = load_wrapper_config(WRAPCFG_PATH)
    settings = load_settings(settings_path)
    if wcfg is None or settings is None:
        log.error("wrapper/settings config validation failed - aborting")
        return EXIT_CONFIG

    try:
        steps = resolve_profile(wcfg, args.profile)
    except KeyError as e:
        log.error("%s", e)
        return EXIT_CONFIG

    try:
        pipes = load_registered_pipes()
    except ValueError as e:
        log.error("run registry / pipe routing failed - aborting: %s", e)
        return EXIT_CONFIG
    if not pipes:
        log.error("no pipes registered in config/run.yaml")
        return EXIT_CONFIG

    # Global extraction knobs: present-but-invalid is a CONFIG error
    # (exit 3, nothing ran) - validated once here, reused by dry-run
    # and extract. A settings file WITHOUT the block still just warns
    # and runs sequential (safe-upgrade rule, Phase 1).
    try:
        gk = validate_global_extraction(settings)
    except ValueError as e:
        log.error("extraction knobs invalid - aborting: %s", e)
        return EXIT_CONFIG
    # --dry-run: Tier-1 validation + plan printing ONLY. No run_id, no
    # history rows, no vault init, no connections - configuration truth,
    # printed for the operator, then exit. Nothing anywhere is executed.
    if args.dry_run:
        try:
            pipe_conns: list[tuple[str, dict[str, dict]]] = []
            for pipe in pipes:
                cfg = load_config(pipe.path, pipe.connector["schema"])
                if cfg is None:
                    log.error("dry run: pipe %s config failed validation (%s)",
                              pipe.name, pipe.path)
                    return EXIT_CONFIG
                pipe_conns.append(
                    (pipe.name, validate_pipe_connections(pipe.name, cfg)))
            print(format_plan(build_plan(
                gk["max_parallel_connections"],
                _namespaced_connections(pipe_conns))))
            log.info("dry run: plan valid - nothing was executed")
        except ValueError as e:
            log.error("dry run failed: %s", e)
            return EXIT_CONFIG
        return EXIT_OK

    history_db = Path(settings["paths"]["history_db"])
    try:
        validate_db_path(history_db, settings)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_CONFIG

    # Vault-backed logins: settings must name the pass_mgr environment;
    # init here so every extractor inherits it (idempotent per process).
    pm_env = (settings.get("pass_mgr") or {}).get("environment")
    if not pm_env:
        log.error(
            "settings file %s must declare the vault environment, e.g.\n"
            "  pass_mgr:\n    environment: laptop", settings_path)
        return EXIT_CONFIG
    try:
        validate_secrets_layout()
        from dpl.lib import pass_mgr
        pass_mgr.init(pm_env)
    except (ValueError, FileNotFoundError) as e:
        log.error("vault unavailable - aborting: %s", e)
        return EXIT_CONFIG

    manifest_days = wcfg["retention"]["manifest_days"]

    # ------------------------------------------------------------------
    # Run identity: ONE run_id for logs, manifests, and DB rows
    # ------------------------------------------------------------------
    rid = wcfg["run_id"]
    run_id = make_run_id(prefix=rid["prefix"], ts_format=rid["ts_format"])
    log = get_run_logger("dpl.wrapper", run_id=run_id)

    only_tables = ([t.strip() for t in args.tables.split(",")]
                   if args.tables else None)
    only_connection = args.connection
    # PT-014/PT-039: --paused-table modifies a SELECTION (--tables or
    # --connection); alone it would change nothing, so refuse it
    # loudly (exit 3, nothing ran).
    paused_override = bool(args.paused_table)
    if paused_override and not (only_tables or only_connection):
        log.error("--paused-table modifies --tables/--connection "
                  "selections only - rerun as: --tables <names> "
                  "--paused-table (or --connection <name> "
                  "--paused-table)")
        return EXIT_CONFIG
    # PT-039: connection name validated against ALL registered pipes
    # up front - a typo is a config error (exit 3), never a silent
    # zero-table night.
    if only_connection is not None:
        err = _validate_connection_name(pipes, only_connection)
        if err:
            log.error("%s", err)
            return EXIT_CONFIG
    log.info("wrapper start: profile=%s run_id=%s tables=%s "
             "connection=%s",
             args.profile, run_id, only_tables or "all",
             only_connection or "all")

    # Tier-3 event (dormant by default; config-governed)
    _notify("run_started", run_id, {
        "run_id": run_id,
        "profile": args.profile,
        "tables": ", ".join(only_tables) if only_tables else "all",
    })

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
        log.error("history db unreachable: %s - continuing without history",
                  exc)
        conn = None
        history_failed = True

    # ------------------------------------------------------------------
    # Execute steps: record-and-continue semantics
    # ------------------------------------------------------------------
    extract_failed = False
    failed_pipes: list[str] = []
    manifests: dict[str, dict] = {}

    for step in steps:
        name = step["name"]
        try:
            if name == "generate_schemas":
                # PT-034: the wrapper stays dumb. All type/matrix knowledge
                # lives in dpl.lib.schema_dispatch; we obey its Decision.
                dispatch = importlib.import_module("dpl.lib.schema_dispatch")
                for pipe in pipes:
                    decision = dispatch.evaluate(
                        pipe.name, pipe.connector, pipe.connections,
                        pipe.source_type)
                    log.info("%s", decision.message)
                    if decision.action == "skip":
                        continue
                    if decision.action == "fail":
                        log.error(
                            "generate_schemas(%s): pipe failed dispatch - "
                            "continuing with remaining pipes", pipe.name)
                        continue
                    gen_mod = importlib.import_module(decision.generator)
                    rc = gen_mod.generate_schemas(
                        config_path=pipe.path,
                        output_path=pipe.master,
                        force=step.get("force", True),
                        dry_run=False,
                        settings_path=settings_path,
                        source_type=pipe.source_type,
                        paused_override=paused_override)
                    if rc != 0:
                        log.warning(
                            "generate_schemas(%s) returned %s - continuing "
                            "(partial introspection failures; affected "
                            "tables fail loudly at extraction if it "
                            "matters)", pipe.name, rc)

            elif name == "reconcile_pending":
                # Merge every registered pipe's master CSV that exists.
                master: dict = {}
                for pipe in pipes:
                    if Path(pipe.master).exists():
                        master.update(load_schema_master(pipe.master))
                resolved = reconcile_pending(master)
                log.info("reconciled pending entries: %s",
                         resolved if resolved else "none")

            elif name == "extract":
                # Phase 4 C2: ONE merged scheduler for all pipes (Option B).
                # Preflight every pipe first (no connections opened during
                # preflight), then run every folder job through folder_runner
                # with the global cap. Ruling Decision 2: a pipe failing ANY
                # preflight check is skipped - the healthy pipes still run.
                ready: list[tuple[Any, Any, dict, dict]] = []
                ext_by_pipe: dict[str, Any] = {}
                for pipe in pipes:
                    cfg = load_config(pipe.path, pipe.connector["schema"])
                    if cfg is None:
                        log.error("pipe %s: config failed validation (%s)",
                                  pipe.name, pipe.path)
                        extract_failed = True
                        failed_pipes.append(pipe.name)
                        continue
                    try:
                        conns = validate_pipe_connections(pipe.name, cfg)
                    except ValueError as e:
                        log.error("pipe %s: %s", pipe.name, e)
                        extract_failed = True
                        failed_pipes.append(pipe.name)
                        continue
                    try:
                        ext_mod = importlib.import_module(
                            pipe.connector["extractor"])
                        ctx = ext_mod.prepare_run(
                            cfg, settings, only_tables=only_tables,
                            run_id=run_id,
                            paused_override=paused_override,
                            only_connection=only_connection)
                    except Exception as exc:                    # noqa: BLE001 - pipe boundary: a bad pipe never kills the healthy ones
                        log.error("pipe %s prepare failed: %s",
                                  pipe.name, exc)
                        extract_failed = True
                        failed_pipes.append(pipe.name)
                        continue
                    ready.append((pipe, cfg, conns, ctx))
                    ext_by_pipe[pipe.name] = ext_mod

                if not ready:
                    log.error("extract: no pipe prepared - nothing to run")
                else:
                    work_folders: set[str] = set()
                    for _p, _c, _conns, ctx in ready:
                        work_folders.update(ctx["by_folder"])

                    plan = build_plan(
                        gk["max_parallel_connections"],
                        _namespaced_connections(
                            [(p.name, c) for p, _pc, c, _px in ready],
                            work_folders))
                    owners = _folder_owners(ready)

                    _force = step.get("force", False)

                    def make_task(folder, _owners=owners,
                                  _ext=ext_by_pipe,
                                  _force=_force):
                        pipe, cfg, ctx = _owners[folder]

                        def task():
                            return _ext[pipe.name].extract_folder(
                                ctx, cfg, settings, load_date, folder,
                                force=_force, run_id=run_id,
                                profile=args.profile)
                        return task

                    log.info("extract: %d folder job(s), max_parallel=%d "
                             "(worst-case sessions=%d)", plan["tasks"],
                             gk["max_parallel_connections"],
                             plan["worst_case_sessions"])

                    outcomes = run_jobs(
                        jobs_from_plan(plan, make_task),
                        gk["max_parallel_connections"])

                    for folder, (status, value) in outcomes.items():
                        if status == "ok":
                            manifests[folder] = value
                        else:
                            # Ruling Decision 1: folder-level
                            # continue-on-failure - the crash is recorded,
                            # siblings already ran; no envelope for this
                            # folder, tomorrow's disk-as-truth re-extracts.
                            log.error("folder '%s' failed: %s: %s",
                                      folder, type(value).__name__,
                                      str(value)[:300])
                            extract_failed = True
                            pipe_name = owners[folder][0].name
                            if pipe_name not in failed_pipes:
                                failed_pipes.append(pipe_name)

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
                log.error("unknown step '%s' - skipping", name)

        except Exception as exc:                                # noqa: BLE001 - step boundary: log failure, continue remaining steps
            log.error("step '%s' failed: %s", name, exc)
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
    # before this rewrite) is the documented accepted caveat - a manual
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
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.error("post-run history ingest failed: %s - repair with "
                       "`python -m wrapper.update_history`", exc)
            history_failed = True
            exit_code = EXIT_HISTORY

    # ── PT-014 Commit 2: stale-pause digest (once per run) ─────────
    # Pure scan -> payload; fires ONLY when something is stale. Pipe
    # configs are re-read here (cheap, no connections): by this point
    # the extract step has already loudly rejected any config fault,
    # so a scanner failure would mean the yaml changed mid-run - logged
    # and never fatal (FR-17: notifications can't touch the exit code).
    try:
        from dpl.lib.pause_digest import digest_payload, stale_pause_entries
        stale_days = (settings.get("pause") or {}).get("stale_days", 14)
        pipe_cfgs: dict[str, dict] = {}
        for p in pipes:
            c = load_config(p.path, p.connector["schema"])
            if c is not None:
                pipe_cfgs[p.name] = c
        payload = digest_payload(
            stale_pause_entries(pipe_cfgs, stale_days), stale_days)
        if payload:
            log.info("pause digest: %d stale pause(s), threshold %d "
                     "day(s)", payload["stale_count"], stale_days)
            payload["run_id"] = run_id
            _notify("pause_stale", run_id, payload)
        else:
            log.debug("pause digest: nothing stale at threshold %d "
                      "day(s)", stale_days)
    except Exception as exc:               # noqa: BLE001 - never fatal
        log.error("pause digest could not be evaluated: %s", exc)

    # ── Email events: fire after final state is known, before exit ──
    # Exactly one terminal event per run. Tier protection for
    # run_failed lives in config.py (enabled: false -> load error).
    if exit_code == EXIT_OK:
        _notify("run_completed", run_id, {
            "run_id": run_id, "profile": args.profile,
            "pipes": str(len(pipes)), "status": "success",
        })
    else:
        status = ("partial_failure" if extract_failed
                  else ("history_error" if history_failed else "success"))
        _notify("run_failed", run_id, {
            "run_id": run_id, "profile": args.profile,
            "status": status, "exit_code": str(exit_code),
            "failed_pipes": ", ".join(sorted(set(failed_pipes)))
            if failed_pipes else "unknown (see log)",
        })

    log.info("wrapper done: run_id=%s profile=%s exit=%s%s",
             run_id, args.profile, exit_code,
             " (run `python -m wrapper.update_history` to repair history)"
             if exit_code == EXIT_HISTORY else "")
    return exit_code


def _settings_path_from_env() -> str:
    import os
    return os.environ.get("DPL_SETTINGS", SETTINGS_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
