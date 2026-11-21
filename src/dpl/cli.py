# dpl/cli.py
"""Umbrella CLI: ``dpl`` command family (PT-003 / PT-004).

Surface (design doc Rev 5):
    dpl                          full help
    dpl --version / -v           version, read LIVE from pyproject.toml
    dpl --functionality          capability catalog from func_registry
    dpl run [...args]            forwards to dpl.wrapper.run:main(argv)
    dpl update-history [...args] forwards to dpl.wrapper.update_history:main(argv)

Deliberately NOT a verb (Rev 5, D-11): generate-schemas - deferred
pending PT-040 merge (args.settings bug on main; fix entangled with
the environment redesign). Revival = micro-item after PT-040 merges.

Security posture: --functionality parses registered source files with
the ``ast`` module ONLY. No inventory module is ever imported, so no
pipeline code, no connections, no vault access can run during catalog
printing. Deprecation shims (dpl-run / dpl-update-history) print one
notice line on stderr, then delegate with identical argv and pass the
exit code through unchanged. The wrapped mains are never edited.

Version truth (Q-3, resolved 2026-11-16): pyproject.toml. We read it
live via tomllib so a bumped version shows up with no code edit; the
importlib.metadata fallback covers non-editable installs where the
pyproject file is not shipped alongside the package.

Exit codes:
    0 - success (version printed, catalog printed, or the dispatched
        command returned 0)
    2 - usage error (argparse convention; unknown verb)
    otherwise - whatever the dispatched command returned, unchanged
"""

from __future__ import annotations

import argparse
import ast
import sys
from importlib import metadata
from pathlib import Path

from dpl import func_registry

PACKAGE_ROOT = Path(__file__).resolve().parent          # .../src/dpl
PROJECT_ROOT = PACKAGE_ROOT.parent.parent               # .../<project>

CATEGORY_TITLES = (
    "extraction",
    "schema",
    "storage",
    "secrets",
    "notifications",
    "operations",
)


# --------------------------------------------------------------------------
# version (PT-003)
# --------------------------------------------------------------------------
def _read_pyproject_version() -> str | None:
    """Live-read [project].version from the pyproject.toml beside the package."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return None
    import tomllib

    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) and version else None


def get_version() -> str:
    """Return the live version: pyproject first, installed metadata second."""
    version = _read_pyproject_version()
    if version is not None:
        return version
    try:
        return metadata.version("dpl")
    except metadata.PackageNotFoundError:
        return "unknown"


# --------------------------------------------------------------------------
# functionality catalog (PT-004)
# --------------------------------------------------------------------------
def _extract_func_msg(path: Path) -> dict[str, str] | None:
    """AST-parse one module file and pull its __func_msg__ dict.

    Pure static analysis: the file is read as text and parsed; it is
    never imported and never executed. Returns None if the module
    carries no __func_msg__ at module level.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "__func_msg__"
                for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            raw: dict[str, str] = {}
            for key_node, val_node in zip(node.value.keys, node.value.values):
                if not isinstance(key_node, ast.Constant):
                    continue
                key = key_node.value
                if not isinstance(key, str):
                    continue
                if isinstance(val_node, ast.Constant) and isinstance(
                    val_node.value, str
                ):
                    raw[key] = val_node.value
                else:
                    # multi-line purpose built from concatenated fragments -
                    # statically evaluatable, still zero execution
                    try:
                        evaluated = ast.literal_eval(val_node)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(evaluated, str):
                        raw[key] = evaluated
            if raw:
                return raw
    return None


def _render_entry(label: str, purpose: str, indent: str) -> str:
    """Render one catalog entry; multi-line purposes get continuation indents."""
    lines = purpose.split("\n")
    out = f"{indent}{label:<26}{lines[0]}"
    for continuation in lines[1:]:
        out += f"\n{indent}{'':26}{continuation}"
    if len(lines) > 1:
        out += "\n"           # blank line after expanded entries only
    return out


def build_functionality_text() -> str:
    """Assemble the catalog: header with live version, grouped by category."""
    entries: dict[str, list[tuple[str, str]]] = {c: [] for c in CATEGORY_TITLES}
    for rel in func_registry.MODULES:
        msg = _extract_func_msg(PACKAGE_ROOT / rel)
        if msg is None:
            print(
                f"dpl --functionality: WARNING: registered module "
                f"'{rel}' has no __func_msg__ (registry drift)",
                file=sys.stderr,
            )
            continue
        label = msg.get("invocation") or msg.get("name", rel)
        if label == "(internal module, not a command)":
            label = msg.get("name", rel)
        entries.setdefault(msg.get("category", "operations"), []).append(
            (label, msg.get("purpose", ""))
        )

    out = [f"dpl {get_version()} — functionality inventory"]
    for category in CATEGORY_TITLES:
        bucket = entries.get(category)
        if not bucket:
            continue
        out.append("")
        out.append(category.upper())
        for label, purpose in sorted(bucket):
            out.append(_render_entry(label, purpose, indent="  "))
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------
def _forward(module: str, func: str, argv: list[str]) -> int:
    """Import the wrapped main lazily and forward argv verbatim."""
    import importlib

    mod = importlib.import_module(module)
    rc = getattr(mod, func)(argv)
    return rc if isinstance(rc, int) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dpl",
        description=(
            "DPL umbrella CLI. Use 'dpl run' to orchestrate the pipeline, "
            "'dpl --functionality' for the capability catalog, "
            "'dpl --version' for the version."
        ),
        epilog="commands: run, update-history (remaining args forwarded verbatim)",
    )
    parser.add_argument(
        "-v", "--version", action="store_true",
        help="print the dpl version (read live from pyproject.toml)"
    )
    parser.add_argument(
        "--functionality", action="store_true",
        help="print the dpl capability catalog"
    )

    args, rest = parser.parse_known_args(argv)

    if args.version:
        print(f"dpl {get_version()}")
        return 0
    if args.functionality:
        print(build_functionality_text(), end="")
        return 0

    # hand-rolled verb routing (not argparse subparsers): the wrapped mains
    # own their parsers entirely, including their own --help; forwarding
    # stays verbatim and unknown-verb handling stays deterministic.
    verb = rest[0] if rest else None
    tail = rest[1:]
    if verb == "run":
        return _forward("dpl.wrapper.run", "main", tail)
    if verb == "update-history":
        return _forward("dpl.wrapper.update_history", "main", tail)
    if verb is None:
        parser.print_help()
        return 0

    print(f"dpl: unknown command '{verb}' (see 'dpl --help')", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# deprecation shims (targets of the OLD pyproject entry points)
# --------------------------------------------------------------------------
def _shim(old_name: str, new_invocation: str, module: str, func: str) -> int:
    print(
        f"note: '{old_name}' continues to work; consider '{new_invocation}'",
        file=sys.stderr,
    )
    return _forward(module, func, sys.argv[1:])


def run_shim() -> int:
    """Entry point for the legacy 'dpl-run' script name."""
    return _shim("dpl-run", "dpl run", "dpl.wrapper.run", "main")


def update_history_shim() -> int:
    """Entry point for the legacy 'dpl-update-history' script name."""
    return _shim(
        "dpl-update-history", "dpl update-history",
        "dpl.wrapper.update_history", "main",
    )


if __name__ == "__main__":
    sys.exit(main())
