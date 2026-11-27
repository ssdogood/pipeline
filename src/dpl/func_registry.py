"""Functionality registry - single index of modules carrying ``__func_msg__``.

Pure data: this module imports nothing, executes nothing. Adding a
capability means adding its path here AND an ``__func_msg__`` block in
the module itself. The gate-time sweep test (tests/unit/test_cli.py)
enforces both directions of that pairing - a registry entry without a
matching ``__func_msg__`` fails the suite, and a module carrying an
``__func_msg__`` that is not registered here also fails the suite.

Paths are relative to ``src/dpl/`` and always use POSIX separators.
"""

MODULES = (
    "wrapper/run.py",
    "wrapper/update_history.py",
    "lib/generate_schemas.py",
    "lib/connectors.py",
    "lib/ora_connect.py",
    "lib/ldap_connect.py",
    "lib/pass_mgr/__init__.py",
    "lib/stg_keys.py",
    "lib/email_service/__init__.py",
    "lib/pause.py",
    "lib/pause_digest.py",
    "lib/history_db.py",
    "lib/manifest_compare.py",
    "lib/session_pool.py",
    "lib/write_parquet.py",
    "lib/extraction_config.py",
    "lib/schedule.py",
    "lib/asof.py",
    "lib/missed_days.py",
    "lib/schema_dispatch.py",
    "lib/folder_runner.py",
    "extractors/oracle_to_stg.py",
    "extractors/ad_to_stg.py",
)