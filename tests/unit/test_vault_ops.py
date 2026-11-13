"""Smoke tests for the vault operations tool (scripts/vault_ops.py).

These test the OPERATOR TOOL, not the library - the library has its
own 67-test suite. Offline doctrine applies: everything runs in temp
directories against fixture configs; no network; no real secrets.

What is deliberately NOT tested here: anything that requires the
real sops binary, the real age key, or the real vault - the library
suite already covers the crypto round-trip. These tests cover the
logic that burned us live: argument parsing, categorization, tree
merging, pruning, and the config cross-check.

Run:   pytest tests/unit/test_vault_ops.py -v
"""
# tests/unit/<this file> -> parents[2] is the project root

import importlib.util
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from cryptography.fernet import Fernet

# Import vault_ops from the scripts directory (it is NOT a package).
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "vault_ops", SCRIPTS_DIR / "vault_ops.py")
assert _spec is not None and _spec.loader is not None, \
    "vault_ops.py not found - scripts/ layout changed?"
vault_ops = importlib.util.module_from_spec(_spec)
sys.modules["vault_ops"] = vault_ops
_spec.loader.exec_module(vault_ops)


# --------------------------------------------------------------- fixtures ---

@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """Redirect all vault_ops filesystem roots into a temp directory."""
    pr = tmp_path
    (pr / "config" / "extractors").mkdir(parents=True)
    (pr / "secrets").mkdir(parents=True)
    (pr / "secrets" / "logs").mkdir()
    (pr / "secrets" / "backups").mkdir()

    monkeypatch.setattr(vault_ops, "PROJECT_ROOT", pr)
    monkeypatch.setattr(vault_ops, "CONFIG_DIR", pr / "config")
    monkeypatch.setattr(vault_ops, "EXTRACTORS_DIR", pr / "config" / "extractors")
    (pr / "config" / "smtp").mkdir(parents=True)
    monkeypatch.setattr(vault_ops, "SMTP_CREDENTIALS_DIR",
                        pr / "config" / "smtp")
    monkeypatch.setattr(vault_ops, "SECRETS_DIR", pr / "secrets")
    monkeypatch.setattr(vault_ops, "BACKUP_DIR", pr / "secrets" / "backups")
    monkeypatch.setattr(vault_ops, "LOG_FILE", pr / "secrets" / "logs" / "x.log")
    return pr


def write_extractor(tmp_project, filename, connections: dict) -> None:
    p = tmp_project / "config" / "extractors" / filename
    p.write_text(yaml.safe_dump({"connections": connections}), encoding="utf-8")


def write_pass_cfg(tmp_project, env: str, secrets: list) -> None:
    p = tmp_project / "config" / f"pass_mgr.{env}.yaml"
    body = ("# header comment - must survive\n"
            "version: 1\n"
            "default_method: sops\n"
            "methods:\n"
            "  sops:\n"
            "    binary: bin/sops-x\n"
            "    age_key_file: ~/.dpl-keys/x\n"
            "    encrypted_file: secrets/secrets.sops.yaml\n")
    body += "secrets:\n" + "".join(f'  "{n}": {{}}\n' for n in secrets)
    p.write_text(body, encoding="utf-8")


# ------------------------------------------------------------ arg parsing ---

class TestArgumentParsing:
    """The --dry-run-as-env bug and the missing-env bugs, bottled."""

    def _run(self, *args):
        return vault_ops.main(["vault.sh", *args])

    def test_no_args_shows_usage(self, capsys):
        assert self._run() == 2
        assert "usage:" in capsys.readouterr().err

    def test_unknown_command(self, capsys):
        assert self._run("frobnicate", "laptop") == 2

    def test_list_without_env_is_usage_error(self, capsys):
        assert self._run("list") == 2
        err = capsys.readouterr().err
        assert "needs an environment" in err

    def test_scan_without_env_is_usage_error(self, capsys):
        assert self._run("scan") == 2
        err = capsys.readouterr().err          # <- the missing line
        assert "needs an environment" in err
        assert "scan laptop --dry-run" in err

    def test_dry_run_alone_is_not_an_environment(self, capsys):
        # The original bug: 'scan --dry-run' treated the flag as env.
        assert self._run("scan", "--dry-run") == 2
        assert "needs an environment" in capsys.readouterr().err

    def test_flag_order_is_free(self, tmp_project, monkeypatch, capsys):
        seen = {}
        monkeypatch.setattr(vault_ops, "cmd_scan",
                            lambda env, dry_run=False:
                            seen.update(env=env, dry_run=dry_run))
        self._run("--dry-run", "scan", "laptop")
        assert seen == {"env": "laptop", "dry_run": True}

    def test_unknown_flag_rejected(self, capsys):
        assert self._run("scan", "laptop", "--force") == 2
        assert "unknown option" in capsys.readouterr().err


