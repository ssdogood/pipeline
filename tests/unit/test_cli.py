# tests/unit/test_cli.py
"""PT-003/PT-004: umbrella CLI, version, functionality, registry, shims."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from dpl import cli, func_registry

SRC_DPL = Path(cli.__file__).resolve().parent          # .../src/dpl
TESTS_DIR = Path(__file__).resolve().parent.parent     # .../tests/

CATEGORY_VOCAB = {
    "extraction", "schema", "storage", "secrets", "notifications",
    "operations",
}
KEY_ORDER = ["name", "invocation", "category", "purpose"]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _registry_paths() -> tuple[str, ...]:
    return func_registry.MODULES


def _registry_file_text() -> str:
    return (SRC_DPL / "func_registry.py").read_text(encoding="utf-8")


def _module_func_msg(rel: str) -> dict | None:
    return cli._extract_func_msg(SRC_DPL / rel)


# ----------------------------------------------------------------------
# version (PT-003)
# ----------------------------------------------------------------------
class TestVersion:
    def test_version_matches_pyproject(self):
        pyproject = SRC_DPL.parent.parent / "pyproject.toml"
        expected = tomllib.loads(pyproject.read_text())["project"]["version"]
        assert cli.get_version() == expected

    def test_live_read_not_hardcoded(self, tmp_path, monkeypatch):
        """Tmp-patched bumped pyproject must yield the bumped number."""

        source = SRC_DPL.parent.parent / "pyproject.toml"
        text = source.read_text().replace('version = "0.3.0"', 'version = "9.9.9"')
        fake_root = tmp_path
        (fake_root / "pyproject.toml").write_text(text)
        # temporarily repoint PROJECT_ROOT at the tmp tree
        monkeypatch.setattr(cli, "PROJECT_ROOT", fake_root)
        assert cli.get_version() == "9.9.9"

    def test_fallback_when_no_pyproject(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cli.metadata, "version", lambda _name: "1.2.3")
        assert cli.get_version() == "1.2.3"

    def test_fallback_unknown_package(self, tmp_path, monkeypatch):
        from importlib import metadata as _md

        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        def raise_missing(_name):
            raise _md.PackageNotFoundError("dpl")

        monkeypatch.setattr(cli.metadata, "version", raise_missing)
        assert cli.get_version() == "unknown"

    def test_main_dash_v_prints_version(self, capsys):
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("dpl ")
        assert cli.main(["-v"]) == 0


# ----------------------------------------------------------------------
# functionality catalog (PT-004)
# ----------------------------------------------------------------------
class TestFunctionality:
    def test_header_carries_live_version(self, capsys):
        assert cli.main(["--functionality"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("dpl ")
        assert "functionality inventory" in out

    def test_every_registered_module_appears(self, capsys):
        cli.main(["--functionality"])
        out = capsys.readouterr().out
        for rel in _registry_paths():
            msg = _module_func_msg(rel)
            assert msg is not None, f"{rel} missing __func_msg__"
            label = msg.get("invocation") or msg.get("name")
            if label == "(internal module, not a command)":
                label = msg.get("name", rel)   # renderer's internal-module rule
            assert label in out, f"{label} ({rel}) absent from catalog"

    def test_categories_are_valid_vocab(self, capsys):
        cli.main(["--functionality"])
        out = capsys.readouterr().out
        for line in out.splitlines():
            if line and line == line.upper() and not line.startswith(" "):
                assert line.lower() in CATEGORY_VOCAB, f"unexpected section: {line}"

    def test_known_multi_line_entry_expands(self, capsys):
        """run.py's numbered purpose must render continuation lines indented."""
        cli.main(["--functionality"])
        out = capsys.readouterr().out
        assert "1. pipes come from config/run.yaml" in out
        assert "2. add a pipe" in out

    def test_exit_zero(self, capsys):
        assert cli.main(["--functionality"]) == 0


