"""Unit tests for email_service result.py + composer.py (stage 4c),
amended for finished_at (ratified 2026-11-11).

Runs under pytest AND standalone:
    python tests/unit/test_email_compose.py

Pure-local: no SMTP, no vault, no network. ServerConfig/CallerConfig
are built directly (frozen dataclasses) with template files created in
tmp_path - no YAML involved here (config parsing is stage 4a's tested
territory).

Fixture note: pytest's builtin is tmp_path; the `tmp` bridge fixture
below hands it to every test under the shorter name.

finished_at doctrine under test:
  - dry_run (terminal)        -> stamped, canonical UTC-Z
  - compose failures (terminal)-> stamped, canonical UTC-Z
  - composed (NOT terminal)   -> None; transport stamps it later

Group map:
  RESULT - schema lockstep + canonical created_at
  HDRS   - header discipline (From/To/Cc/Subject/BCC, custom headers)
  BODY   - rendering, reserved stamping, HTML subtype, subject rules
  FAIL   - every failure mode and its error_class
  ATTACH - whitelist, existence, size cap, content fidelity
  DRYRUN - outbox .eml writing, terminal result, write failure
  FINSIH - finished_at stamping rules
"""
from __future__ import annotations

import email
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from dpl.lib.email_service.composer import compose_email
from dpl.lib.email_service.config import CallerConfig, ServerConfig
from dpl.lib.email_service.result import (
    EC_BAD_ATTACHMENT,
    EC_BAD_SUBJECT,
    EC_BAD_TEMPLATE,
    EC_INTERNAL,
    EC_NONE,
    EC_OUTBOX,
    OUTCOME_COMPOSED,
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    OUTCOME_SENT,
    SendResult,
)
from dpl.lib.timefmt import require_utc_z

TPL = ("Report: ${title}\nStatus: ${status}\n"
       "Caller: ${caller}\nRef ${send_id} at ${generated_at}\n")


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """Bridge: tests below use `tmp` for readability."""
    return tmp_path


# ----------------------------------------------------------------- helpers --

def make_server(tmp: Path, **over: object) -> ServerConfig:
    base: dict[str, object] = {
        "host": "smtp.example.org", "port": 465, "mode": "ssl",
        "auth_scheme": "basic", "username_ref": "smtp/username",
        "password_ref": "smtp/password",
        "sender": "dpl-pipeline@example.org",
        "allow_html": False, "allow_bcc": True, "allowed_domains": (),
        "attach_extensions": ("csv", "xlsx", "log", "txt", "parquet"),
        "max_attach_mb": 10.0, "retries": 2, "timeout_sec": 30.0,
        "dry_run": False,
        "templates_dir": tmp / "templates",
        "outbox_dir": tmp / "outbox",
    }
    base.update(over)
    return ServerConfig(**base)          # type: ignore[arg-type]


def make_caller(tmp: Path, template: str = TPL, **over: object
                ) -> CallerConfig:
    tdir = tmp / "templates"
    tdir.mkdir(exist_ok=True)
    tfile = tdir / "tpl.txt"
    tfile.write_text(template, encoding="utf-8")
    base: dict[str, object] = {
        "name": "daily_report", "enabled": True,
        "to": ("ops@example.org",), "cc": (), "bcc": (),
        "template": "tpl.txt", "template_path": tfile,
        "placeholders": (), "use_html": False, "data": {},
        "attachments": (),
    }
    base.update(over)
    return CallerConfig(**base)          # type: ignore[arg-type]


GOOD_DATA = {"subject": "[dpl] daily report",
             "title": "Daily Run", "status": "success"}


# ----------------------------------------------------------------- RESULT ---

def test_send_result_history_row_lockstep(tmp: Path) -> None:
    r = SendResult(
        send_id="S-1", caller="c", created_at="2026-11-11T09:30:00Z",
        mode="live", outcome=OUTCOME_SENT, error_class=EC_NONE,
        attempts_used=1, recipient_count=2, subject="s",
        error_summary=None, run_id="R", message=None,
        envelope_recipients=(), finished_at="2026-11-11T09:30:31Z")
    assert frozenset(r.to_history_row()) == SendResult.row_keys()
    assert r.ok() is True


