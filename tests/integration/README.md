# Integration tests

Reserved for tests that touch real systems: live Oracle connections,
Active Directory, and full pipeline runs end-to-end.

These tests are deliberately SEPARATE from tests/unit/:

- unit/    = offline, hermetic, safe to run any time (tmp_path only)
- integration/ = requires credentials and reachable systems;
  run deliberately, never as part of routine checks.

Environment note (PT-046 follow-up): integration tests resolve their
config from the ACTIVE environment - the folder named by
config/environment.yaml. They read real values from that environment
(extractor yamls, schema masters, settings) and hit real systems.
Run them where you mean to test: the selector decides what the test
sees.

The integration tests below exist and run deliberately:
  pytest tests/integration -m integration -v
