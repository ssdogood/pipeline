# dpl/lib/connectors.py
"""Connector registry - the ONE map from a source type to its pieces.

run.py contains NO connector knowledge. Each registered pipe's yaml is
routed through HERE by its connections' shared 'type'. All per-connector
knowledge lives with the connectors:

    Add a PIPE (existing connector, e.g. another Oracle source):
        drop config/extractors/<name>.yaml and register <name> in
        config/run.yaml -> done. Nothing else changes anywhere.

    Add a CONNECTOR (new source type, e.g. postgres):
        1. lib/pg_connect.py       - the connector (schema + connect)
        2. extractors/pg_to_stg.py - the extractor (must export extract_all())
        3. ONE entry in CONNECTORS below.
        run.py and run.yaml NEVER change.

    Remove a connector: delete its files + its entry. A run.yaml entry
        still naming the removed type fails at wrapper startup with an
        explicit error - never a silent skip.

Naming conventions (derived by run.py, never configured per pipe):
    config/extractors/<name>.yaml  ->  config/extractors/schema_master_<name>.csv
"""
from __future__ import annotations

from dpl.lib.ldap_connect import LDAP_CONFIG_SCHEMA
from dpl.lib.ora_connect import ORACLE_CONFIG_SCHEMA

CONNECTORS: dict[str, dict] = {
    # schema    : validates config/extractors/<name>.yaml at load time
    # extractor : module exporting extract_all(); run.py imports it
    # generator : module exporting generate_schemas() for the
    #             generate_schemas step; None = master is curated by
    #             hand for this connector type
    "oracle": {
        "schema": ORACLE_CONFIG_SCHEMA,
        "extractor": "dpl.extractors.oracle_to_stg",
        "generator": "dpl.lib.generate_schemas",
    },
    "ldap": {
        "schema": LDAP_CONFIG_SCHEMA,
        "extractor": "dpl.extractors.ad_to_stg",
        "generator": None,   # AD master CSV is hand-curated for now
    },
}
