"""Unit tests for wrapper.run._notify - the email event seam.

Slice 1 (run-level events): _notify must (a) forward caller/run_id/data
to the public facade, and (b) NEVER raise, no matter how badly the
email stack misbehaves. Failure isolation is the contract.

_notify imports the facade lazily (`from dpl.lib.email_service import
send_email` INSIDE the function), so the attribute is resolved at call
time from the package module - monkeypatching the package attribute
therefore intercepts even the late import. That laziness is itself part
of the isolation design (a damaged email package must not kill the
wrapper at import time) and is why these tests patch the package, not
dpl.wrapper.run.

Run-level branch coverage (started vs completed vs failed selection)
is exercised by the dry-run end-to-end run, not here - driving
wrapper.main() in unit tests would drag in Oracle/AD/vault side
effects deliberately kept out of 'pytest -q'.
"""
from __future__ import annotations

import pytest

from dpl.lib import email_service
from dpl.wrapper.run import _notify


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch):
    """Swap the facade for a recorder; returns the calls it saw."""
    calls: list[dict[str, object]] = []

    def fake_send_email(caller, data=None, run_id=None, **_):
        calls.append({"caller": caller, "run_id": run_id,
                      "data": dict(data or {})})
        return object()   # opaque: _notify only reads outcome/send_id attrs

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    return calls


def test_notify_forwards_event_and_join_key(captured) -> None:
    _notify("run_failed", "RUN-XYZ",
            {"run_id": "RUN-XYZ", "status": "partial_failure",
             "exit_code": "4", "failed_pipes": "ora_main"})
    assert len(captured) == 1
    call = captured[0]
    assert call["caller"] == "run_failed"
    assert call["run_id"] == "RUN-XYZ"          # joins to runs.run_id
    assert call["data"]["failed_pipes"] == "ora_main"


def test_notify_swallows_facade_exception(
        captured, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_):
        raise RuntimeError("facade exploded")

    monkeypatch.setattr(email_service, "send_email", boom)
    _notify("run_completed", "RUN-ABC", {})     # must not raise


def test_notify_swallows_import_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Damaged-install simulation: the facade symbol is missing from
    # the package. The lazy import raises ImportError inside _notify,
    # which must swallow it (email is never fatal to the run).
    monkeypatch.delattr(email_service, "send_email", raising=False)
    _notify("run_started", "RUN-DEF", {})       # must not raise
