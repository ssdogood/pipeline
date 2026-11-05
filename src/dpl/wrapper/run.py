# dpl/wrapper/run.py
"""Wrapper entry point: python -m dpl.wrapper.run [--profile NAME] [--tables a,b]

Runs from project root only (relative config paths, like every other
module). No args -> profile 'full'.

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

import yaml

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
    validate_secrets_layout,
)
from dpl.lib.connectors import CONNECTORS
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrapper.run",
        description="DPL orchestration wrapper (default profile: full)")
    parser.add_argument("--profile", default="full",
                        help="profile name from wrapper.yaml (default: full)")
    parser.add_argument("--tables", default=None,
                        help="comma-separated table filter (testing/targeted)")
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
    log.info("registered pipes: %s",
             ", ".join(f"{p.name}[{p.source_type}]" for p in pipes))

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
        log.error("history db unreachable: %s - continuing without history",
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
                # One call per REGISTERED pipe that has a generator;
                # pipes without one (hand-curated masters) are skipped
                # via the registry, not by branching here.
                for pipe in pipes:
                    gen_path = pipe.connector.get("generator")
                    if not gen_path:
                        continue
                    gen_mod = importlib.import_module(gen_path)
                    log.info("generate_schemas: pipe=%s", pipe.name)
                    rc = gen_mod.generate_schemas(
                        config_path=pipe.path,
                        output_path=pipe.master,
                        force=step.get("force", True),
                        dry_run=False,
                        settings_path=settings_path)
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
                # Every REGISTERED pipe, in run.yaml order. Failures mark
                # the run but never stop the remaining pipes.
                for pipe in pipes:
                    try:
                        cfg = load_config(pipe.path, pipe.connector["schema"])
                        if cfg is None:
                            log.error("pipe %s: config failed validation "
                                      "(%s)", pipe.name, pipe.path)
                            extract_failed = True
                            continue

                        ext_mod = importlib.import_module(
                            pipe.connector["extractor"])
                        manifests_src, any_failed = ext_mod.extract_all(
                            cfg, settings, load_date,
                            force=step.get("force", False),
                            only_tables=only_tables,
                            run_id=run_id,
                            profile=args.profile)
                        manifests.update(manifests_src)   # FIXED: merge -
                        # previously collected but never merged, so the
                        # envelope-rewrite loop below ran empty and the
                        # wrapper-state stamp never landed in manifests.
                        if any_failed:
                            extract_failed = True
                    except Exception as exc:
                        log.error("pipe %s failed: %s", pipe.name, exc,
                                  exc_info=True)
                        extract_failed = True

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

        except Exception as exc:                    # noqa: BLE001 - deliberate
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
        except (sqlite3.Error, OSError, ValueError,
                json.JSONDecodeError) as exc:
            log.error("post-run history ingest failed: %s - repair with "
                       "`python -m wrapper.update_history`", exc)
            history_failed = True
            exit_code = EXIT_HISTORY

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
