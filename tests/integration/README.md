# Integration tests

Reserved for tests that touch real systems: live Oracle connections,
Active Directory, and full pipeline runs end-to-end.

These tests are deliberately SEPARATE from tests/unit/:
- unit/    = offline, hermetic, safe to run any time (tmp_path only)
- integration/ = requires credentials and reachable systems;
  run deliberately, never as part of routine checks.

Until the first integration test exists, this folder intentionally
holds nothing but this README.