def test_failed_result_not_ok(tmp: Path) -> None:
    r = SendResult(
        send_id="S-1", caller="c", created_at="2026-11-11T09:30:00Z",
        mode="live", outcome=OUTCOME_FAILED, error_class=EC_INTERNAL,
        attempts_used=0, recipient_count=1, subject="",
        error_summary="boom", run_id="-", message=None,
        envelope_recipients=(), finished_at="2026-11-11T09:30:00Z")
    assert r.ok() is False


def test_finished_at_defaults_to_none(tmp: Path) -> None:
    r = SendResult(
        send_id="S-1", caller="c", created_at="2026-11-11T09:30:00Z",
        mode="live", outcome=OUTCOME_SENT, error_class=EC_NONE,
        attempts_used=1, recipient_count=1, subject="s",
        error_summary=None, run_id="-", message=None,
        envelope_recipients=())
    assert r.finished_at is None, "optional field defaults None"
    assert r.to_history_row()["finished_at"] is None


# ------------------------------------------------------------------- HDRS ---

def test_compose_success_headers_and_envelope(tmp: Path) -> None:
    server = make_server(tmp)
    caller = make_caller(tmp, cc=("lead@example.org",),
                         bcc=("auditor@example.org",))
    r = compose_email(server, caller, GOOD_DATA)
    assert r.outcome == OUTCOME_COMPOSED
    msg = r.message
    assert msg is not None
    assert msg["From"] == "dpl-pipeline@example.org"
    assert "ops@example.org" in msg["To"]
    assert "lead@example.org" in msg["Cc"]
    assert msg["Bcc"] is None, "BCC must never appear in headers"
    assert "auditor@example.org" in r.envelope_recipients
    assert r.recipient_count == 3
    assert r.error_class == EC_NONE and r.error_summary is None


def test_custom_headers_present(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp), GOOD_DATA)
    msg = r.message
    assert msg is not None
    assert msg["X-dpl-Send-ID"] == r.send_id
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Date"] is not None


def test_subject_sanitized_no_control_chars(tmp: Path) -> None:
    data = {**GOOD_DATA, "subject": "OK\r\nBcc: evil@attacker.net"}
    r = compose_email(make_server(tmp), make_caller(tmp), data)
    msg = r.message
    assert msg is not None
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert "Bcc:" in r.subject, "text preserved, only control chars die"


# ------------------------------------------------------------------- BODY ---

def test_body_has_reserved_stamps(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp), GOOD_DATA)
    msg = r.message
    assert msg is not None
    body = msg.get_content()
    assert r.send_id in body
    assert r.created_at in body, "generated_at stamped in canonical form"
    assert "daily_report" in body, "caller name stamped via ${caller}"
    require_utc_z(r.created_at)


def test_html_subtype_when_requested(tmp: Path) -> None:
    server = make_server(tmp, allow_html=True)
    caller = make_caller(tmp, use_html=True)
    r = compose_email(server, caller, GOOD_DATA)
    msg = r.message
    assert msg is not None
    assert msg.get_content_subtype() == "html"


def test_config_data_merged_under_call_data(tmp: Path) -> None:
    caller = make_caller(tmp, data={"status": "from-config",
                                    "subject": "config subject"})
    call_data = {"status": "from-call", "title": "T"}
    r = compose_email(make_server(tmp), caller, call_data)
    msg = r.message
    assert msg is not None
    body = msg.get_content()
    assert "from-call" in body, "send-call data wins over config data"
    assert r.subject == "config subject", "untouched keys pass through"


# ------------------------------------------------------------------- FAIL ---

def test_missing_subject_fails(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp),
                      {"title": "x", "status": "y"})
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_BAD_SUBJECT
    assert r.message is None
    assert r.attempts_used == 0


def test_non_scalar_subject_fails(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp),
                      {"subject": ["a"], "title": "x", "status": "y"})
    assert r.error_class in (EC_BAD_SUBJECT, EC_BAD_TEMPLATE)


def test_template_missing_key_fails_naming_key(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp),
                      {"subject": "s"})          # title/status missing
    assert r.error_class == EC_BAD_TEMPLATE
    assert "title" in (r.error_summary or "") \
        and "status" in (r.error_summary or "")


