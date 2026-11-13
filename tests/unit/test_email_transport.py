"""Unit tests for email_service transport.py (stage 4d),
amended for finished_at (ratified 2026-11-11).

Every SMTP and vault touchpoint is MOCKED: no network, no real
server, no real credentials, no real database. Tests patch the
smtplib constructors the transport uses and/or _credentials /
record_email_send.

pytest-only (mocking with monkeypatch/caplog reads best there); no
standalone runner for this file.

The classifier map is tested against REAL smtplib exception
instances - that map is the product feature of this slice. Key
regression: test_5xx_response_is_rejected pins the branch order -
smtplib exceptions are OSErrors, so a bare 5xx response must be
classified BEFORE the generic OSError fallback can call it transient.

Helper loading: pyrefly's import root is src/, and tests/ is not a
package, so `from tests.unit...` is unresolvable to static checkers
(same finding as scripts/init_history_db in stage 2). Solution per
the stage-2 precedent: load the compose test kit BY FILE PATH via
importlib - runtime and static checking both stay happy.

Standing checklist item: every new pytest file that wants `tmp`
MUST define the bridge fixture below (pytest's builtin is tmp_path).
Note: socket.timeout is an alias of TimeoutError since 3.10 - one
test covers both identities.
"""
from __future__ import annotations

import importlib.util
import logging
import smtplib
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dpl.lib.email_service import transport
from dpl.lib.email_service.composer import compose_email
from dpl.lib.email_service.config import ServerConfig
from dpl.lib.email_service.result import (
    EC_AUTH_FAILED,
    EC_INTERNAL,
    EC_NONE,
    EC_REJECTED,
    EC_TRANSIENT,
    OUTCOME_COMPOSED,
    OUTCOME_FAILED,
    OUTCOME_SENT,
    SendResult,
)
from dpl.lib.email_service.transport import (
    PLAIN_WARN,
    TransportFailure,
    classify_error,
    deliver_email,
)
from dpl.lib.timefmt import require_utc_z

# --- load the 4c test kit (make_server/make_caller/GOOD_DATA) by path ---

_spec = importlib.util.spec_from_file_location(
    "_compose_test_kit",
    Path(__file__).with_name("test_email_compose.py"))
assert _spec is not None and _spec.loader is not None
_kit: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_kit)
make_server = _kit.make_server
make_caller = _kit.make_caller
GOOD_DATA = _kit.GOOD_DATA


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """Bridge: tests use `tmp` for readability (builtin is tmp_path)."""
    return tmp_path


# ---------------------------------------------------------------- helpers --

def compose(tmp: Path, **server_over: object
            ) -> tuple[SendResult, ServerConfig]:
    """A genuinely composed message from the real composer."""
    server = make_server(tmp, **server_over)
    r = compose_email(server, make_caller(tmp), GOOD_DATA)
    assert r.outcome == OUTCOME_COMPOSED
    assert r.message is not None
    return r, server


