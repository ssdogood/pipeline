# dpl/lib/session_pool.py
"""Long-lived per-folder sessions, error classification, configurable retry.

Phase 2 of the parallel-extraction design (design v3, LOCKED). Absorbs and
upgrades the retry loop that lived in extractors:

  BEFORE (oracle_to_stg.extract_table):   one fresh connection per ATTEMPT,
      1 hardcoded retry, 0.5s fixed backoff, hardcoded retryable substrings.
  AFTER (this module, wired in Phase 4):  one long-lived connection per
      FOLDER (lazy open, reused across tables), retries/backoff configurable
      per connection (validated by extraction_config), and a session
      health gate: only SESSION-CLASS failures reset + retry the table;
      data-class failures mark the table failed and move on - today's
      semantics, unchanged.

Error classification - REVIEW TABLE (the table the operator signs off on):

    SESSION-CLASS (transport broke -> reset connection, retry same table):
        ORA-03113  end-of-file on communication channel
        ORA-03114  not connected to ORACLE
        ORA-03135  connection lost contact            (added vs v0.2)
        ORA-12170  TNS: connect timeout occurred
        ORA-12541  TNS: no listener
        ORA-12571  TNS: packet writer failed          (added vs v0.2)
        DPI-1080   oracledb: network connection was lost
        type match: ConnectionError family (reset/refused/aborted),
                    TimeoutError, EOFError
        substring : Network, Timeout, Timed out, Broken pipe,
                    End-of-file, ConnectionReset, ConnectionRefused,
                    LDAPSocketReceiveError, LDAPSocketSendError,
                    LDAPSocketOpenError

    DATA-CLASS (table is the problem -> NO retry, table fails, siblings
    continue): everything else - SQL/syntax errors, missing table/view,
    permission errors, cast errors, schema drift - and ALL UNKNOWN ERRORS.
    Deliberate: an unpredicted error at 2 a.m. must not become a silent
    retry storm; it fails one table loudly, exactly as today.

Everything here is offline-testable: factories inject connectors, sleepers
inject the clock. No module-level driver imports.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Self

from dpl.lib.logger import get_run_logger

log = get_run_logger("dpl.session_pool")

# --- classification -----------------------------------------------------------
# Substring list absorbs extractors' RETRYABLE_ERRORS verbatim (backward-
# compatible superset) plus the reviewed additions. String matching is
# required: oracledb raises DatabaseError for transport drops too - the
# ORA-/DPI- code in the message is the only discriminator.
SESSION_CLASS_SUBSTRINGS = (
    # Oracle transport / TNS
    "ORA-03113",          # end-of-file on communication channel
    "ORA-03114",          # not connected to ORACLE
    "ORA-03135",          # connection lost contact
    "ORA-12170",          # TNS: connect timeout
    "ORA-12541",          # TNS: no listener
    "ORA-12571",          # TNS: packet writer failed
    "DPI-1080",            # network connection was lost (thin-mode drop)
    # generic transport (matches yesterday's RETRYABLE_ERRORS set)
    "ConnectionReset",
    "ConnectionRefused",
    "Network",
    "Timeout",
    "Timed out",
    "Broken pipe",
    "End-of-file",
    # ldap3 socket layer
    "LDAPSocketReceiveError",
    "LDAPSocketSendError",
    "LDAPSocketOpenError",
)

# Exceptions whose TYPE alone means transport. ConnectionError covers
# ConnectionResetError/ConnectionRefusedError/ConnectionAbortedError.
# Deliberately NOT OSError (disk-full is a data-class problem).
SESSION_CLASS_TYPES = (ConnectionError, TimeoutError, EOFError)


def classify_error(exc: BaseException) -> str:
    """'session' (transport broke: reset+retry may help) or 'data'.

    Pure function; substring match on the message plus exact-type match.
    Unknown -> 'data' (no retry) - today's behavior for unpredicted errors.
    """
    if isinstance(exc, SESSION_CLASS_TYPES):
        return "session"
    msg = str(exc).replace("\n", " ")
    for needle in SESSION_CLASS_SUBSTRINGS:
        if needle in msg:
            return "session"
    return "data"


# --- FolderSession -------------------------------------------------------------

class FolderSession:
    """One long-lived connection shared by a folder's tables.

    The connector's own context manager owns open/close discipline (the
    architectural boundary: session_pool never imports drivers) - we hold
    it OPEN between tables, which is the entire point. Lazy: no connection
    until the first open(), so folders queued behind the parallelism cap
    hold nothing. reset() closes best-effort and drops the reference; the
    next open() reconnects. close() = reset(); as a context manager the
    session ALWAYS releases its connection on exit, even if the table
    loop exploded mid-run.

    connect_factory: zero-arg callable returning the connector's context
    manager (e.g. partial(ora_connect.connect, cfg, "ora_db")). Wire the
    keepalive through the factory (see ora_connect.connect expire_time).
    """

    def __init__(self, connect_factory: Callable[[], AbstractContextManager],
                 label: str = "-") -> None:
        self._factory = connect_factory
        self._label = label
        self._cm: AbstractContextManager | None = None
        self.conn: Any = None
        self.opens = 0

    # ------------------------------------------------------------- lifecycle
    def open(self) -> Any:
        """Return the live connection, opening it if this is first use."""
        if self.conn is not None:
            return self.conn
        log.info("[%s] opening session (open #%d)", self._label, self.opens + 1)
        self._cm = self._factory()
        self.conn = self._cm.__enter__()
        self.opens += 1
        return self.conn

    def reset(self) -> None:
        """Best-effort close, then forget. Next open() reconnects.

        Best-effort on purpose: by the time we reset, the transport is
        usually already dead - a secondary error while closing a corpse
        must never mask why we are here.
        """
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception as exc:                    # noqa: BLE001 - teardown of a possibly-dead transport; the triggering failure is the story, not this
                log.debug("[%s] close during reset raised (ignored): %s",
                          self._label, exc)
        self._cm = None
        self.conn = None

    def close(self) -> None:
        self.reset()

    # ------------------------------------------------------ context manager
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


# --- retry loop -----------------------------------------------------------------

def run_with_retry(
    session: FolderSession,
    attempt_fn: Callable[[Any], Any],
    retries: int = 2,
    backoff_seconds: int = 5,
    classify: Callable[[BaseException], str] = classify_error,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Any, int]:
    """Run attempt_fn(conn); on session-class failure reset + retry.

    attempt_fn MUST be fresh per call (new writer, new generator) - the
    write_parquet contract: "a retry is simply a new call".

    Returns (result, attempts). RAISES the last exception when:
      - the error is data-class (no retry, attempts == 1), or
      - session-class but retries exhausted (attempts == retries + 1).
    The caller records the failure in the manifest, exactly as today.

    A failure of session.open() itself (server momentarily down) is
    naturally session-class (ConnectionRefusedError family) and retried
    by the same loop - connect attempts count as attempts.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return attempt_fn(session.open()), attempts
        except Exception as exc:
            if classify(exc) != "session":
                raise
            if attempts > retries:
                log.error("[%s] session-class failure persisted after %d "
                          "attempt(s): %s", session._label, attempts, exc)
                raise
            log.warning("[%s] session-class failure (attempt %d/%d), "
                        "resetting session and retrying in %ss: %s",
                        session._label, attempts, retries + 1,
                        backoff_seconds, str(exc)[:300])
            session.reset()
            if backoff_seconds:
                sleeper(backoff_seconds)