def test_reserved_collision_via_call_data_fails(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp),
                      {**GOOD_DATA, "send_id": "FAKE"})
    assert r.error_class == EC_BAD_TEMPLATE
    assert "reserved" in (r.error_summary or "").lower()


def test_non_mapping_data_never_raises(tmp: Path) -> None:
    r = compose_email(                   # type: ignore[arg-type]
        make_server(tmp), make_caller(tmp), None)
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_INTERNAL
    assert r.message is None, "no half-built message escapes"


# ----------------------------------------------------------------- ATTACH ---

def _attach(tmp: Path, name: str, size: int = 8) -> Path:
    d = tmp / "att"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * size)
    return f


def test_attachments_attached_with_filenames(tmp: Path) -> None:
    a1 = _attach(tmp, "rows.csv")
    a2 = _attach(tmp, "notes.txt", size=50)
    caller = make_caller(tmp, attachments=(str(a1), str(a2)))
    r = compose_email(make_server(tmp), caller, GOOD_DATA)
    msg = r.message
    assert msg is not None
    parts = list(msg.iter_attachments())
    names = {p.get_filename() for p in parts}
    assert names == {"rows.csv", "notes.txt"}
    csv_part = next(p for p in parts if p.get_filename() == "rows.csv")
    assert csv_part.get_payload(decode=True) == b"x" * 8


def test_attachment_bad_extension_fails_loudly(tmp: Path) -> None:
    exe = _attach(tmp, "tool.exe")
    caller = make_caller(tmp, attachments=(str(exe),))
    r = compose_email(make_server(tmp), caller, GOOD_DATA)
    assert r.error_class == EC_BAD_ATTACHMENT
    assert "whitelist" in (r.error_summary or "")


def test_attachment_missing_file_fails(tmp: Path) -> None:
    caller = make_caller(tmp, attachments=(str(tmp / "ghost.csv"),))
    r = compose_email(make_server(tmp), caller, GOOD_DATA)
    assert r.error_class == EC_BAD_ATTACHMENT
    assert "not found" in (r.error_summary or "")


def test_attachment_size_cap_fails(tmp: Path) -> None:
    big = _attach(tmp, "big.csv", size=2048)
    server = make_server(tmp, max_attach_mb=0.001)   # ~1 KB cap
    caller = make_caller(tmp, attachments=(str(big),))
    r = compose_email(server, caller, GOOD_DATA)
    assert r.error_class == EC_BAD_ATTACHMENT
    assert "cap" in (r.error_summary or "")


def test_custom_whitelist_respected(tmp: Path) -> None:
    txt = _attach(tmp, "notes.txt", size=16)
    server = make_server(tmp, attach_extensions=("csv",))
    caller = make_caller(tmp, attachments=(str(txt),))
    r = compose_email(server, caller, GOOD_DATA)
    assert r.error_class == EC_BAD_ATTACHMENT


# ----------------------------------------------------------------- DRYRUN ---

def test_dry_run_writes_eml_and_terminal_result(tmp: Path) -> None:
    server = make_server(tmp, dry_run=True)
    r = compose_email(server, make_caller(tmp), GOOD_DATA)
    assert r.outcome == OUTCOME_DRY_RUN
    assert r.mode == "dry_run"
    assert r.message is None, "nothing to send in dry-run"
    eml = server.outbox_dir / f"{r.send_id}.eml"
    assert eml.is_file()
    blob = eml.read_bytes()
    assert b"Subject: [dpl] daily report" in blob
    assert b"auditor@" not in blob, "base caller has no BCC; sanity guard"


def test_dry_run_eml_opens_as_mime(tmp: Path) -> None:
    server = make_server(tmp, dry_run=True)
    caller = make_caller(tmp, cc=("lead@example.org",),
                         bcc=("auditor@example.org",))
    r = compose_email(server, caller, GOOD_DATA)
    eml = server.outbox_dir / f"{r.send_id}.eml"
    parsed = email.message_from_bytes(eml.read_bytes())
    assert parsed["Subject"] == "[dpl] daily report"
    assert parsed["Bcc"] is None, "even the .eml must hide BCC"
    body = parsed.get_payload(decode=True)
    assert isinstance(body, bytes), "decoded payload is raw bytes"
    assert b"Daily Run" in body


