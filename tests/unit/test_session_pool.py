# tests/unit/test_session_pool.py
"""Unit tests for session_pool: classification, lifecycle, retry loop.

Offline/hermetic per doctrine: a fake connector factory plays the driver,
an injected sleeper plays the clock. No network, no oracledb import.
"""
from __future__ import annotations

import pytest

from dpl.lib.session_pool import (
    FolderSession,
    classify_error,
    run_with_retry,
)

# ------------------------------------------------------------------ fakes ---

class _FakeCM:
    """Context manager playing one connector connection."""

    def __init__(self, open_error: BaseException | None = None):
        self.open_error = open_error
        self.conn = object()
        self.exited = False

    def __enter__(self):
        if self.open_error is not None:
            err, self.open_error = self.open_error, None
            raise err
        return self.conn

    def __exit__(self, *exc) -> bool:
        self.exited = True
        return False


def _factory(open_errors: list[BaseException | None] | None = None):
    """Zero-arg factory; the nth open raises open_errors[n-1] if given."""
    state = {"calls": 0}
    created: list[_FakeCM] = []

    def make() -> _FakeCM:
        n = state["calls"]
        state["calls"] += 1
        err = (open_errors[n] if open_errors and n < len(open_errors)
               else None)
        cm = _FakeCM(err)
        created.append(cm)
        return cm

    return make, created


def _refused(msg: str = "DP: ConnectionRefusedError") -> ConnectionRefusedError:
    return ConnectionRefusedError(msg)


# --------------------------------------------------------- classification ---

@pytest.mark.parametrize("msg", [
    "ORA-03113: end-of-file on communication channel",
    "ORA-03114: not connected to ORACLE",
    "ORA-03135: connection lost contact",
    "ORA-12170: TNS:Connect timeout occurred",
    "ORA-12541: TNS:no listener",
    "ORA-12571: TNS:packet writer failed",
    "DPI-1080: network connection was lost",
    "LDAPSocketReceiveError - socket connection lost",
    "LDAPS send error: Broken pipe",
])
def test_substring_matches_are_session_class(msg):
    assert classify_error(RuntimeError(msg)) == "session"


@pytest.mark.parametrize("exc", [
    ConnectionResetError(),
    ConnectionRefusedError("refused"),
    TimeoutError("timed out"),
    EOFError("end of file"),
])
def test_type_matches_are_session_class(exc):
    assert classify_error(exc) == "session"


@pytest.mark.parametrize("msg", [
    "ORA-00942: table or view does not exist",
    "ORA-01017: invalid username/password; logon denied",
    "ORA-01722: invalid number",
    "schema mismatch: master says ['a'], query returns ['b']",
])
def test_known_data_errors_are_data_class(msg):
    assert classify_error(RuntimeError(msg)) == "data"


def test_unknown_error_is_data_class():
    assert classify_error(RuntimeError("something never seen before")) == "data"


def test_realistic_data_error_is_data_class():
    # substring matching is a heuristic: a data error that HAPPENS to
    # contain a transport word ("Timeout") misclassifies as session -
    # accepted trade-off, cost is one wasted retry then a hard failure.
    # Real data errors, as raised in practice, stay data-class:
    assert classify_error(TypeError("cannot convert Decimal to Timestamp")) == "data"


# --------------------------------------------------------------- lifecycle ---

def test_lazy_open_creates_exactly_one_connection():
    make, created = _factory()
    s = FolderSession(make)
    assert created == []                      # nothing before open
    s.open()
    s.open()
    assert len(created) == 1
    assert s.opens == 1


def test_reset_closes_and_reopens():
    make, created = _factory()
    s = FolderSession(make)
    first = s.open()
    s.reset()
    assert created[0].exited                   # old connection was closed
    assert s.conn is None
    second = s.open()
    assert len(created) == 2
    assert first is not second


def test_context_manager_exit_closes_even_after_error():
    make, created = _factory()
    try:
        with FolderSession(make) as s:
            s.open()
            raise ValueError("table blew up mid-run")
    except ValueError:
        pass
    assert created[0].exited                  # no leaked session


