"""Facade tests: the send_email() contract (acceptance slice) +
the asymmetric credential rule (config, direct) + the literal-
username transport seam.

Covers the ratified behaviors:
  - disabled caller -> terminal skipped_disabled WITH history row
  - unknown caller / bad config -> structured bad_caller/bad_config
  - dry-run end-to-end: real config file -> .eml on disk -> history
    row in a real (throwaway) SQLite DB via the real facade
  - live config hands off to transport
  - literal username (public config) flows through to smtp.login();
    password still vault-only

Configs are built as dicts and dumped with yaml.safe_dump - never
hand-formatted YAML (the stage-4a lesson: serialize, don't paste).

Standing checklist: pytest's builtin is tmp_path; the `tmp` bridge
fixture below is REQUIRED in every new pytest file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dpl.lib import pass_mgr
from dpl.lib.email_service import transport
from dpl.lib.email_service.api import send_email
from dpl.lib.email_service.config import (
    ConfigError,
    load_email_config,
)
from dpl.lib.email_service.result import (
    EC_BAD_CALLER,
    EC_BAD_CONFIG,
    EC_NONE,
    OUTCOME_COMPOSED,
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
)
from dpl.lib.history_db import init_db, open_db


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """Bridge (builtin is tmp_path)."""
    return tmp_path


# ---------------------------------------------------------------- helpers --

def _server(tmp: Path, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "host": "smtp.test.invalid", "port": 465, "mode": "ssl",
        "auth": {"scheme": "basic", "username": "unit-user",
                 "password_ref": "smtp.password.secret"},
        "sender": "dpl@test.invalid",
        "allow_html": False, "allow_bcc": False,
        "allowed_recipient_domains": [],
        "attachments": {
            "allowed_extensions": ["csv", "xlsx", "log", "txt",
                                    "parquet"],
            "max_total_mb": 10},
        "retries": 2, "timeout_sec": 30,
        "dry_run": True,
        "templates_dir": str(tmp / "templates"),
        "outbox_dir": str(tmp / "outbox"),
    }
    base.update(over)
    return base


def _caller(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True, "to": ["ops@example.org"], "cc": [],
        "bcc": [], "template": "selftest.txt", "use_html": False,
        "data": {"subject": "[dpl] api test"}, "attachments": [],
    }
    base.update(over)
    return base


def write_config(tmp: Path,
                 server_over: dict[str, object] | None = None,
                 callers: dict[str, object] | None = None) -> Path:
    """Dict-built, yaml.safe_dumped config with a real template file."""
    tdir = tmp / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / "selftest.txt").write_text("dpl selftest\n",
                                       encoding="utf-8")
    cfg = {"email": _server(tmp, **(server_over or {})),
           "callers": callers or {"selftest": _caller()}}
    path = tmp / "email.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False),
                    encoding="utf-8")
    return path


def fresh_db(tmp: Path) -> Path:
    db = tmp / "hist.db"
    init_db(db)
    return db


def read_row(db: Path, send_id: str
             ) -> tuple[object, object, object, object] | None:
    """(send_id, outcome, error_class, finished_at) or None."""
    conn = open_db(db)
    try:
        return conn.execute(
            "SELECT send_id, outcome, error_class, finished_at "
            "FROM email_sends WHERE send_id = ?", (send_id,)
        ).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------- facade contract --

def test_unknown_caller_is_structured_bad_caller(tmp: Path) -> None:
    path = write_config(tmp)
    db = fresh_db(tmp)
    r = send_email(caller="nope", config_path=path, db_path=db)
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_BAD_CALLER
    assert r.ok() is False
    assert "nope" in (r.error_summary or "")
    row = read_row(db, r.send_id)
    assert row is not None, "bad caller still records history"
    assert row[1] == "failed" and row[2] == EC_BAD_CALLER
    assert row[3] is not None, "finished_at stamped (terminal)"


def test_disabled_caller_skipped_with_history_row(
        tmp: Path) -> None:
    path = write_config(
        tmp, callers={"selftest": _caller(enabled=False)})
    db = fresh_db(tmp)
    r = send_email(caller="selftest", config_path=path, db_path=db)
    assert r.outcome == OUTCOME_SKIPPED
    assert r.error_class == "disabled"
    assert r.ok() is False, "asked for a send, did not get one"
    row = read_row(db, r.send_id)
    assert row is not None, "skipped MUST be auditable (ratified)"
    assert row[1] == "skipped_disabled"
    assert row[2] == "disabled"
    assert row[3] is not None


def test_dry_run_end_to_end_eml_and_row(tmp: Path) -> None:
    path = write_config(tmp)                 # dry_run: true
    db = fresh_db(tmp)
    r = send_email(caller="selftest", config_path=path, db_path=db)
    assert r.outcome == OUTCOME_DRY_RUN
    assert r.ok() is True
    eml = tmp / "outbox" / f"{r.send_id}.eml"
    assert eml.is_file(), "dry-run must drop a real .eml"
    row = read_row(db, r.send_id)
    assert row is not None
    assert row[1] == "dry_run" and row[2] == EC_NONE
    assert row[3] is not None


def test_live_config_hands_off_to_transport(
        tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp, server_over={"dry_run": False})
    captured: list[object] = []

    def fake_deliver(server: object, composed: object,
                     db_path: object = None,
                     backoff_sec: float = 30.0) -> object:
        captured.append(composed)
        return composed

    from dpl.lib.email_service import api as api_module
    monkeypatch.setattr(api_module, "deliver_email", fake_deliver)
    r = send_email(caller="selftest", config_path=path,
                   db_path=fresh_db(tmp))
    assert len(captured) == 1
    assert r.outcome == OUTCOME_COMPOSED, "transport owns finalization"


def test_malformed_config_is_bad_config(tmp: Path) -> None:
    path = write_config(
        tmp, server_over={"auth": {"scheme": "basic", "username": "u",
                                    "password": "hunter2"}})
    r = send_email(caller="selftest", config_path=path,
                   db_path=fresh_db(tmp))
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_BAD_CONFIG
    assert r.ok() is False


# ------------------------------------------------ asymmetric credential rule --

def test_literal_password_is_load_error(tmp: Path) -> None:
    path = write_config(
        tmp, server_over={"auth": {"scheme": "basic",
                                   "username": "u",
                                   "password": "hunter2"}})
    with pytest.raises(ConfigError):
        load_email_config(path)


def test_literal_username_with_password_ref_loads(tmp: Path) -> None:
    path = write_config(tmp)        # default server: username literal
    cfg = load_email_config(path)
    assert cfg.server.username == "unit-user"
    assert cfg.server.username_ref is None
    assert cfg.server.password_ref == "smtp.password.secret"


def test_basic_without_any_identity_is_load_error(
        tmp: Path) -> None:
    path = write_config(
        tmp, server_over={"auth": {
            "scheme": "basic",
            "password_ref": "smtp.password.secret"}})
    with pytest.raises(ConfigError):
        load_email_config(path)


def test_basic_with_both_identity_is_load_error(
        tmp: Path) -> None:
    path = write_config(
        tmp, server_over={"auth": {
            "scheme": "basic", "username": "u",
            "username_ref": "smtp.user",
            "password_ref": "smtp.password.secret"}})
    with pytest.raises(ConfigError):
        load_email_config(path)


def test_username_ref_only_loads(tmp: Path) -> None:
    path = write_config(
        tmp, server_over={"auth": {
            "scheme": "basic", "username_ref": "smtp.user",
            "password_ref": "smtp.password.secret"}})
    cfg = load_email_config(path)
    assert cfg.server.username is None
    assert cfg.server.username_ref == "smtp.user"


# -------------------------------------------- literal-username transport seam --

def test_live_send_logs_in_with_literal_username(
        tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp, server_over={"dry_run": False})
    seen: dict[str, tuple[str, ...]] = {}

    class FakeSMTP:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def starttls(self, **kw: object) -> None:
            pass

        def login(self, user: str, pw: str) -> None:
            seen["creds"] = (user, pw)

        def send_message(self, msg: object, from_addr: str,
                         to_addrs: list[str]) -> None:
            seen["to"] = tuple(to_addrs)

        def quit(self) -> None:
            pass

    monkeypatch.setattr(pass_mgr, "get_secret",
                        lambda ref: "unit-pw")
    monkeypatch.setattr(transport.smtplib, "SMTP_SSL", FakeSMTP)
    r = send_email(caller="selftest", config_path=path,
                   db_path=fresh_db(tmp))
    assert r.outcome == OUTCOME_SENT
    assert r.attempts_used == 1
    creds = seen.get("creds")
    assert creds is not None
    assert creds[0] == "unit-user", "literal username reaches login"
    assert creds[1] == "unit-pw", "password always from vault"


# -------------------------------------------------------- never-raises proof --

def test_facade_never_raises_on_none_caller(tmp: Path) -> None:
    path = write_config(tmp)
    r = send_email(caller=None,             # type: ignore[arg-type]
                   config_path=path, db_path=fresh_db(tmp))
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_BAD_CALLER
