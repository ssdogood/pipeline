"""
T3/T4/T9 - SOPS provider tests: round-trip, failure modes, version pin.

WHAT THESE TESTS PROVE
======================
This is the PRIMARY provider - the one the security team is being asked
to approve. The suite demonstrates, with the REAL pinned binary executing
as a real subprocess:

  T3  Round-trip: secrets encrypted with a throwaway age key decrypt
      correctly through get_secret(), with namespace flattening.
  T4  Failure modes: missing binary, missing identity file, missing
      vault, WRONG identity (the 'key rotated out from under us'
      scenario), tampered ciphertext, and a hung subprocess timing out
      instead of parking a cron job forever.
  T9  Version pinning: a substituted or drifted binary is REFUSED by
      comparing its --version output against the manifest pin - the
      enforcement that makes 'verify, then trust' automatic at runtime,
      complementing the shasum check that happens at staging time.

REQUIRES BINARIES
=================
Uses the `project` fixture (binaries copied into the temp tree):
sops pinned per sops_manifest.txt, age-keygen v1.3.2. On a machine
without them the suite fails LOUDLY - by design, that's the green
script's job to catch.

NOTHING NETWORK: sops + age are entirely local. The tests generate
their own keys per-test inside tmp_path; no key is ever reused across
tests, and no test reads the developer's real secrets/.

Run with:  pytest test/pass_mgr/test_sops.py -v
"""

from __future__ import annotations

import sys
import stat
from pathlib import Path

import pytest

import pass_mgr._providers
from pass_mgr import (
    PassMgrBinaryMissingError,
    PassMgrConfigError,
    PassMgrDecryptError,
    PassMgrKeyFileMissingError,
    PassMgrSecretNotFoundError,
    PassMgrVersionMismatchError,
    get_secret,
    init,
)
from pass_mgr._config import load_config
from pass_mgr._providers import SopsProvider


# ---------------------------------------------------------------------------
# Shared setup: one canonical, fully-working SOPS world per test
# ---------------------------------------------------------------------------


def build_working_sops_setup(project, entries, secrets=None):
    """Standard world build (see module docstring); callers with custom
    vault entries must pass matching config secret names."""
    identity_file, recipient = project.add_age_identity()
    project.add_sops_vault(entries, identity_file, recipient)
    if secrets is None:
        secrets = {"oracle.stg_pass": {}, "ldap.service_pass": {}}
    project.write_config_default(secrets=secrets)
    return identity_file, recipient


# ---------------------------------------------------------------------------
# T3 - round-trip, the happy path
# ---------------------------------------------------------------------------


class TestRoundTrip:

    def test_decrypt_via_get_secret(self, project, sample_entries):
        # THE headline demo test: real sops binary, real age key,
        # end-to-end through the public interface callers will use.
        build_working_sops_setup(project, sample_entries)

        init("laptop", project_root=project.root)
        assert get_secret("oracle.stg_pass") == "0ra-stg-pw-7f3"
        assert get_secret("ldap.service_pass") == "ld4p-svc-pw-9k1"

    def test_namespaces_flatten_to_dotted_names(self, project, sample_entries):
        # The vault stores oracle: {stg_pass: ...}; callers ask for
        # 'oracle.stg_pass'. The flattening contract lives here.
        build_working_sops_setup(project, sample_entries)

        init("laptop", project_root=project.root)
        vault_keys = {"oracle.stg_pass", "ldap.service_pass"}
        # both names resolvable, and distinct values (no cross-wiring)
        assert get_secret("oracle.stg_pass") != get_secret("ldap.service_pass")
        assert vault_keys  # guards the test itself against empty sets

    def test_deep_namespace_flattens_multi_level(self, project):
        build_working_sops_setup(
            project,
            {"app": {"subsystem": {"deep_pass": "d33p-v4lue"}}},
            secrets={"app.subsystem.deep_pass": {}},
        )

        init("laptop", project_root=project.root)
        assert get_secret("app.subsystem.deep_pass") == "d33p-v4lue"

    def test_non_string_leaf_refused(self, project):
        # YAML coerces bare numbers to ints - a numeric password would
        # crash a connector at bind time. Refused at the boundary.
        build_working_sops_setup(
            project,
            {"oracle": {"numeric": 12345}},
            secrets={"oracle.numeric": {}},
        )

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrDecryptError, match="string"):
            get_secret("oracle.numeric")

    def test_provider_direct_load_returns_full_map(self, project, sample_entries):
        # Cold-cache path: instantiate the provider directly, no
        # dispatcher. Same result as via get_secret.
        build_working_sops_setup(project, sample_entries)

        cfg = load_config("laptop", project_root=project.root)
        provider = SopsProvider(cfg.method_sections["sops"], cfg)
        assert provider.load() == {
            "oracle.stg_pass": "0ra-stg-pw-7f3",
            "ldap.service_pass": "ld4p-svc-pw-9k1",
        }


