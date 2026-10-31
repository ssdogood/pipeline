# lib/ldap_connect.py
"""LDAP connection layer for Active Directory extraction.

Contract mirrors lib/ora_connect.py at the connection level:
    - NTLM bind, credentials read from secrets/ (UTF-8, stripped)
    - one shared connection per folder; lifecycle owned by the extractor
    - paged-search generator streams entries page by page (memory-bounded,
      same discipline as ora_connect.iter_batches)

Usage:
    python -m lib.ldap_connect          # connection smoke tests
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ldap3 import ALL, NTLM, SIMPLE, SUBTREE, Connection, Server

from dpl.lib.logger import get_run_logger, setup_logging

log = get_run_logger("dpl.ldap")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = PROJECT_ROOT / "secrets"
CONFIG_FILE = "config/extractors/ad_to_stg.yaml"
PAGE_SIZE_DEFAULT = 500
AUTH_MAP = {"ntlm": NTLM, "simple": SIMPLE}


def _read_secret(secret_file: str) -> str:
    """Read one secret (UTF-8, whitespace-stripped). Raise loudly if absent."""
    path = SECRETS_DIR / secret_file
    if not path.is_file():
        raise FileNotFoundError(f"secret file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


@contextmanager
def connect(cfg: dict, conn_name: str):
    """Open one LDAP connection (context-managed) and yield it bound.

    cfg: full extractor config dict (connections keyed by name).
    conn_name: entry in connections (e.g. ad_main).
    """
    conn_cfg = cfg["connections"][conn_name]
    uri = conn_cfg["uri"]
    username = conn_cfg["username"]
    auth_name = conn_cfg.get("auth", "ntlm").lower()

    password = _read_secret(conn_cfg["secret"])
    log.info("connecting to %s as %s (auth=%s)", uri, username, auth_name)

    server = Server(uri, get_info=ALL, connect_timeout=10)
    conn = Connection(
        server,
        user=username,
        password=password,
        authentication=AUTH_MAP[auth_name],
        auto_bind=True,
        receive_timeout=60,
    )
    try:
        if not conn.bound:
            raise ConnectionError(f"ldap bind failed: {conn_name}")
        log.info("connected: %s", conn.extend.standard.who_am_i())
        yield conn
    finally:
        conn.unbind()
        log.info("connection closed: %s", uri)


def iter_paged(conn: Connection, search_base: str, search_filter: str,
               attributes: list[str], page_size: int | None = None):
    """Yield entry attribute dicts from a paged subtree search.

    Generator — one page at a time, so memory stays bounded regardless of
    result-set size. Entry attribute values are lists for multi-valued
    attributes and scalars for single-valued ones (caller normalizes).
    """
    page_size = page_size or PAGE_SIZE_DEFAULT
    entries = conn.extend.standard.paged_search(
        search_base,
        search_filter,
        search_scope=SUBTREE,
        attributes=attributes,
        paged_size=page_size,
        generator=True,
    )
    for entry in entries:
        if entry.get("type") != "searchResEntry":
            continue  # skip searchResRef referrals
        yield entry["attributes"]


# ---------------------------------------------------------------- smoke --

def _smoke_missing_secret():
    try:
        _read_secret("__nope__.txt")
    except FileNotFoundError as e:
        print(f"[PASS] missing secret raises clean error ({e})")
        return
    raise AssertionError("missing secret did not raise")


def _smoke_connect(cfg: dict):
    name = next(iter(cfg["connections"]))
    with connect(cfg, name) as conn:
        whoami = conn.extend.standard.who_am_i()
        assert conn.bound
        print(f"[PASS] bind to {name}: {whoami}")


def _smoke_first_entry(cfg: dict):
    t = cfg["tables"][0]
    with connect(cfg, t["connection"]) as conn:
        it = iter_paged(conn, t["search_base"], t["filter"],
                        ["objectGUID", "sAMAccountName"])
        first = next(iter(it), None)
        if first is None:
            print("[WARN] no entries returned — check filter/search base")
        else:
            print(f"[PASS] first entry keys: {sorted(first.keys())}")


def main() -> int:
    setup_logging()
    from lib.common import load_config
    cfg = load_config(CONFIG_FILE)
    if cfg is None:
        print("[FAIL] config could not be loaded")
        return 3

    _smoke_missing_secret()
    _smoke_connect(cfg)
    _smoke_first_entry(cfg)
    print("all ldap_connect smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())