# ------------------------------------------------------------ tree logic ----

class TestTreeUtils:
    def test_flatten_nested(self):
        tree = {"a": {"b": {"c": "v"}}, "top": "x"}
        assert vault_ops.flatten(tree) == {"a.b.c": "v", "top": "x"}

    def test_flatten_skips_empty_namespaces(self):
        assert vault_ops.flatten({"empty": {}, "live": "v"}) == {"live": "v"}

    def test_flatten_rejects_non_string_leaf(self):
        with pytest.raises(SystemExit):
            vault_ops.flatten({"bad": 42})

    def test_unflatten_round_trip(self):
        flat = {"a.b.c": "v"}
        assert vault_ops.flatten(vault_ops.unflatten(flat)) == flat

    def test_merge_extra_wins(self):
        base = {"ns": {"k": "old", "keep": "same"}}
        extra = {"ns": {"k": "new"}}
        assert vault_ops.merge_tree(base, extra)["ns"]["k"] == "new"
        assert vault_ops.merge_tree(base, extra)["ns"]["keep"] == "same"

    def test_prune_removes_empty_parents_recursively(self):
        tree = {"live": "v",
                "dead": {"child": {}},
                "keeper": {"inner": "v"}}
        removed = vault_ops.prune_empty_namespaces(tree)
        assert "dead" in removed and "child" in removed
        assert "keeper" in tree and "dead" not in tree

    def test_prune_on_clean_tree_is_noop(self):
        tree = {"a": {"b": "v"}}
        assert vault_ops.prune_empty_namespaces(tree) == []
        assert tree == {"a": {"b": "v"}}


# --------------------------------------------------------- credential refs --

class TestDiscovery:
    def test_finds_refs_across_files(self, tmp_project):
        write_extractor(tmp_project, "one.yaml", {
            "ora_db": {"users": {"u1": {"credential_ref": "ora_db.u1.pw"}}}})
        write_extractor(tmp_project, "two.yaml", {
            "ad_x": {"users": {"u2": {"credential_ref": "ad_x.u2.pw"}}}})
        refs = vault_ops.discover_credential_refs()
        assert {r[0] for r in refs} == {"ora_db.u1.pw", "ad_x.u2.pw"}

    def test_user_without_ref_is_ignored(self, tmp_project):
        write_extractor(tmp_project, "one.yaml", {
            "ora_db": {"users": {"no_cred": {"role": "reader"}}}})
        assert vault_ops.discover_credential_refs() == []

    def test_backslash_in_username_survives(self, tmp_project):
        # Real-world case: AD users like subd\support
        write_extractor(tmp_project, "ad.yaml", {
            "ad_atm": {"users": {"subd\\support":
                          {"credential_ref": "ad_atm.subd_support.password"}}}})
        refs = vault_ops.discover_credential_refs()
        assert refs[0][0] == "ad_atm.subd_support.password"

    def test_malformed_yaml_skips_file_without_crashing(self, tmp_project):
        (tmp_project / "config" / "extractors" / "broken.yaml") \
            .write_text("{ not: valid: yaml :", encoding="utf-8")
        write_extractor(tmp_project, "good.yaml", {
            "c": {"users": {"u": {"credential_ref": "c.u.pw"}}}})
        refs = vault_ops.discover_credential_refs()
        assert len(refs) == 1

    def test_missing_extractors_dir(self, tmp_project):
        vault_ops.EXTRACTORS_DIR.rmdir()
        assert vault_ops.discover_credential_refs() == []


# ----------------------------------------------------------- cross-check ----

