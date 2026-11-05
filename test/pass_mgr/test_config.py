"""
T1/T2 - pass_mgr config loading and validation tests.

WHAT THESE TESTS PROVE
======================
`load_config()` is the gatekeeper: every way a config file can be wrong
must be caught AT STARTUP with a typed, named error - never later, at
fetch time, and never silently. This is design-goal #2: "Production
refuses insecure configurations at load time, not at fail time."

WHY NO BINARIES HERE
====================
Config validation is pure structure checking: YAML in, validated data
out. No sops, no age-keygen, no crypto. So these tests build only a
`config/` folder inside pytest's tmp_path and call load_config()
directly. Meaning: this file runs green even on a machine where bin/
hasn't been populated yet - useful when you bootstrap a new machine in
stages (first code, then binaries, then provider tests).

HOW TO READ EACH TEST
=====================
Pattern used throughout:

    with pytest.raises(PassMgrConfigError, match="..."):
        load_config(...)

Translation: "call load_config and CONFIRM it raises the expected error
type, and that the message mentions the right thing." If the call
succeeds instead of raising - the test FAILS. Tests assert that
mistakes are caught, which is backwards from how beginners expect
tests to work; here, failure IS success. This style is called
"testing the negative path" and it is the backbone of robust software.

Run with:  pytest test/pass_mgr/test_config.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pass_mgr import (
    PassMgrConfigError,
    PassMgrProductionPolicyError,
)
from pass_mgr._config import load_config, resolve_relative_path

# ---------------------------------------------------------------------------
# Local helpers - deliberately NOT the TempProject fixture (see docstring)
# ---------------------------------------------------------------------------


def write_config(root: Path, environment: str, config: dict) -> Path:
    """Write a pass_mgr config into a minimal temp tree.

    Mirrors exactly one thing from the real layout:
    <root>/config/pass_mgr.<environment>.yaml
    """
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"pass_mgr.{environment}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def base_config(**overrides) -> dict:
    """The canonical VALID config - every test starts here and then
    breaks exactly ONE thing. That's the discipline that makes failures
    diagnosable: one broken knob per test, so when a test fails you know
    precisely which rule regressed."""
    config = {
        "version": 1,
        "default_method": "sops",
        "methods": {
            "sops": {
                "binary": "bin/sops-v3.13.3.example.platform",
                "manifest": "bin/sops_manifest.txt",
                "age_key_file": "secrets/age.key",
                "encrypted_file": "secrets/secrets.sops.yaml",
            },
            "fernet": {
                "key_file": "secrets/fernet.key",
                "encrypted_file": "secrets/secrets.fernet.bin",
            },
        },
        "secrets": {
            "oracle.stg_pass": {},                   # inherits default: sops
            "ldap.service_pass": {"method": "fernet"},  # explicit override
        },
    }
    config.update(overrides)  # replace top-level keys the caller overrides
    return config


# ---------------------------------------------------------------------------
# T1 - happy path first: prove the WELL-FORMED config loads and resolves
# ---------------------------------------------------------------------------


class TestValidConfig:
    """Baseline. Every negative test below is meaningless unless we've
    first proven the valid case works - otherwise all those 'raises'
    tests could pass simply because NOTHING ever loads."""

    def test_valid_config_loads(self, tmp_path: Path):
        write_config(tmp_path, "laptop", base_config())

        cfg = load_config("laptop", project_root=tmp_path)

        assert cfg.environment == "laptop"
        assert cfg.is_production is False
        assert cfg.default_method == "sops"
        # Per-secret method resolution: inheritance AND override.
        assert cfg.secret_methods == {
            "oracle.stg_pass": "sops",
            "ldap.service_pass": "fernet",
        }

    def test_method_for_returns_resolved_method(self, tmp_path: Path):
        write_config(tmp_path, "laptop", base_config())
        cfg = load_config("laptop", project_root=tmp_path)

        assert cfg.method_for("oracle.stg_pass") == "sops"
        assert cfg.method_for("ldap.service_pass") == "fernet"

    def test_method_for_unknown_secret_raises_config_error(self, tmp_path: Path):
        # Layer-1 'not found': the NAME is absent from the config itself.
        # (Distinct from layer-2: name in config but missing from vault -
        # tested in the SOPS/dispatcher suites.)
        write_config(tmp_path, "laptop", base_config())
        cfg = load_config("laptop", project_root=tmp_path)

        with pytest.raises(PassMgrConfigError, match="hpc.api_token"):
            cfg.method_for("hpc.api_token")


# ---------------------------------------------------------------------------
# T1 - environment and file-level errors
# ---------------------------------------------------------------------------


class TestEnvironmentErrors:

    def test_unknown_environment_name(self, tmp_path: Path):
        # 'server' is not one of the two sanctioned environments. The
        # environment is always explicit - a typo must not quietly
        # produce a search for a nonexistent config file (though it
        # would next fail on missing-file anyway, this earlier error
        # is more precise about WHAT went wrong).
        with pytest.raises(PassMgrConfigError, match="unknown environment"):
            load_config("server", project_root=tmp_path)

    def test_missing_config_file(self, tmp_path: Path):
        # Nothing written at all - the error must name the exact path it
        # looked for, so the operator knows what to create.
        with pytest.raises(PassMgrConfigError, match="pass_mgr.prod.yaml"):
            load_config("prod", project_root=tmp_path)


# ---------------------------------------------------------------------------
# T1 - structural validation (the 'fail fast' rules from design §3)
# ---------------------------------------------------------------------------


class TestStructuralValidation:

    def test_invalid_yaml_syntax(self, tmp_path: Path):
        # Broken YAML (unclosed bracket). The error must carry the
        # underlying YAML diagnostic, not swallow it.
        bad = tmp_path / "config"
        bad.mkdir(parents=True)
        (bad / "pass_mgr.laptop.yaml").write_text(
            "{ version: 1, methods:", encoding="utf-8"
        )

        with pytest.raises(PassMgrConfigError, match="YAML syntax error"):
            load_config("laptop", project_root=tmp_path)

    def test_top_level_not_a_mapping(self, tmp_path: Path):
        # A YAML list at top level - valid YAML, wrong shape.
        bad = tmp_path / "config"
        bad.mkdir(parents=True)
        (bad / "pass_mgr.laptop.yaml").write_text("- a\n- b\n", encoding="utf-8")

        with pytest.raises(PassMgrConfigError, match="mapping"):
            load_config("laptop", project_root=tmp_path)

    def test_unknown_top_level_key_rejected(self, tmp_path: Path):
        # Typo protection: 'secret' instead of 'secrets' would, in a
        # tolerant loader, become silently dead config - the loader
        # would never see any secrets and every fetch would fail
        # mysteriously later. Strict rejection converts that mystery
        # into an immediate, named error at startup.
        write_config(tmp_path, "laptop", base_config(secret={}))

        with pytest.raises(PassMgrConfigError, match="unknown top-level key"):
            load_config("laptop", project_root=tmp_path)

    def test_missing_required_key_rejected(self, tmp_path: Path):
        config = base_config()
        del config["secrets"]

        write_config(tmp_path, "laptop", config)

        with pytest.raises(PassMgrConfigError, match="missing required"):
            load_config("laptop", project_root=tmp_path)

    def test_unsupported_schema_version_rejected(self, tmp_path: Path):
        # version != 1 is reserved for the v2 rotation era. Until a
        # second version exists, anything else is corruption or a file
        # from the future - refuse it rather than guess.
        write_config(tmp_path, "laptop", base_config(version=2))

        with pytest.raises(PassMgrConfigError, match="unsupported config schema version"):
            load_config("laptop", project_root=tmp_path)

    def test_empty_methods_rejected(self, tmp_path: Path):
        # A config with no methods can't serve any secret - refuse at
        # load rather than fail per-fetch forever.
        write_config(tmp_path, "laptop", base_config(methods={}))

        with pytest.raises(PassMgrConfigError, match="methods"):
            load_config("laptop", project_root=tmp_path)

    def test_empty_secrets_rejected(self, tmp_path: Path):
        write_config(tmp_path, "laptop", base_config(secrets={}))

        with pytest.raises(PassMgrConfigError, match="secrets"):
            load_config("laptop", project_root=tmp_path)

    def test_method_section_not_a_mapping(self, tmp_path: Path):
        config = base_config()
        config["methods"]["sops"] = "just-a-string"

        write_config(tmp_path, "laptop", config)

        with pytest.raises(PassMgrConfigError, match="'sops'"):
            load_config("laptop", project_root=tmp_path)

    def test_default_method_must_be_defined(self, tmp_path: Path):
        # default_method pointing at a method with no section = every
        # inheriting secret is unservable. Catch at load.
        write_config(tmp_path, "laptop", base_config(default_method="vault"))

        with pytest.raises(PassMgrConfigError, match="default_method"):
            load_config("laptop", project_root=tmp_path)


# ---------------------------------------------------------------------------
# T1 - per-secret reference validation
# ---------------------------------------------------------------------------


class TestSecretEntryValidation:

    def test_secret_entry_wrong_type(self, tmp_path: Path):
        config = base_config()
        config["secrets"]["oracle.stg_pass"] = "sops"  # a string, not a mapping

        write_config(tmp_path, "laptop", config)

        with pytest.raises(PassMgrConfigError, match="oracle.stg_pass"):
            load_config("laptop", project_root=tmp_path)

    def test_secret_entry_unknown_key_rejected(self, tmp_path: Path):
        # Entry allows ONLY 'method'. 'methd:' would silently fall back
        # to the default - the wrong vault - and fail with a confusing
        # not-found error later. Reject at load.
        config = base_config()
        config["secrets"]["oracle.stg_pass"] = {"methd": "fernet"}

        write_config(tmp_path, "laptop", config)

        with pytest.raises(PassMgrConfigError, match="unknown key"):
            load_config("laptop", project_root=tmp_path)

    def test_secret_referencing_undefined_method(self, tmp_path: Path):
        # 'vault' has no section in methods{}. The error must name BOTH
        # the secret and the method so the fix is obvious.
        config = base_config()
        config["secrets"]["hpc.api_token"] = {"method": "vault"}

        write_config(tmp_path, "laptop", config)

        with pytest.raises(PassMgrConfigError, match="undefined method.*vault"):
            load_config("laptop", project_root=tmp_path)


# ---------------------------------------------------------------------------
# T2 - production policy (design goal #2, the security-critical rule)
# ---------------------------------------------------------------------------


def production_config_with(plain_in: str) -> dict:
    """A prod config where 'plain' sneaks in via `plain_in`.

    'default'  -> default_method: plain (everything inherits it)
    'override' -> one secret overrides to plain, rest stay on sops
    """
    config = {
        "version": 1,
        "default_method": "sops" if plain_in == "override" else "plain",
        "methods": {
            "sops": {
                "binary": "bin/sops-v3.13.3.example.platform",
                "manifest": "bin/sops_manifest.txt",
                "age_key_file": "secrets/age.key",
                "encrypted_file": "secrets/secrets.sops.yaml",
            },
            "plain": {},  # plain must be DEFINED to be REFERENCED -
                          # otherwise the undefined-method rule fires first,
                          # which would mask the policy error.
        },
        "secrets": {
            "oracle.stg_pass": (
                {"method": "plain"} if plain_in == "override" else {}
            ),
        },
    }
    return config


class TestProductionPolicy:

    def test_prod_rejects_plain_as_default_method(self, tmp_path: Path):
        # The headline rule: production REFUSES plaintext credentials.
        # Not a warning - a hard load failure. This is the test the
        # security team will want to see exists.
        write_config(tmp_path, "prod", production_config_with("default"))

        with pytest.raises(PassMgrProductionPolicyError, match="PRODUCTION"):
            load_config("prod", project_root=tmp_path)

    def test_prod_rejects_single_plain_override(self, tmp_path: Path):
        # Stricter variant: default is sops, but ONE secret overrides to
        # plain. Still refused AT LOAD - prod must not even be CAPABLE
        # of serving a plaintext secret, whether or not anything asks.
        write_config(tmp_path, "prod", production_config_with("override"))

        with pytest.raises(PassMgrProductionPolicyError, match="plain"):
            load_config("prod", project_root=tmp_path)

    def test_laptop_allows_plain(self, tmp_path: Path):
        # The mirror test - policy errors must NOT over-fire. On the
        # laptop, plain is legal (local development convenience), so the
        # identical config that prod refused must load cleanly here.
        # Every rejection rule deserves a companion like this: prove
        # the gate opens for the right key, not just that it shuts.
        write_config(tmp_path, "laptop", production_config_with("override"))

        cfg = load_config("laptop", project_root=tmp_path)

        assert cfg.secret_methods["oracle.stg_pass"] == "plain"

    def test_prod_with_only_secure_methods_loads(self, tmp_path: Path):
        # And the real prod shape - sops everywhere - sails through.
        write_config(tmp_path, "prod", base_config())

        cfg = load_config("prod", project_root=tmp_path)

        assert cfg.is_production is True
        assert cfg.secret_methods["oracle.stg_pass"] == "sops"


# ---------------------------------------------------------------------------
# T1 - path hygiene rules (used by providers; rules live in the loader)
# ---------------------------------------------------------------------------


class TestPathHygiene:

    def test_relative_path_resolves_inside_root(self, tmp_path: Path):
        resolved = resolve_relative_path(
            "secrets/age.key", "sops.age_key_file", tmp_path
        )
        assert resolved == (tmp_path / "secrets" / "age.key").resolve()

    def test_absolute_path_rejected(self, tmp_path: Path):
        # Design rule: no absolute user paths - they break the
        # laptop->office->server transport, which carries the whole tree.
        # NOTE: '/etc/passwd' is NOT absolute on Windows (it is
        # drive-relative there), so the test must build a path that is
        # absolute on whichever platform runs the suite.
        absolute = str(Path(tmp_path.anchor) / "etc" / "passwd")
        with pytest.raises(PassMgrConfigError, match="absolute"):
            resolve_relative_path(absolute, "sops.binary", tmp_path)

    def test_traversal_outside_root_rejected(self, tmp_path: Path):
        # '../..' tries to escape the project tree. Even though the
        # office transport makes exfiltration awkward, defense here is
        # cheap and the failure message is worth having.
        with pytest.raises(PassMgrConfigError, match="outside the project root"):
            resolve_relative_path(
                "../../etc/passwd", "sops.binary", tmp_path
            )

    def test_non_string_path_rejected(self, tmp_path: Path):
        with pytest.raises(PassMgrConfigError, match="non-empty string"):
            resolve_relative_path(None, "sops.binary", tmp_path)

class TestKeyFilePathResolution:
    """Pins the key-location contract: key material may live OUTSIDE the
    project tree, expressed as a ~/... path - and nowhere else."""

    def test_tilde_key_path_expands_and_is_allowed(self, tmp_path):
        raw = "~/.dpl-keys/laptop/fernet/key"
        assert resolve_relative_path(raw, "fernet.key_file", tmp_path) \
            == Path(raw).expanduser().resolve()

    def test_age_key_path_uses_the_same_rule(self, tmp_path):
        raw = "~/.dpl-keys/laptop/sops-age/age.key"
        assert resolve_relative_path(raw, "sops.age_key_file", tmp_path) \
            == Path(raw).expanduser().resolve()

    def test_absolute_key_path_is_still_rejected(self, tmp_path):
        with pytest.raises(PassMgrConfigError):
            resolve_relative_path("/Users/someone/.dpl-keys/k",
                                  "fernet.key_file", tmp_path)

    def test_tilde_key_path_escaping_home_is_rejected(self, tmp_path):
        with pytest.raises(PassMgrConfigError):
            resolve_relative_path("~/../../etc/shadow",
                                  "fernet.key_file", tmp_path)