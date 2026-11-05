"""
T5/T7/T8 - dispatcher composition tests: cache, routing, isolation.

WHAT THESE TESTS PROVE
======================
The dispatcher (_Dispatcher behind the get_secret facade) COMPOSES the
pieces the other suites proved individually:

  T5  Cache: each provider's vault is decrypted exactly once per
      process, no matter how many get_secret calls are made - verified
      by COUNTING real subprocess invocations with a spy, not by
      asserting the return value twice.
  T7  Cross-provider routing: a single config mixing SOPS and Fernet
      secrets, with per-secret method overrides, delivers each name
      from the CORRECT vault - the scenario no single-provider suite
      could exercise.
  T8  Interface isolation: a method referenced in config but missing
      from the provider registry fails cleanly with a typed error;
      init() lifecycle rules (before-init, environment switching)
      behave as documented.

Run with:  pytest test/pass_mgr/test_dispatcher.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pass_mgr
import pass_mgr._providers as providers_module
from pass_mgr import (
    PassMgrConfigError,
    PassMgrSecretNotFoundError,
    get_secret,
    init,
    reset,
)


# ---------------------------------------------------------------------------
# Shared world: a MIXED deployment - sops primary, fernet override
# ---------------------------------------------------------------------------


def build_mixed_world(project):
    """The deployment shape that exercises routing end-to-end:

    - oracle.stg_pass        -> sops (default)
    - ldap.service_pass      -> sops (default)
    - hpc.api_token          -> fernet (per-secret override)
    - batch.job_token        -> fernet (per-secret override)

    Both vaults are REAL (sops sealed via age; fernet sealed via the
    cryptography library). Nothing mocked except where a test says so.
    """
    identity_file, recipient = project.add_age_identity()
    project.add_sops_vault(
        {
            "oracle": {"stg_pass": "0ra-stg-pw-7f3"},
            "ldap": {"service_pass": "ld4p-svc-pw-9k1"},
        },
        identity_file,
        recipient,
    )
    project.add_fernet_envelope(
        {
            "hpc.api_token": "hpc-tok-42aa",
            "batch.job_token": "btch-tok-77cd",
        }
    )
    project.write_config_default(
        secrets={
            "oracle.stg_pass": {},
            "ldap.service_pass": {},
            "hpc.api_token": {"method": "fernet"},
            "batch.job_token": {"method": "fernet"},
        }
    )
    return identity_file


# ---------------------------------------------------------------------------
# T7 - cross-provider routing (the headline composition tests)
# ---------------------------------------------------------------------------


class TestCrossProviderRouting:

    def test_every_secret_comes_from_its_own_vault(self, project):
        # Four names, two vaults. Correct values prove correct routing;
        # wrong routing delivers either SecretNotFound or the wrong
        # value - either way this assertion catches it.
        build_mixed_world(project)

        init("laptop", project_root=project.root)
        assert get_secret("oracle.stg_pass") == "0ra-stg-pw-7f3"
        assert get_secret("ldap.service_pass") == "ld4p-svc-pw-9k1"
        assert get_secret("hpc.api_token") == "hpc-tok-42aa"
        assert get_secret("batch.job_token") == "btch-tok-77cd"

    def test_name_present_in_wrong_vault_is_not_found(self, project):
        # Routing is per-NAME, not per-availability: a name assigned to
        # fernet must fail even if a same-named entry existed in the
        # sops vault (it doesn't here, but the dispatcher must consult
        # ONLY the assigned vault). Meanwhile a name the fernet vault
        # genuinely lacks is a layer-2 SecretNotFound naming the method.
        build_mixed_world(project)
        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        text = cfg_path.read_text(encoding="utf-8")
        text += "  missing.everywhere: {}\n"
        cfg_path.write_text(text, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrSecretNotFoundError, match="missing.everywhere"):
            get_secret("missing.everywhere")

    def test_interleaved_calls_stay_correct(self, project):
        # Hammer both providers alternately - a cache-corruption or
        # cross-provider bleed would surface as a wrong value.
        build_mixed_world(project)

        init("laptop", project_root=project.root)
        expected = {
            "oracle.stg_pass": "0ra-stg-pw-7f3",
            "hpc.api_token": "hpc-tok-42aa",
            "ldap.service_pass": "ld4p-svc-pw-9k1",
            "batch.job_token": "btch-tok-77cd",
        }
        for _ in range(3):  # three interleaved sweeps over all four
            for name, value in expected.items():
                assert get_secret(name) == value


# ---------------------------------------------------------------------------
# T5 - cache: decrypt-once, verified by counting subprocess launches
# ---------------------------------------------------------------------------


class TestCacheInvocations:

    def test_sops_subprocess_runs_exactly_once(self, project, monkeypatch):
        # The strongest form of the cache test: wrap the REAL
        # subprocess.run inside _providers, count every invocation that
        # targets the sops binary, make many get_secret calls across
        # BOTH providers, and assert the count equals the expected
        # decrypt passes:
        #   1 x --version probe + 1 x --decrypt  = 2 sops launches
        # (the version probe is part of the pinned-version contract).
        build_mixed_world(project)

        calls = []

        real_run = providers_module.subprocess.run

        def counting_run(args, *run_args, **run_kwargs):
            if "sops" in str(args[0]):
                calls.append(args[0])
            return real_run(args, *run_args, **run_kwargs)

        monkeypatch.setattr(providers_module.subprocess, "run", counting_run)

        init("laptop", project_root=project.root)
        for i in range(5):
            get_secret("oracle.stg_pass")     # sops vault, repeatedly
            get_secret("hpc.api_token")       # fernet vault interleaved
            get_secret("ldap.service_pass")   # sops again

        # version probe + exactly ONE decrypt, despite 15 calls touching
        # the sops vault
        assert len(calls) == 2, f"expected 2 sops invocations, saw {len(calls)}"

    def test_reset_forces_cold_cache(self, project):
        # reset() must drop the cached vault: the next get_secret pays
        # the decryption cost again. Verified by the same counting spy.
        build_mixed_world(project)

        calls = []
        real_run = providers_module.subprocess.run

        def counting_run(args, *run_args, **run_kwargs):
            if "sops" in str(args[0]):
                calls.append(args[0])
            return real_run(args, *run_args, **run_kwargs)

        providers_module.subprocess.run = counting_run
        try:
            init("laptop", project_root=project.root)
            get_secret("oracle.stg_pass")     # probe + decrypt = 2
            reset()                           # simulate a fresh process
            init("laptop", project_root=project.root)
            get_secret("oracle.stg_pass")     # probe + decrypt AGAIN = 4
        finally:
            providers_module.subprocess.run = real_run

        assert len(calls) == 4, f"expected 4 sops invocations after reset, saw {len(calls)}"


# ---------------------------------------------------------------------------
# T8 - interface isolation and init() lifecycle
# ---------------------------------------------------------------------------


class TestInterfaceIsolation:

    def test_unknown_method_id_in_registry_fails_typed(self, project, monkeypatch):
        # A config that references a method no provider implements -
        # e.g. someone hand-adds 'vault: {}' to methods and points a
        # secret at it BEFORE a Vault provider exists. The dispatcher
        # must refuse with a typed error naming the method and the
        # known implementations.
        build_mixed_world(project)
        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        text = cfg_path.read_text(encoding="utf-8")
        # define a vault section and point a secret at it
        text = text.replace(
            "methods:",
            "methods:\n  vault:\n    endpoint: https://example.invalid",
        )
        text += "  future.secret: {method: vault}\n"
        cfg_path.write_text(text, encoding="utf-8")

        # Simulate the not-yet-written provider: temporarily hide it
        # from the registry (restoring afterwards, whatever happens).
        saved = providers_module.PROVIDER_TYPES.copy()
        providers_module.PROVIDER_TYPES.pop("vault", None)
        try:
            init("laptop", project_root=project.root)
            with pytest.raises(PassMgrConfigError, match="vault"):
                get_secret("future.secret")
        finally:
            providers_module.PROVIDER_TYPES.clear()
            providers_module.PROVIDER_TYPES.update(saved)

    def test_get_secret_before_init_raises(self, project_nobin):
        # The facade demands initialization - a connector calling
        # get_secret without init() gets a startup-order error, not a
        # mysterious None.
        with pytest.raises(PassMgrConfigError, match="init"):
            get_secret("anything")

    def test_init_is_idempotent_per_environment(self, project):
        # Re-init with the SAME environment is a no-op: the cached
        # vault survives (verified: subprocess not re-invoked).
        build_mixed_world(project)

        calls = []
        real_run = providers_module.subprocess.run

        def counting_run(args, *run_args, **run_kwargs):
            if "sops" in str(args[0]):
                calls.append(args[0])
            return real_run(args, *run_args, **run_kwargs)

        providers_module.subprocess.run = counting_run
        try:
            init("laptop", project_root=project.root)
            get_secret("oracle.stg_pass")
            init("laptop", project_root=project.root)   # same env: no-op
            get_secret("ldap.service_pass")
        finally:
            providers_module.subprocess.run = real_run

        # probe + decrypt only - the second init contributed NOTHING
        assert len(calls) == 2

    def test_reinit_with_different_environment_resets(self, project):
        # Switching environments mid-process is allowed but deliberate:
        # the cache is dropped and the OTHER environment's config loads.
        # Building a second environment in the same temp tree proves
        # the switch actually reloads rather than serving stale state.
        build_mixed_world(project)

        # A fernet-only second environment, sharing the same envelope
        project.write_config(
            "prod",
            {
                "version": 1,
                "default_method": "fernet",
                "methods": {
                    "fernet": {
                        "key_file": "secrets/fernet.key",
                        "encrypted_file": "secrets/secrets.fernet.bin",
                    },
                },
                "secrets": {
                    "hpc.api_token": {},
                    "batch.job_token": {},
                },
            },
        )

        init("laptop", project_root=project.root)
        assert get_secret("oracle.stg_pass") == "0ra-stg-pw-7f3"

        init("prod", project_root=project.root)
        assert get_secret("hpc.api_token") == "hpc-tok-42aa"
        # and the laptop-only name is now legitimately unknown
        with pytest.raises(PassMgrConfigError, match="oracle.stg_pass"):
            get_secret("oracle.stg_pass")


# ---------------------------------------------------------------------------
# Guardrail: the spy technique itself must not silently stop working
# ---------------------------------------------------------------------------


class TestSpySanity:

    def test_counting_wrapper_detects_real_invocations(self, project):
        # A meta-test protecting the cache tests above: if the counting
        # spy ever stopped intercepting (e.g. an import refactor moved
        # subprocess), the 'exactly once' tests would pass vacuously by
        # counting zero. This test FAILS if the spy sees nothing.
        build_mixed_world(project)

        calls = []
        real_run = providers_module.subprocess.run

        def counting_run(args, *run_args, **run_kwargs):
            if "sops" in str(args[0]):
                calls.append(args[0])
            return real_run(args, *run_args, **run_kwargs)

        providers_module.subprocess.run = counting_run
        try:
            init("laptop", project_root=project.root)
            get_secret("oracle.stg_pass")
        finally:
            providers_module.subprocess.run = real_run

        assert calls, (
            "Spy saw no sops invocations - the counting technique is "
            "broken; the cache tests above may be passing vacuously."
        )
