"""scripts/email_acceptance.py - LOCAL acceptance for the email service.

Proves the whole chain with ZERO network: loads the REAL
config/email_service.yaml, sends one dry-run email through the
PUBLIC facade as the 'selftest' caller, asserts the .eml landed in
the outbox and the history row landed in a throwaway DB, then prints
a masked summary.

Deliberately REFUSES to run when email.dry_run is false: local
acceptance must never touch a socket. Live sends belong to the
office SMTP drill (pytest -m integration).

Exit codes (wrapper contract): 0 = ok | 2 = failure | 3 = config error.

Run:
    python scripts/email_acceptance.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dpl.lib.email_service.api import send_email
from dpl.lib.email_service.config import load_email_config
from dpl.lib.email_service.mask import mask_address
from dpl.lib.history_db import init_db, open_db

CONFIG_PATH = PROJECT_ROOT / "config" / "email_service.yaml"
CALLER = "selftest"


def main() -> int:
    if not CONFIG_PATH.is_file():
        print(f"CONFIG ERROR: {CONFIG_PATH} not found - place the "
              f"starter config first")
        return 3
    try:
        cfg = load_email_config(CONFIG_PATH)
    except Exception as exc:                             # noqa: BLE001
        print(f"CONFIG ERROR: {exc}")
        return 3
    if not cfg.server.dry_run:
        print("CONFIG ERROR: email.dry_run is FALSE - this local "
              "acceptance never touches the network. Flip dry_run to "
              "true, or use the office SMTP drill (pytest -m "
              "integration) for live sends.")
        return 3
    c = cfg.callers.get(CALLER)
    if c is None or not c.enabled:
        print(f"CONFIG ERROR: caller {CALLER!r} is missing or "
              f"disabled in {CONFIG_PATH}")
        return 3

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "acceptance.db"
        init_db(db)
        r = send_email(caller=CALLER, run_id="ACCEPTANCE",
                       config_path=CONFIG_PATH, db_path=db)

        if r.outcome != "dry_run" or not r.ok():
            print(f"FAILURE: outcome={r.outcome} "
                  f"error_class={r.error_class} "
                  f"summary={r.error_summary}")
            return 2

        eml = cfg.server.outbox_dir / f"{r.send_id}.eml"
        if not eml.is_file():
            print(f"FAILURE: .eml missing at {eml}")
            return 2

        conn = open_db(db)
        try:
            row = conn.execute(
                "SELECT outcome, error_class, finished_at "
                "FROM email_sends WHERE send_id = ?",
                (r.send_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            print("FAILURE: no history row for the dry-run send")
            return 2
        if row[0] != "dry_run" or row[1] != "none" or row[2] is None:
            print(f"FAILURE: bad history row "
                  f"(outcome={row[0]}, class={row[1]}, "
                  f"finished_at={row[2]})")
            return 2

        print("ACCEPTANCE PASS: dpl email service "
              "(local, no network)")
        print(f"  send_id:      {r.send_id}")
        print(f"  outcome:      {r.outcome}")
        print(f"  subject:      {r.subject}")
        print(f"  recipients:   "
              f"{', '.join(mask_address(a) for a in c.to)}")
        print(f"  created_at:   {r.created_at}")
        print(f"  finished_at:  {r.finished_at}")
        print(f"  .eml on disk: {eml}")
        print(f"  history row:  verified in throwaway DB {db.name} "
              f"(removed with the temp dir)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