def test_reset_survives_rude_connector_close():
    # close during reset raises; reset must swallow and still forget
    class _Rude(_FakeCM):
        def __exit__(self, *exc):
            raise RuntimeError("already dead")

    def rude():
        return _Rude()

    s = FolderSession(rude)
    s.open()
    s.reset()                                 # must not raise
    assert s.conn is None


# -------------------------------------------------------------- retry loop ---

def _recorder():
    calls: list[float] = []
    return (lambda seconds: calls.append(seconds)), calls


def test_retry_succeeds_second_attempt():
    make, _ = _factory()
    s = FolderSession(make, label="t")
    sleep, slept = _recorder()
    attempts_done = []

    def attempt(conn):
        attempts_done.append(conn)
        if len(attempts_done) == 1:
            raise ConnectionResetError("ORA-03113: end-of-file")
        return "rows"

    result, attempts = run_with_retry(
        s, attempt, retries=2, backoff_seconds=5, sleeper=sleep)
    assert result == "rows"
    assert attempts == 2
    assert slept == [5]


def test_session_reset_between_attempts():
    make, created = _factory()
    s = FolderSession(make)
    sleep, _ = _recorder()

    calls = {"n": 0}

    def attempt(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("ORA-03113: end-of-file")
        return "ok"

    run_with_retry(s, attempt, retries=2, backoff_seconds=0, sleeper=sleep)
    assert len(created) == 2                 # reset reopened a new session
    assert created[0].exited


def test_data_class_never_retries_and_keeps_session():
    make, created = _factory()
    s = FolderSession(make)
    sleep, slept = _recorder()
    conn_before = s.open()

    def attempt(conn):
        raise RuntimeError("ORA-00942: table or view does not exist")

    with pytest.raises(RuntimeError):
        run_with_retry(s, attempt, retries=2, backoff_seconds=5,
                       sleeper=sleep)
    assert slept == []                        # no backoff was taken
    assert s.conn is conn_before              # session NOT reset - it's healthy
    assert len(created) == 1


def test_session_class_exhausts_retries_then_raises():
    make, _ = _factory()
    s = FolderSession(make)
    sleep, slept = _recorder()

    def attempt(conn):
        raise ConnectionResetError("ORA-03135: connection lost contact")

    with pytest.raises(ConnectionResetError):
        run_with_retry(s, attempt, retries=2, backoff_seconds=3,
                       sleeper=sleep)
    assert slept == [3, 3]                    # two backoffs, three attempts


def test_zero_retries_means_single_attempt():
    make, _ = _factory()
    s = FolderSession(make)
    sleep, slept = _recorder()
    n = {"i": 0}

    def attempt(conn):
        n["i"] += 1
        raise ConnectionResetError("network blip")

    with pytest.raises(ConnectionResetError):
        run_with_retry(s, attempt, retries=0, backoff_seconds=5,
                       sleeper=sleep)
    assert n["i"] == 1
    assert slept == []


def test_open_failure_is_retried_as_session_class():
    # first open refused (server restarting), second succeeds
    make, created = _factory(open_errors=[_refused()])
    s = FolderSession(make)
    sleep, _slept = _recorder()

    def attempt(conn):
        return "rows"

    result, attempts = run_with_retry(
        s, attempt, retries=2, backoff_seconds=1, sleeper=sleep)
    assert result == "rows"
    assert attempts == 2
    assert len(created) == 2


def test_retry_after_reset_receives_fresh_connection():
    # health gate: a session-class failure RESETS the session, so the
    # retried attempt must run on a newly opened connection.
    make, _created = _factory()
    s = FolderSession(make)
    seen = []

    def attempt(conn):
        seen.append(conn)
        if len(seen) == 1:
            raise TimeoutError("temporary")
        return "ok"

    result, attempts = run_with_retry(s, attempt, retries=1, backoff_seconds=0,
                                      sleeper=lambda _s: None)
    assert result == "ok" and attempts == 2
    assert seen[0] is not seen[1]          # fresh connection after reset
    assert seen[1] is s.conn               # and it is the live session's