# ----------------------------------------------------------------------
# registry consistency - the gate-time bidirectional sweep
# ----------------------------------------------------------------------
class TestRegistryConsistency:
    def test_a_every_registered_module_has_valid_block(self):
        for rel in _registry_paths():
            path = SRC_DPL / rel
            assert path.is_file(), f"registry points at missing file: {rel}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            blocks = [
                n for n in tree.body
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "__func_msg__"
                    for t in n.targets
                )
            ]
            assert len(blocks) == 1, f"{rel}: expected exactly one __func_msg__"
            value = blocks[0].value
            assert isinstance(value, ast.Dict), f"{rel}: __func_msg__ is not a dict"
            keys = [
                k.value
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            assert keys == KEY_ORDER, f"{rel}: key order {keys}"

    def test_a2_category_vocabulary(self):
        for rel in _registry_paths():
            msg = _module_func_msg(rel)
            assert msg is not None, rel
            assert msg["category"] in CATEGORY_VOCAB, rel

    def test_a3_purpose_never_ends_with_newline(self):
        for rel in _registry_paths():
            msg = _module_func_msg(rel)
            assert msg is not None, rel
            assert not msg["purpose"].endswith("\n"), rel

    def test_b_every_carrier_is_registered(self):
        """Full-tree sweep: dunder name makes this collision-proof."""
        carriers = set()
        for p in SRC_DPL.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__func_msg__"
                        for t in node.targets
                    )
                ):
                    carriers.add(str(p.relative_to(SRC_DPL)))
        registered = set(_registry_paths())
        unregistered = sorted(carriers - registered)
        assert not unregistered, f"carriers missing from registry: {unregistered}"

    def test_registry_is_pure_data(self):
        tree = ast.parse(_registry_file_text())
        assert not [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ], "func_registry.py must import nothing"

    def test_no_duplicate_entries(self):
        paths = _registry_paths()
        assert len(paths) == len(set(paths))


# ----------------------------------------------------------------------
# routing + no-execution security
# ----------------------------------------------------------------------
class TestRouting:
    def test_run_verb_forwards_argv(self, monkeypatch):
        seen = {}

        class FakeMod:
            @staticmethod
            def main(argv):
                seen["argv"] = argv
                return 7

        monkeypatch.setitem(sys.modules, "dpl.wrapper.run", FakeMod)
        assert cli.main(["run", "--profile", "inc", "--extra"]) == 7
        assert seen["argv"] == ["--profile", "inc", "--extra"]

    def test_update_history_verb_forwards_argv(self, monkeypatch):
        seen = {}

        class FakeMod:
            @staticmethod
            def main(argv):
                seen["argv"] = argv
                return 3

        monkeypatch.setitem(sys.modules, "dpl.wrapper.update_history", FakeMod)
        assert cli.main(["update-history", "--db", "x.db"]) == 3
        assert seen["argv"] == ["--db", "x.db"]

    def test_unknown_verb_exits_nonzero(self, capsys):
        assert cli.main(["fly"]) == 2
        assert "unknown command 'fly'" in capsys.readouterr().err

    def test_non_int_return_treated_as_success(self, monkeypatch):
        class FakeMod:
            @staticmethod
            def main(argv):
                return None

        monkeypatch.setitem(sys.modules, "dpl.wrapper.run", FakeMod)
        assert cli.main(["run"]) == 0

    def test_no_inventory_module_imported_during_catalog(self, capsys, monkeypatch):
        """Security posture: --functionality must not import ANY registered module."""
        import importlib

        imported: list[str] = []

        real_import = importlib.import_module

        def spy(name, *a, **kw):
            if name.startswith("dpl.") and not name.startswith("dpl.cli"):
                imported.append(name)
            return real_import(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", spy)
        cli.main(["--functionality"])
        # only func_registry and cli itself are ever imported by the catalog
        assert not imported, f"catalog imported: {imported}"


# ----------------------------------------------------------------------
# deprecation shims
# ----------------------------------------------------------------------
class TestShims:
    def test_run_shim_notices_then_delegates(self, capsys, monkeypatch):
        seen = {}

        class FakeMod:
            @staticmethod
            def main(argv):
                seen["argv"] = argv
                return 5

        monkeypatch.setitem(sys.modules, "dpl.wrapper.run", FakeMod)
        monkeypatch.setattr(sys, "argv", ["dpl-run", "--profile", "inc"])
        assert cli.run_shim() == 5
        err = capsys.readouterr().err
        assert err.count("dpl-run") == 1 and "dpl run" in err
        assert seen["argv"] == ["--profile", "inc"]

    def test_update_history_shim_notices_then_delegates(self, capsys, monkeypatch):
        class FakeMod:
            @staticmethod
            def main(argv):
                return 4

        monkeypatch.setitem(sys.modules, "dpl.wrapper.update_history", FakeMod)
        monkeypatch.setattr(sys, "argv", ["dpl-update-history"])
        assert cli.update_history_shim() == 4
        err = capsys.readouterr().err
        assert "consider 'dpl update-history'" in err

    def test_shim_notice_goes_to_stderr_not_stdout(self, capsys, monkeypatch):
        class FakeMod:
            @staticmethod
            def main(argv):
                return 0

        monkeypatch.setitem(sys.modules, "dpl.wrapper.run", FakeMod)
        monkeypatch.setattr(sys, "argv", ["dpl-run"])
        cli.run_shim()
        captured = capsys.readouterr()
        assert "note:" in captured.err
        assert "note:" not in captured.out
