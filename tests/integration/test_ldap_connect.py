"""Integration tests for dpl.lib.ldap_connect (live Active Directory).

Provenance: these were the _smoke_* routines that lived at the bottom of
src/dpl/lib/ldap_connect.py, run manually via
`python -m dpl.lib.ldap_connect`. Moved here so test code never ships in
the production wheel.

UNLIKE tests/unit/, these tests are INTEGRATION tests: they open a real
NTLM bind to the live Active Directory using real vault credentials
resolved via the user's credential_ref (pass_mgr). They therefore:

  - never run as part of the routine suite or the green script
    (pyproject registers the marker and deselects it by default);
  - are run DELIBERATELY, on a machine with network access to AD and
    an initialized vault:   pytest -m integration -v
  - read the REAL config (config/extractors/ad_to_stg.yaml) - this is
    a live-fire verification, deliberately not hermetic.

Run with:  pytest tests/integration/test_ldap_connect.py -m integration -v
"""

from __future__ import annotations

import pytest

from dpl.lib.common import load_config
from dpl.lib.ldap_connect import LDAP_CONFIG_SCHEMA, connect, iter_paged
from dpl.lib.logger import setup_logging

CONFIG_FILE = "config/extractors/ad_to_stg.yaml"


@pytest.fixture(scope="module")
def ldap_cfg():
    """Load the real AD extractor config once for the whole module.

    Fails loudly (not skips) when the config is broken: if you
    deliberately asked for integration tests and they cannot even
    load config, that IS the finding.
    """
    setup_logging()
    cfg = load_config(CONFIG_FILE, LDAP_CONFIG_SCHEMA)
    if cfg is None:
        pytest.fail(
            f"integration prerequisite failed: {CONFIG_FILE} did not load/"
            f"validate. Fix the config before judging the connector."
        )
    return cfg


@pytest.mark.integration
def test_bind_and_whoami(ldap_cfg):
    """NTLM bind with vault-resolved password; server confirms identity."""
    name = next(iter(ldap_cfg["connections"]))
    with connect(ldap_cfg, name) as conn:
        whoami = conn.extend.standard.who_am_i()
        assert conn.bound
        assert whoami, "who_am_i returned empty - bind did not authenticate"


@pytest.mark.integration
def test_first_paged_entry(ldap_cfg):
    """Paged subtree search returns at least one entry (or reports empty)."""
    t = ldap_cfg["tables"][0]
    with connect(ldap_cfg, t["connection"]) as conn:
        it = iter_paged(
            conn, t["search_base"], t["filter"],
            ["objectGUID", "sAMAccountName"],
        )
        first = next(iter(it), None)
        if first is None:
            # The original smoke test treated this as a WARN, not a
            # failure: the bind and paging machinery are what's under
            # test; an empty result is a data/filter question.
            pytest.skip(
                "no entries returned - check filter/search_base "
                "(bind and paging worked)"
            )
        assert "sAMAccountName" in first or "objectGUID" in first, first