class FakeSMTP:
    """Configurable SMTP stand-in; records what it saw."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, outcome: Exception | None = None,
                 login_should_fail: bool = False) -> None:
        self.outcome = outcome
        self.login_should_fail = login_should_fail
        self.logged_in = False
        self.sent_to: list[str] | None = None
        self.quits = 0
        FakeSMTP.instances.append(self)

    def starttls(self, **kw: object) -> None:
        pass

    def login(self, user: str, pw: str) -> None:
        self.logged_in = True
        if self.login_should_fail:
            raise smtplib.SMTPAuthenticationError(535, b"no")

    def send_message(self, msg: object, from_addr: str,
                     to_addrs: list[str]) -> None:
        if self.outcome is not None:
            raise self.outcome
        self.sent_to = list(to_addrs)

    def quit(self) -> None:
        self.quits += 1


def patch_smtp(monkeypatch: pytest.MonkeyPatch,
               outcomes: list[Exception | None]) -> None:
    """Replace both smtplib constructors with a FakeSMTP factory that
    yields the given outcomes in sequence (None = clean send)."""
    FakeSMTP.instances = []
    queue = list(outcomes)

    def factory(*args: object, **kwargs: object) -> FakeSMTP:
        nxt = queue.pop(0) if queue else None
        return FakeSMTP(outcome=nxt)

    monkeypatch.setattr(transport.smtplib, "SMTP", factory)
    monkeypatch.setattr(transport.smtplib, "SMTP_SSL", factory)


@pytest.fixture
def stub_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_credentials",
                        lambda server: ("unit-user", "unit-pw"))


@pytest.fixture
def stub_history(monkeypatch: pytest.MonkeyPatch
                 ) -> dict[str, object]:
    """Capture history writes; tests assert on the recorded row."""
    recorded: dict[str, object] = {}
    monkeypatch.setattr(transport, "record_email_send",
                        lambda row, db_path=None: recorded.update(
                            row=row, db_path=db_path))
    return recorded


# ---------------------------------------------------------------- classify --

class TestClassifier:
    """classify_error exhaustively, with real exception instances."""

    def test_auth_is_nontransient(self) -> None:
        tf = classify_error(smtplib.SMTPAuthenticationError(535, b"no"))
        assert (tf.error_class, tf.transient) == (EC_AUTH_FAILED, False)

    def test_sender_refused_nontransient(self) -> None:
        tf = classify_error(smtplib.SMTPSenderRefused(550, b"no",
                                                       "a@b.c"))
        assert (tf.error_class, tf.transient) == (EC_REJECTED, False)

    def test_recipients_refused_nontransient(self) -> None:
        errs = {"x@y.z": (550, b"no user")}
        tf = classify_error(smtplib.SMTPRecipientsRefused(errs))
        assert (tf.error_class, tf.transient) == (EC_REJECTED, False)

    def test_data_error_nontransient(self) -> None:
        tf = classify_error(smtplib.SMTPDataError(552, b"too big"))
        assert (tf.error_class, tf.transient) == (EC_REJECTED, False)

    def test_helo_error_nontransient(self) -> None:
        tf = classify_error(smtplib.SMTPHeloError(550, b"helo no"))
        assert (tf.error_class, tf.transient) == (EC_REJECTED, False)

    def test_4xx_response_transient(self) -> None:
        tf = classify_error(smtplib.SMTPResponseException(421, b"busy"))
        assert (tf.error_class, tf.transient) == (EC_TRANSIENT, True)

    def test_5xx_response_is_rejected(self) -> None:
        # REGRESSION: smtplib exceptions ARE OSErrors; without the
        # explicit branch before the OSError fallback this was
        # misclassified transient (found in review run 2026-11-11)
        tf = classify_error(smtplib.SMTPResponseException(501, b"no"))
        assert (tf.error_class, tf.transient) == (EC_REJECTED, False)

    def test_connect_error_transient(self) -> None:
        tf = classify_error(smtplib.SMTPConnectError(421, b"refused"))
        assert (tf.error_class, tf.transient) == (EC_TRANSIENT, True)

    def test_timeout_transient(self) -> None:
        # TimeoutError covers socket.timeout too (alias since 3.10)
        tf = classify_error(TimeoutError("slow"))
        assert (tf.error_class, tf.transient) == (EC_TRANSIENT, True)

    def test_connreset_transient(self) -> None:
        tf = classify_error(ConnectionResetError())
        assert (tf.error_class, tf.transient) == (EC_TRANSIENT, True)

    def test_econnreset_errno_transient(self) -> None:
        exc = OSError()
        exc.errno = 54          # macOS ECONNRESET; map is errno-based
        tf = classify_error(exc)
        assert (tf.error_class, tf.transient) == (EC_TRANSIENT, True)

    def test_epipe_errno_transient(self) -> None:
        exc = OSError()
        exc.errno = 32          # EPIPE
        tf = classify_error(exc)
        assert tf.transient is True

    def test_unexpected_internal_nontransient(self) -> None:
        tf = classify_error(RuntimeError("surprise"))
        assert (tf.error_class, tf.transient) == (EC_INTERNAL, False)


# ----------------------------------------------------------------- deliver --

class TestDeliver:
    def test_clean_send(self, tmp: Path, monkeypatch, stub_creds,
                        stub_history) -> None:
        composed, server = compose(tmp)
        patch_smtp(monkeypatch, [None])
        r = deliver_email(server, composed, db_path=Path("/tmp/x.db"),
                          backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        assert (r.error_class, r.attempts_used) == (EC_NONE, 1)
        inst = FakeSMTP.instances[0]
        assert inst.logged_in is True
        assert inst.sent_to == list(composed.envelope_recipients)
        assert inst.quits == 1

    def test_transient_retry_then_success(self, tmp: Path, monkeypatch,
                                          stub_creds, stub_history) -> None:
        composed, server = compose(tmp)       # retries=2 by default
        err = smtplib.SMTPResponseException(421, b"busy")
        patch_smtp(monkeypatch, [err, None])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        assert r.attempts_used == 2           # 1 fail + 1 success

    def test_retries_exhausted_failed(self, tmp: Path, monkeypatch,
                                      stub_creds, stub_history) -> None:
        composed, server = compose(tmp)       # retries=2 -> 3 attempts
        err = smtplib.SMTPResponseException(421, b"down")
        patch_smtp(monkeypatch, [err, err, err])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        assert (r.error_class, r.attempts_used) == (EC_TRANSIENT, 3)

    def test_auth_failure_fails_fast_no_retry(self, tmp: Path,
                                              monkeypatch, stub_creds,
                                              stub_history) -> None:
        composed, server = compose(tmp)
        err = smtplib.SMTPAuthenticationError(535, b"no")
        patch_smtp(monkeypatch, [err, err, err, err])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        assert (r.error_class, r.attempts_used) == (EC_AUTH_FAILED, 1)

    def test_5xx_refusal_not_retried(self, tmp: Path, monkeypatch,
                                     stub_creds, stub_history) -> None:
        # wire-level twin of the classifier regression test
        composed, server = compose(tmp)
        err = smtplib.SMTPResponseException(501, b"no")
        patch_smtp(monkeypatch, [err, err, err, err])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        assert (r.error_class, r.attempts_used) == (EC_REJECTED, 1)

    def test_auth_none_skips_login(self, tmp: Path, monkeypatch,
                                   stub_creds, stub_history) -> None:
        composed, server = compose(tmp, auth_scheme="none")
        patch_smtp(monkeypatch, [None])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        inst = FakeSMTP.instances[0]
        assert inst.logged_in is False, "auth_scheme none must not login"

    def test_starttls_selected_when_mode_starttls(self, tmp: Path,
                                                  monkeypatch, stub_creds,
                                                  stub_history) -> None:
        composed, server = compose(tmp, mode="starttls", port=587)
        seen_tls: dict[str, bool] = {"tls": False}

        class TLSFake(FakeSMTP):
            def starttls(self, **kw: object) -> None:
                seen_tls["tls"] = True

        FakeSMTP.instances = []

        def factory(*a: object, **kw: object) -> FakeSMTP:
            return TLSFake()

        monkeypatch.setattr(transport.smtplib, "SMTP", factory)
        monkeypatch.setattr(transport.smtplib, "SMTP_SSL", factory)
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        assert seen_tls["tls"] is True

    def test_incomplete_compose_is_internal(self, tmp: Path, monkeypatch,
                                            stub_creds,
                                            stub_history) -> None:
        composed, server = compose(tmp)
        broken = SendResult(
            send_id=composed.send_id, caller=composed.caller,
            created_at=composed.created_at, mode=composed.mode,
            outcome=OUTCOME_COMPOSED, error_class=EC_NONE,
            attempts_used=0, recipient_count=1, subject="s",
            error_summary=None, run_id="-", message=None,
            envelope_recipients=())
        r = deliver_email(server, broken, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        assert r.error_class == EC_INTERNAL


# ------------------------------------------------------------ vault seams --

class TestVault:
    def test_vault_outage_is_internal_not_auth(self, tmp: Path,
                                               monkeypatch,
                                               stub_history) -> None:
        def boom(server: ServerConfig) -> tuple[str, str]:
            raise TransportFailure(EC_INTERNAL, "vault down",
                                   transient=False)

        monkeypatch.setattr(transport, "_credentials", boom)
        composed, server = compose(tmp)
        r = deliver_email(server, composed, backoff_sec=0)
        assert (r.outcome, r.error_class) == (OUTCOME_FAILED,
                                              EC_INTERNAL)
        assert r.attempts_used == 0, "no wire attempt after vault fault"

    def test_basic_scheme_requires_refs(self, tmp: Path) -> None:
        # config validation makes this unreachable; transport is loud
        server = make_server(tmp, auth_scheme="basic",
                             username_ref=None, password_ref=None)
        with pytest.raises(TransportFailure):
            transport._credentials(server)


# ------------------------------------------------------------ history seam --

class TestHistoryWiring:
    def test_row_contents_on_sent(self, tmp: Path, monkeypatch,
                                  stub_creds,
                                  stub_history) -> None:
        composed, server = compose(tmp)
        patch_smtp(monkeypatch, [None])
        dbp = Path("/tmp/unit-hist.db")
        r = deliver_email(server, composed, db_path=dbp, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        row = stub_history["row"]
        assert isinstance(row, dict)
        assert row["outcome"] == "sent"
        assert row["send_id"] == r.send_id
        assert row["error_class"] == "none"
        assert row["finished_at"] is not None, "terminal: stamped"
        require_utc_z(str(row["finished_at"]))
        assert stub_history["db_path"] == dbp

    def test_row_written_on_failure_too(self, tmp: Path, monkeypatch,
                                        stub_creds, stub_history) -> None:
        composed, server = compose(tmp)
        err = smtplib.SMTPAuthenticationError(535, b"no")
        patch_smtp(monkeypatch, [err])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        row = stub_history["row"]
        assert isinstance(row, dict)
        assert row["error_class"] == EC_AUTH_FAILED
        assert row["finished_at"] is not None, "failure is terminal"
        require_utc_z(str(row["finished_at"]))

    def test_pretransport_fault_stamps_finished_at(
            self, tmp: Path, monkeypatch, stub_history) -> None:
        def boom(server: ServerConfig) -> tuple[str, str]:
            raise TransportFailure(EC_INTERNAL, "vault down",
                                   transient=False)

        monkeypatch.setattr(transport, "_credentials", boom)
        composed, server = compose(tmp)
        assert composed.finished_at is None   # composed: not terminal
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_FAILED
        assert r.finished_at is not None, "pre-transport fault is terminal"
        require_utc_z(r.finished_at)

    def test_db_failure_does_not_demote_sent(self, tmp: Path,
                                             monkeypatch, stub_creds
                                             ) -> None:
        def boom(row: dict, db_path: Path | None = None) -> None:
            raise RuntimeError("db locked")

        monkeypatch.setattr(transport, "record_email_send", boom)
        composed, server = compose(tmp)
        patch_smtp(monkeypatch, [None])
        r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT, \
            "DB failure must never demote a successful send"


# ---------------------------------------------------------- plain WARNING --

class TestPlainModeWarning:
    def test_warns_once_per_send_despite_retry(
            self, tmp: Path, monkeypatch, stub_creds, stub_history,
            caplog: pytest.LogCaptureFixture) -> None:
        composed, server = compose(tmp, mode="plain", port=25)
        err = smtplib.SMTPResponseException(421, b"busy")
        patch_smtp(monkeypatch, [err, None])
        with caplog.at_level(logging.WARNING,
                             logger="dpl.email.transport"):
            r = deliver_email(server, composed, backoff_sec=0)
        assert r.outcome == OUTCOME_SENT
        plain_warnings = [rec for rec in caplog.records
                          if PLAIN_WARN in rec.getMessage()]
        assert len(plain_warnings) == 1, \
            "exactly one WARNING per send, retry must not repeat it"