# ---------------------------------------------------------------------------
# T4 - failure modes: every one typed, every message actionable
# ---------------------------------------------------------------------------


class TestFailureModes:

    def test_missing_binary(self, project, sample_entries):
        # Config points at a binary that isn't there - the classic
        # 'new machine, bin/ not populated' case. Named error includes
        # the EXPECTED PATH so the operator knows exactly what to copy.
        identity_file, recipient = project.add_age_identity()
        project.add_sops_vault(sample_entries, identity_file, recipient)
        project.write_config_default()

        # break the one knob: point the config at a nonexistent binary
        real_binary = project.binary_path("sops")
        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(real_binary, "bin/sops-v0.0.0.absent")
        cfg_path.write_text(cfg_text, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrBinaryMissingError, match="sops-v0.0.0.absent"):
            get_secret("oracle.stg_pass")

    def test_missing_identity_file(self, project, sample_entries):
        # The 'USB stick lost the key' case: vault present, binary fine,
        # age.key absent.
        build_working_sops_setup(project, sample_entries)
        (project.secrets_dir / "age.key").unlink()

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrKeyFileMissingError, match="age.key"):
            get_secret("oracle.stg_pass")

    def test_missing_vault_file(self, project, sample_entries):
        # Config + key present, vault file absent (never created, or
        # lost in transport).
        identity_file, _ = project.add_age_identity()
        project.write_config_default()
        # no vault created; key exists so probes pass until the vault

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrConfigError, match="secrets.sops.yaml"):
            get_secret("oracle.stg_pass")

    def test_wrong_identity_raises_decrypt_error(self, project, sample_entries):
        # The 'key rotated out from under us' case: the vault was
        # sealed to recipient A, but age.key now holds identity B.
        # sops must refuse; we surface it as DecryptError naming the file.
        build_working_sops_setup(project, sample_entries)

        # mint a SECOND, different identity over the first
        (project.secrets_dir / "age.key").unlink()  # age-keygen refuses overwrite
        project.add_age_identity()

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrDecryptError, match="failed to decrypt"):
            get_secret("oracle.stg_pass")

    def test_tampered_vault_raises_decrypt_error(self, project, sample_entries):
        # Corrupt the encrypted YAML: authenticated encryption must
        # refuse, never serve garbage. (Mirrors the Fernet tamper test
        # - symmetric taxonomy across primary and backup, by design.)
        build_working_sops_setup(project, sample_entries)

        vault = project.secrets_dir / "secrets.sops.yaml"
        text = vault.read_text(encoding="utf-8")
        # flip a character deep inside the data-encrypted body
        mid = len(text) // 2
        flipped = text[:mid] + ("\n" if text[mid] != "\n" else " ") + text[mid + 1 :]
        vault.write_text(flipped, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrDecryptError):
            get_secret("oracle.stg_pass")
        
    @pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake hanging binary is a Unix shebang script; Windows "
            "cannot launch it (WinError 216). The timeout mechanism "
            "itself (subprocess timeout -> DecryptError) is "
            "platform-uniform stdlib behavior.",
    )

    def test_subprocess_timeout_instead_of_hang(self, project, sample_entries):
        # A sops invocation that never returns must be killed by the
        # hard timeout and surfaced as DecryptError - cron jobs must
        # fail loudly, never park forever (design §5.1).
        #
        # How: substitute a fake 'binary' that is a shell script that
        # sleeps, and shrink the timeout to half a second. The version
        # probe invokes it first - and times out there.
        identity_file, recipient = project.add_age_identity()
        project.add_sops_vault(sample_entries, identity_file, recipient)

        # write the sleeping fake where the config expects sops
        real_binary_name = Path(project.binary_path("sops")).name
        fake = project.root / "bin" / real_binary_name
        fake.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        project.write_config_default()

        monkeypatch_holder = pytest.MonkeyPatch()
        monkeypatch_holder.setattr(
            pass_mgr._providers, "SOPS_TIMEOUT_SECONDS", 0.5
        )
        try:
            init("laptop", project_root=project.root)
            with pytest.raises(PassMgrDecryptError, match="timed out"):
                get_secret("oracle.stg_pass")
        finally:
            monkeypatch_holder.undo()

    def test_secret_in_config_but_missing_from_vault(self, project, sample_entries):
        # Layer-2 not-found: config lists a name the vault never held.
        build_working_sops_setup(project, sample_entries)
        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            "ldap.service_pass: {}", "ldap.service_pass: {}\n  hpc.api_token: {}"
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrSecretNotFoundError, match="hpc.api_token"):
            get_secret("hpc.api_token")