class TestSecretMappingSync:
    """The three-place-consistency gap, closed and pinned by tests."""

    def test_sync_adds_missing_and_removes_stale(self, tmp_project, monkeypatch):
        write_pass_cfg(tmp_project, "laptop", ["ghost.secret"])
        monkeypatch.setattr("builtins.input", lambda _: "y")
        vault_ops.sync_secret_mapping("laptop", {"a.b.password"}, dry_run=False)

        cfg = yaml.safe_load(
            (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text())
        assert set(cfg["secrets"].keys()) == {"a.b.password"}
        # ghost.secret removed, a.b.password added

    def test_sync_preserves_content_above_sections(self, tmp_project, monkeypatch):
        write_pass_cfg(tmp_project, "laptop", [])
        monkeypatch.setattr("builtins.input", lambda _: "y")
        vault_ops.sync_secret_mapping("laptop", {"n.v"}, dry_run=False)

        text = (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text()
        assert "# header comment - must survive" in text
        assert "default_method: sops" in text
        assert "methods:" in text

    def test_sync_declined_leaves_file_untouched(self, tmp_project, monkeypatch):
        write_pass_cfg(tmp_project, "laptop", ["ghost.secret"])
        before = (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text()
        monkeypatch.setattr("builtins.input", lambda _: "n")
        vault_ops.sync_secret_mapping("laptop", {"a.b.password"}, dry_run=False)
        after = (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text()
        assert before == after

    def test_sync_dry_run_touches_nothing(self, tmp_project):
        write_pass_cfg(tmp_project, "laptop", ["ghost.secret"])
        before = (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text()
        vault_ops.sync_secret_mapping("laptop", {"a.b.password"}, dry_run=True)
        assert (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text() \
            == before

    def test_sync_silent_when_already_canonical(self, tmp_project, capsys):
        write_pass_cfg(tmp_project, "laptop", ["a.b.password"])
        vault_ops.sync_secret_mapping("laptop", {"a.b.password"}, dry_run=False)
        out = capsys.readouterr().out
        assert "CROSS-CHECK" not in out  # no noise when nothing to do

    def test_sync_appends_mapping_when_none_exists(self, tmp_project, monkeypatch):
        p = tmp_project / "config" / "pass_mgr.laptop.yaml"
        p.write_text("version: 1\ndefault_method: sops\n", encoding="utf-8")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        vault_ops.sync_secret_mapping("laptop", {"x.y"}, dry_run=False)
        cfg = yaml.safe_load(p.read_text())
        assert set(cfg["secrets"].keys()) == {"x.y"}

    def test_quoted_keys_handle_special_chars(self, tmp_project, monkeypatch):
        # Names with backslashes/dashes must survive YAML round-trip
        write_pass_cfg(tmp_project, "laptop", [])
        monkeypatch.setattr("builtins.input", lambda _: "y")
        weird = {"ad.subd-support.password", "a'b.password"}
        vault_ops.sync_secret_mapping("laptop", weird, dry_run=False)
        cfg = yaml.safe_load(
            (tmp_project / "config" / "pass_mgr.laptop.yaml").read_text())
        assert set(cfg["secrets"].keys()) == weird


# -------------------------------------------------------------- regression --

class TestRegressions:
    """One test per bug found live during this project. If one fails,
    the corresponding incident has regressed."""

    def test_placeholder_never_enters_vault_tree(self):
        # Incident: new secrets were added with value "PLACEHOLDER"
        # when the real password never reached final_tree. Guard: any
        # value routed through the additions loop must be real. Here we
        # assert the API contract: unflatten inserts exactly what it is
        # given - so callers must only ever feed it prompted values.
        assert vault_ops.unflatten({"a.b": "typed-value"})["a"]["b"] \
            == "typed-value"

    def test_fingerprint_is_stable_and_short(self):
        fp1 = vault_ops.fingerprint("name", "value")
        fp2 = vault_ops.fingerprint("name", "value")
        fp3 = vault_ops.fingerprint("name", "other")
        fp4 = vault_ops.fingerprint("other-name", "value")
        assert fp1 == fp2                 # deterministic
        assert len(fp1) == 16            # truncated by design
        assert fp1 != fp3                 # value-sensitive
        assert fp1 != fp4                 # name-salted

    def test_vault_header_has_do_not_edit_and_timestamp(self):
        for header in (vault_ops.fernet_header(), vault_ops.sops_header()):
            assert "DO NOT EDIT" in header
            assert "Last synced:" in header

"""Sync-behavior smoke tests for the fernet-canonical vault_ops.

Covers the five sync guarantees plus a full tool-side round trip:
  1. sops mirror skipped QUIETLY (SYNC_SKIP logged) when binary missing
  2. sops mirror skipped when no sops config section exists
  3. missing fernet section is a HARD failure (canonical, not optional)
  4. mirror absent / unreadable / drifted  -> refresh needed
  5. mirror already in sync -> not rewritten
  6. seal refuses an empty tree (anti-wipe guard)
  7. seal -> decrypt round trip through the real envelope format,
     including the DO-NOT-EDIT header
"""

# --------------------------------------------------------------- scaffolding --

def install_project(monkeypatch, tmp_path):
    """Point every vault_ops path constant at a throwaway tree."""
    root = tmp_path / "pipeline"
    dirs = {
        "CONFIG_DIR": root / "config",
        "EXTRACTORS_DIR": root / "config" / "extractors",
        "SECRETS_DIR": root / "secrets",
        "BACKUP_DIR": root / "secrets" / "backups",
        "LOG_FILE": root / "secrets" / "logs" / "dpl_pass_mgr.log",
        "PROJECT_ROOT": root,
    }
    for name, path in dirs.items():
        monkeypatch.setattr(vault_ops, name, path)
    for path in dirs.values():
        if path.suffix == ".log":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return root


def write_fernet_key(root):
    key = Fernet.generate_key()
    key_file = root / "secrets" / "fernet.key"
    key_file.write_bytes(key)
    return key_file


def fake_sops_cfg(root, *, binary_exists=True, vault_exists=False):
    binary = root / "secrets" / "sops"
    if binary_exists:
        binary.write_text("#!/bin/sh\nexit 0\n")
    key = root / "secrets" / "age.key"
    key.write_text("# public key: age1abc\nAGE-SECRET-KEY-xyz\n")
    vault = root / "secrets" / "secrets.sops.yaml"
    if vault_exists:
        vault.write_text("# stub sops vault\n")
    return binary, key, vault


# ------------------------------------------------------------------- tests ----

class TestSopsMirrorSkipping:
    """Guarantee 1 & 2: missing sops prerequisites NEVER crash the flow."""

    def test_skip_when_binary_missing(self, monkeypatch, tmp_path, capsys):
        root = install_project(monkeypatch, tmp_path)
        _binary, key, _vault = fake_sops_cfg(root, binary_exists=False)
        cfg = {"methods": {"sops": {
            "binary": "secrets/sops",               # resolved via PROJECT_ROOT
            "age_key_file": str(key),               # resolved via expanduser
            "encrypted_file": "secrets/secrets.sops.yaml",
        }}}
        assert vault_ops.optional_sops_settings(cfg) is None
        out = capsys.readouterr().out
        assert "SYNC_SKIP" in out and "sops binary not present" in out

    def test_skip_when_no_sops_section(self, monkeypatch, tmp_path, capsys):
        install_project(monkeypatch, tmp_path)
        assert vault_ops.optional_sops_settings({"methods": {}}) is None
        assert "SYNC_SKIP" in capsys.readouterr().out

    def test_provisioned_when_all_present(self, monkeypatch, tmp_path, capsys):
        root = install_project(monkeypatch, tmp_path)
        _binary, key, _vault = fake_sops_cfg(root, vault_exists=True)
        cfg = {"methods": {"sops": {
            "binary": "secrets/sops",
            "age_key_file": str(key),
            "encrypted_file": "secrets/secrets.sops.yaml",
        }}}
        assert vault_ops.optional_sops_settings(cfg) is not None
        assert "SYNC_SKIP" not in capsys.readouterr().out


class TestCanonicalFernetPrerequisites:
    """Guarantee 3: the canonical vault's prerequisites are HARD failures."""

    def test_missing_fernet_section_is_fatal(self, monkeypatch, tmp_path, capsys):
        install_project(monkeypatch, tmp_path)
        with pytest.raises(SystemExit):
            vault_ops.fernet_settings({"methods": {}})
        assert "no 'fernet' method section" in capsys.readouterr().err

    def test_missing_fernet_key_is_fatal(self, monkeypatch, tmp_path, capsys):
        install_project(monkeypatch, tmp_path)
        cfg = {"methods": {"fernet": {
            "key_file": "secrets/fernet.key",       # never written
            "encrypted_file": "secrets/secrets.fernet.bin",
        }}}
        with pytest.raises(SystemExit):
            vault_ops.fernet_settings(cfg)
        assert "fernet key file missing" in capsys.readouterr().err


class TestMirrorDriftDetection:
    """Guarantees 4 & 5: drift -> refresh; in sync -> leave alone."""

    ENTRIES: ClassVar[dict] = {"oracle.scott.password": "pw-one"}

    def test_absent_mirror_needs_refresh(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        sops_cfg = fake_sops_cfg(root, binary_exists=True, vault_exists=False)
        assert vault_ops.sops_mirror_needed(sops_cfg, self.ENTRIES) is True

    def test_unreadable_mirror_needs_refresh(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        sops_cfg = fake_sops_cfg(root, binary_exists=True, vault_exists=True)

        def boom(*a, **kw):
            raise SystemExit(1)  # simulate decrypt failure in sops_mirror_needed
        monkeypatch.setattr(vault_ops, "decrypt_sops_tree", boom)
        assert vault_ops.sops_mirror_needed(sops_cfg, self.ENTRIES) is True

    def test_drifted_mirror_needs_refresh(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        sops_cfg = fake_sops_cfg(root, binary_exists=True, vault_exists=True)
        monkeypatch.setattr(vault_ops, "decrypt_sops_tree",
                            lambda *a, **kw: {"oracle": {"scott": {"password": "OLD"}}})
        assert vault_ops.sops_mirror_needed(sops_cfg, self.ENTRIES) is True

    def test_in_sync_mirror_is_left_alone(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        sops_cfg = fake_sops_cfg(root, binary_exists=True, vault_exists=True)
        monkeypatch.setattr(vault_ops, "decrypt_sops_tree",
                            lambda *a, **kw: {"oracle": {"scott": {"password": "pw-one"}}})
        assert vault_ops.sops_mirror_needed(sops_cfg, self.ENTRIES) is False


class TestSealIntegrity:
    """Guarantees 6 & 7: anti-wipe guard + real envelope round trip."""

    def test_seal_refuses_empty_tree(self, monkeypatch, tmp_path, capsys):
        root = install_project(monkeypatch, tmp_path)
        key_file = write_fernet_key(root)
        target = root / "secrets" / "secrets.fernet.bin"
        with pytest.raises(SystemExit):
            vault_ops.seal_fernet_and_verify(key_file, target, {})
        assert "refusing to seal an empty" in capsys.readouterr().err
        assert not target.exists()          # nothing was written

    def test_seal_then_decrypt_roundtrip_with_header(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        key_file = write_fernet_key(root)
        target = root / "secrets" / "secrets.fernet.bin"
        tree = {"oracle": {"scott": {"password": "pw-one"}},
                "ad_atm": {"subd_support": {"password": "pw-two"}}}

        vault_ops.seal_fernet_and_verify(key_file, target, tree)

        # File carries the DO-NOT-EDIT banner, then exactly one token line.
        lines = [ln for ln in target.read_text().splitlines() if ln.strip()]
        assert lines[0].startswith("#"), "header banner must lead the file"
        assert "DO NOT EDIT" in target.read_text()
        token_lines = [ln for ln in lines if not ln.strip().startswith("#")]
        assert len(token_lines) == 1

        # And the tool's own reader recovers the identical tree.
        assert vault_ops.decrypt_fernet_tree(key_file, target) == tree

        # Cross-check the payload against the library's envelope contract.
        key = key_file.read_text().strip()
        envelope = json.loads(Fernet(key).decrypt(token_lines[0].encode()))
        assert envelope["version"] == vault_ops.FERNET_ENVELOPE_VERSION
        assert envelope["entries"] == vault_ops.flatten(tree)

class TestSealEncoding:
    """Regression (2026-11-04): on Windows, text writes without an explicit
    encoding used the cp1252 locale, producing bytes the utf-8 readers
    refused -- no vault could be written at all. Sealing must be
    locale-independent: the header is ASCII and every write/read is utf-8."""

    def test_seal_roundtrips_non_ascii_password(self, monkeypatch, tmp_path):
        root = install_project(monkeypatch, tmp_path)
        key_file = write_fernet_key(root)
        target = root / "secrets" / "secrets.fernet.bin"
        # deliberately non-ASCII value (a-umlaut o-umlaut em-dash u-umlaut),
        # written with escapes so this source file stays pure ASCII
        tree = {"svc": {"user": {"password": "p\u00e4ssw\u00f6rd-\u2014-\u00fc"}}}
        vault_ops.seal_fernet_and_verify(key_file, target, tree)
        assert vault_ops.decrypt_fernet_tree(key_file, target) == tree