def test_dry_run_outbox_failure_reported(tmp: Path) -> None:
    blocker = tmp / "blocker"
    blocker.write_text("i am a file where a dir should be",
                        encoding="utf-8")
    server = make_server(tmp, dry_run=True, outbox_dir=blocker)
    r = compose_email(server, make_caller(tmp), GOOD_DATA)
    assert r.outcome == OUTCOME_FAILED
    assert r.error_class == EC_OUTBOX


def test_live_compose_returns_composed_with_message(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp), GOOD_DATA)
    assert r.outcome == OUTCOME_COMPOSED
    assert r.mode == "live"
    assert r.message is not None


# --------------------------------------------------------------- FINISHED ---

def test_dry_run_stamps_finished_at(tmp: Path) -> None:
    server = make_server(tmp, dry_run=True)
    r = compose_email(server, make_caller(tmp), GOOD_DATA)
    assert r.finished_at is not None, "dry_run is terminal: stamped"
    require_utc_z(r.finished_at)


def test_compose_failure_stamps_finished_at(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp),
                      {"subject": "s"})          # missing keys
    assert r.outcome == OUTCOME_FAILED
    assert r.finished_at is not None, "failure is terminal: stamped"
    require_utc_z(r.finished_at)


def test_composed_result_finished_at_is_none(tmp: Path) -> None:
    r = compose_email(make_server(tmp), make_caller(tmp), GOOD_DATA)
    assert r.outcome == OUTCOME_COMPOSED
    assert r.finished_at is None, "composed is NOT terminal yet"


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests: list[tuple[str, Callable[[Path], object]]] = [
        ("send_result_history_row_lockstep",
         test_send_result_history_row_lockstep),
        ("failed_result_not_ok", test_failed_result_not_ok),
        ("finished_at_defaults_to_none", test_finished_at_defaults_to_none),
        ("compose_success_headers_and_envelope",
         test_compose_success_headers_and_envelope),
        ("custom_headers_present", test_custom_headers_present),
        ("subject_sanitized_no_control_chars",
         test_subject_sanitized_no_control_chars),
        ("body_has_reserved_stamps", test_body_has_reserved_stamps),
        ("html_subtype_when_requested", test_html_subtype_when_requested),
        ("config_data_merged_under_call_data",
         test_config_data_merged_under_call_data),
        ("missing_subject_fails", test_missing_subject_fails),
        ("non_scalar_subject_fails", test_non_scalar_subject_fails),
        ("template_missing_key_fails_naming_key",
         test_template_missing_key_fails_naming_key),
        ("reserved_collision_via_call_data_fails",
         test_reserved_collision_via_call_data_fails),
        ("non_mapping_data_never_raises", test_non_mapping_data_never_raises),
        ("attachments_attached_with_filenames",
         test_attachments_attached_with_filenames),
        ("attachment_bad_extension_fails_loudly",
         test_attachment_bad_extension_fails_loudly),
        ("attachment_missing_file_fails", test_attachment_missing_file_fails),
        ("attachment_size_cap_fails", test_attachment_size_cap_fails),
        ("custom_whitelist_respected", test_custom_whitelist_respected),
        ("dry_run_writes_eml_and_terminal_result",
         test_dry_run_writes_eml_and_terminal_result),
        ("dry_run_eml_opens_as_mime", test_dry_run_eml_opens_as_mime),
        ("dry_run_outbox_failure_reported",
         test_dry_run_outbox_failure_reported),
        ("live_compose_returns_composed_with_message",
         test_live_compose_returns_composed_with_message),
        ("dry_run_stamps_finished_at", test_dry_run_stamps_finished_at),
        ("compose_failure_stamps_finished_at",
         test_compose_failure_stamps_finished_at),
        ("composed_result_finished_at_is_none",
         test_composed_result_finished_at_is_none),
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for name, fn in tests:
            try:
                fn(Path(td))
                print(f"  PASS {name}")
            except Exception as exc:        # noqa: BLE001 - runner sentinel
                failures.append(name)
                print(f"  FAIL {name}: {exc}")
    total, bad = len(tests), len(failures)
    if bad:
        print(f"RESULT: {total - bad}/{total} passed, FAILED: {failures}")
        return 1
    print(f"RESULT: ALL GREEN ({total}/{total}) - "
          f"stage 4c+finished_at verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