# ---------------------------------------------------------------------------
# T9 - version pinning: the runtime half of 'verify, then trust'
# ---------------------------------------------------------------------------


class TestVersionPinning:

    def test_drifted_binary_refused_via_manifest_pin(self, project, sample_entries):
        # The scenario the security team cares about: someone drops a
        # DIFFERENT (older/newer/substituted) binary into bin/. The
        # checksum check caught file substitution at staging; THIS
        # test proves runtime enforcement catches version drift even if
        # a file passes through unchecksummed.
        #
        # Trick: leave the real binary in place, but doctor the
        # manifest's PIN COMMENT to claim a version the binary doesn't
        # report. Expect refusal, naming found vs expected.
        build_working_sops_setup(project, sample_entries)

        manifest = project.root / "bin" / "sops_manifest.txt"
        text = manifest.read_text(encoding="utf-8")
        text = text.replace(
            "# SOPS version: v3.13.3 (pinned)",
            "# SOPS version: v9.9.9 (pinned)",
        )
        manifest.write_text(text, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrVersionMismatchError, match="9.9.9"):
            get_secret("oracle.stg_pass")

    def test_substituted_tool_refused(self, project, sample_entries):
        # Point the sops config at age-keygen - an executable that
        # exists but is the WRONG TOOL. It runs, prints a version, and
        # the version probe still refuses it. Belt AND braces: even a
        # present, working binary fails closed if it isn't the pinned
        # one.
        build_working_sops_setup(project, sample_entries)

        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            f"binary: {project.binary_path('sops')}",
            f"binary: {project.binary_path('age-keygen')}",
        )
        cfg_path.write_text(cfg_text, encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrVersionMismatchError):
            get_secret("oracle.stg_pass")

    def test_absent_manifest_degrades_gracefully(self, project, sample_entries):
        # Manifest is optional in config (checksum verification happens
        # out-of-band via the green script). Without it, no runtime
        # version pin - but decryption still works. Degrade, don't die.
        build_working_sops_setup(project, sample_entries)

        cfg_path = project.root / "config" / "pass_mgr.laptop.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        lines = [
            ln for ln in cfg_text.splitlines()
            if not ln.strip().startswith("manifest:")
        ]
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        
        init("laptop", project_root=project.root)
        assert get_secret("oracle.stg_pass") == "0ra-stg-pw-7f3"

    def test_corrupt_manifest_is_hard_error(self, project, sample_entries):
        # Present but MALFORMED pin comment: a half-configured pin is
        # worse than none - refuse rather than guess.
        build_working_sops_setup(project, sample_entries)

        manifest = project.root / "bin" / "sops_manifest.txt"
        text = manifest.read_text(encoding="utf-8")
        text = "\n".join(
            line for line in text.splitlines() if "SOPS version" not in line
        )
        manifest.write_text(text + "\n", encoding="utf-8")

        init("laptop", project_root=project.root)
        with pytest.raises(PassMgrConfigError, match="manifest"):
            get_secret("oracle.stg_pass")
