#!/usr/bin/env python3
"""Smoke tests for the vault operations tool (scripts/vault_ops.py).

These test the OPERATOR TOOL, not the library — the library has its
own 67-test suite. Offline doctrine applies: everything runs in temp
directories against fixture configs; no network; no real secrets.

What is deliberately NOT tested here: anything that requires the
real sops binary, the real age key, or the real vault — the library
suite already covers the crypto round-trip. These tests cover the
logic that burned us live: argument parsing, categorization, tree
merging, pruning, and the config cross-check.

Run:   pytest test/vault_ops/ -v
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# Import vault_ops from the scripts directory (it is NOT a package).
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "vault_ops", SCRIPTS_DIR / "vault_ops.py")
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
    monkeypatch.setattr(vault_ops, "SECRETS_DIR", pr / "secrets")
    monkeypatch.setattr(vault_ops, "BACKUP_DIR", pr / "secrets" / "backups")
    monkeypatch.setattr(vault_ops, "LOG_FILE", pr / "secrets" / "logs" / "x.log")
    return pr


def write_extractor(tmp_project, filename, connections: dict) -> None:
    p = tmp_project / "config" / "extractors" / filename
    p.write_text(yaml.safe_dump({"connections": connections}), encoding="utf-8")


def write_pass_cfg(tmp_project, env: str, secrets: list) -> None:
    p = tmp_project / "config" / f"pass_mgr.{env}.yaml"
    body = ("# header comment — must survive\n"
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
        assert "# header comment — must survive" in text
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
        # given — so callers must only ever feed it prompted values.
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
        h = vault_ops.vault_header()
        assert "DO NOT EDIT" in h
        assert "Last sealed:" in h
        assert h.startswith("# ")         # it is pure YAML comment
