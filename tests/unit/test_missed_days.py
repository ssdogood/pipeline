"""B7 unit tests: missed-day detection (pure module, born strict)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest

from dpl.lib.missed_days import (
    MAX_LOOKBACK_DAYS,
    last_success_by_folder,
    missed_days_for_connection,
)


class TestLastSuccessByFolder:
    def test_fold_takes_max_per_folder(self) -> None:
        rows: list[tuple[str, str | None]] = [
            ("stg", "2026-11-20"),
            ("stg", "2026-11-24"),
            ("stg", "2026-11-22"),
            ("ad", "2026-11-21"),
        ]
        got = last_success_by_folder(rows)
        assert got == {"stg": date(2026, 11, 24), "ad": date(2026, 11, 21)}

    def test_null_dates_ignored(self) -> None:
        rows: list[tuple[str, str | None]] = [("stg", None), ("stg", "2026-11-19")]
        assert last_success_by_folder(rows) == {"stg": date(2026, 11, 19)}

    def test_all_null_yields_empty(self) -> None:
        assert last_success_by_folder([("stg", None)]) == {}

    def test_empty_rows(self) -> None:
        assert last_success_by_folder([]) == {}


class TestMissedDaysForConnection:
    SAT_BLOCK: ClassVar[dict[str, object]] = {"on": ["sat"]}
    TUE_SAT: ClassVar[dict[str, object]] = {"except": ["sun", "mon"]}

    def test_gap_spanning_due_days_detected(self) -> None:
        # last success Tue 24 Nov 2026; today Thu 26 Nov
        # due days after: Wed 25 (except sun/mon -> due), Thu excluded(today)
        today = date(2026, 11, 26)
        got = missed_days_for_connection(self.TUE_SAT, date(2026, 11, 24), today)
        assert got is not None
        assert got["missed"] == ["2026-11-25"]
        assert got["truncated"] is False

    def test_non_due_only_gap_not_flagged(self) -> None:
        # sun/mon-only gap: last success Fri 20 Nov, today Tue 24 Nov
        # Sat 21 was due -> wait; use SAT_BLOCK with a weekend-only gap
        # last success Sat 21 Nov; today Sat 28 Nov; gap days sun-fri all
        # non-due for SAT_BLOCK -> nothing missed
        got = missed_days_for_connection(
            self.SAT_BLOCK, date(2026, 11, 21), date(2026, 11, 28)
        )
        assert got is None

    def test_today_never_a_miss(self) -> None:
        # last success yesterday; today is due but unextracted so far
        got = missed_days_for_connection(
            self.SAT_BLOCK, date(2026, 11, 21), date(2026, 11, 28)
        )
        # today (Sat 28) excluded from sweep by construction
        assert got is None

    def test_never_ran_reports_nothing(self) -> None:
        assert (
            missed_days_for_connection(self.SAT_BLOCK, None, date(2026, 11, 28)) is None
        )

    def test_cap_truncates_honestly(self) -> None:
        # last success 100 days ago: sweep capped at MAX_LOOKBACK_DAYS
        today = date(2026, 11, 28)
        last = today.replace() if False else date(2026, 8, 20)  # 100 days
        got = missed_days_for_connection({"on": ["sat"]}, last, today)
        assert got is not None
        assert got["truncated"] is True
        expected_from = (today - MAX_LOOKBACK_DAYS_OFFSET).isoformat()
        assert got["from"] == expected_from

    def test_every_day_schedule_gap_all_missed(self) -> None:
        got = missed_days_for_connection(None, date(2026, 11, 24), date(2026, 11, 26))
        # None block = due every day; sweep 25 Nov only (26 = today)
        assert got is not None
        assert got["missed"] == ["2026-11-25"]

    def test_zero_gap(self) -> None:
        # last success today -> sweep empty
        got = missed_days_for_connection(None, date(2026, 11, 26), date(2026, 11, 26))
        assert got is None


MAX_LOOKBACK_DAYS_OFFSET = __import__("datetime").timedelta(days=MAX_LOOKBACK_DAYS)


@pytest.mark.parametrize(
    "block,expected",
    [
        ({"on": ["sat"]}, True),
        ({"except": ["sun"]}, True),
        (None, True),
    ],
)
def test_is_due_shim(block: object, expected: bool) -> None:
    from dpl.lib.missed_days import is_due_connection_schedule

    # Saturday 2026-11-28: on:[sat] -> due; except:[sun] -> due; None -> due
    assert is_due_connection_schedule(block, date(2026, 11, 28)) is expected


class TestPayloadAggregation:
    def test_one_email_per_run_across_folders(self) -> None:
        from dpl.lib.missed_days import missed_day_payload

        envelopes: dict[str, dict[str, object]] = {
            "stg": {"missed_days": ["2026-11-25"]},
            "ad": {
                "missed_days": ["2026-11-24", "2026-11-25"],
                "missed_days_truncated": True,
                "missed_days_from": "2026-10-29",
            },
        }
        payload = missed_day_payload(envelopes)
        assert payload is not None
        assert payload["missed_count"] == 3
        assert payload["missed_folders"] == "2"
        lines = str(payload["missed_lines"])
        assert "stg: 1 missed" in lines
        assert "truncated from 2026-10-29" in lines

    def test_no_misses_no_payload(self) -> None:
        from dpl.lib.missed_days import missed_day_payload

        empty: dict[str, dict[str, object]] = {"stg": {"missed_days": []}}
        assert missed_day_payload(empty) is None
        assert missed_day_payload({}) is None

    def test_fetch_last_success_missing_db_is_grace(self) -> None:
        from dpl.lib.missed_days import fetch_last_success

        # nonexistent path -> {} (first-run grace, never raises)
        assert fetch_last_success(Path("/nonexistent/dpl_history.db")) == {}

    def test_fetch_last_success_reads_history(self, tmp_path: Path) -> None:
        import sqlite3

        from dpl.lib.history_db import init_db
        from dpl.lib.missed_days import fetch_last_success

        db = tmp_path / "h.db"
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO folder_runs (run_id, folder, load_date, status)"
            " VALUES ('r1', 'stg', '2026-11-24', 'success')"
        )
        conn.execute(
            "INSERT INTO folder_runs (run_id, folder, load_date, status)"
            " VALUES ('r2', 'stg', '2026-11-22', 'failed')"
        )
        conn.commit()
        conn.close()
        got = fetch_last_success(db)
        # failed rows never count as successes
        assert got == {"stg": date(2026, 11, 24)}
