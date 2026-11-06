#!/usr/bin/env python3
"""Build the dpl release bundle.

Doctrine (locked in design, 2026-11):

  * DEFAULT-DENY. Only paths on the allowlists below are staged into
    the release tree. Everything not explicitly listed is excluded.
    The dry-run makes every exclusion visible, because a filter you
    cannot see is a filter you cannot trust.
  * bin/ ships its MANIFEST ONLY. Executables are never bundled;
    the deployer downloads the pinned tools per the manifest URLs.
  * Tests never ship. tests/ is deliberately absent from every
    allowlist.
  * Empty runtime directories (logs/, secrets/, ...) are FABRICATED
    in the staging tree at build time - they are not taken from the
    repo, because empty dirs cannot be committed to git anyway.

Phase plan for this file:
  Phase 1: CLI, version stamp from pyproject.toml, allowlist engine,
           --dry-run inventory. (THIS PHASE - nothing is written.)
  Phase 2: staging tree construction + .gitkeep scaffolding.
  Phase 3: tar.gz + zip + SHA256SUMS into dist/.
  Phase 4: clean-room self-certification.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

# The repo root is the parent of scripts/ (where this file lives).
# Resolving from __file__ (not from the CWD) means the script behaves
# identically no matter where it is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Allowlists - THE definition of what ships. Default-deny everywhere else.
# ---------------------------------------------------------------------------

#: Directories copied RECURSIVELY - every file inside ships.
WHOLE_DIR_ALLOWLIST = ("config", "docs", "scripts")

#: Root-level files that ship. Keep alphabetical for easy auditing.
ROOT_FILE_ALLOWLIST = (
    "LICENSE",
    "pyproject.toml",
    "run_offline_tests.bat",
    "run_offline_tests.sh",
    "setup_secrets.bat",
    "setup_secrets.sh",
    "vault.bat",
    "vault.sh",
)

#: Within src/, ONLY the package subtree ships. Anything else that
#: ever appears in src/ (an egg-info dir, a stray note) is denied.
SRC_PACKAGE_PREFIX = ("src", "dpl")

#: bin/ ships exactly one file: the checksum manifest.
BIN_MANIFEST_PATH = "bin/sops_manifest.txt"

#: Runtime directories fabricated EMPTY in the staging tree at build
#: time (Phase 2). Declared here so the dry-run can report them.
RUNTIME_SCAFFOLD_DIRS = (
    "logs",
    "secrets",
    "secrets/backups",
    "secrets/logs",
)

#: Top-level repo entries the dry-run walker prunes entirely: build
#: artifacts and tool caches that would flood the listing with noise
#: (and are default-denied regardless).
WALK_PRUNE_TOP = (
    ".conda",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".vscode",
    "build",
    "dist",
)


# ---------------------------------------------------------------------------
# Version stamp
# ---------------------------------------------------------------------------

def read_project_meta(project_root: Path) -> tuple[str, str]:
    """Return (name, version) from pyproject.toml.

    Single source of truth: the version stamped on the archives is the
    version declared by the project itself - hand-copying versions into
    filenames is how mismatches are born.
    """
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        sys.exit(f"FATAL: {pyproject} not found.")
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        name = data["project"]["name"]
        version = data["project"]["version"]
    except KeyError as exc:
        sys.exit(
            f"FATAL: pyproject.toml is missing [project] name/version "
            f"(missing key: {exc})."
        )
    return str(name), str(version)


# ---------------------------------------------------------------------------
# Classification engine - the heart of default-deny
# ---------------------------------------------------------------------------

def classify(rel: Path) -> tuple[bool, str]:
    """Decide whether one repo-relative file path ships.

    Returns (included?, human_reason). Order matters: hygiene denials
    are checked FIRST, so a stray __pycache__ inside an allowlisted
    directory can never ride along.
    """
    parts = rel.parts
    top = parts[0]

    # Hygiene first - applies even inside allowlisted dirs.
    if "__pycache__" in parts or rel.suffix in (".pyc", ".pyo"):
        return False, "bytecode/cache (hygiene)"
    if any(part.startswith(".") for part in parts):
        return False, "hidden entry at any depth (default-deny)"

    # Whole-directory allowlist.
    if top in WHOLE_DIR_ALLOWLIST:
        return True, f"{top}/ (recursive allowlist)"

    # src/: only the package subtree.
    if top == "src":
        if len(parts) >= 2 and parts[1] == SRC_PACKAGE_PREFIX[1]:
            return True, "src/dpl/ (package code)"
        return False, "src/ outside dpl/ (default-deny)"

    # bin/: manifest only, never executables.
    if top == "bin":
        if rel.as_posix() == BIN_MANIFEST_PATH:
            return True, "bin/sops_manifest.txt (executables never ship)"
        return False, "bin/ executable (never bundled)"

    # Root-level file allowlist.
    if rel.as_posix() in ROOT_FILE_ALLOWLIST:
        return True, "root file allowlist"

    # Everything else - including tests/, demo/, notes/ - falls through.
    return False, "default-deny"


def walk_classified(project_root: Path) -> list[tuple[Path, bool, str]]:
    """Walk the repo, returning (rel_path, included?, reason) per file.

    os.walk with in-place dirnames filtering actually prunes the walk
    (rglob cannot prune, so .git would be crawled file-by-file).
    Output is sorted for deterministic, diffable listings.
    """
    results: list[tuple[Path, bool, str]] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = Path(dirpath).relative_to(project_root)
        at_root = not rel_dir.parts
        if at_root:
            dirnames[:] = sorted(d for d in dirnames if d not in WALK_PRUNE_TOP)
        dirnames[:] = sorted(
            d for d in dirnames if d != "__pycache__"
        )
        for fname in sorted(filenames):
            rel = rel_dir / fname if not at_root else Path(fname)
            included, reason = classify(rel)
            results.append((rel, included, reason))
    results.sort(key=lambda row: row[0].as_posix())
    return results


# ---------------------------------------------------------------------------
# Source-tree validation - fail before building, not after shipping
# ---------------------------------------------------------------------------

def validate_source_tree(project_root: Path) -> None:
    """Every allowlist entry MUST exist in the repo.

    An allowlist pointing at a missing file is a typo that silently
    ships an incomplete bundle. Refuse to proceed instead.
    """
    missing: list[str] = []
    for entry in WHOLE_DIR_ALLOWLIST:
        if not (project_root / entry).is_dir():
            missing.append(f"{entry}/")
    if not (project_root.joinpath(*SRC_PACKAGE_PREFIX)).is_dir():
        missing.append("src/dpl/")
    for entry in (*ROOT_FILE_ALLOWLIST, BIN_MANIFEST_PATH):
        if not (project_root / entry).is_file():
            missing.append(entry)
    if missing:
        sys.exit(
            "FATAL: allowlist entries missing from the repo:\n  "
            + "\n  ".join(missing)
        )


# ---------------------------------------------------------------------------
# Dry-run report
# ---------------------------------------------------------------------------

def dry_run(project_root: Path, verbose: bool) -> None:
    name, version = read_project_meta(project_root)
    rows = walk_classified(project_root)

    shipped = [row for row in rows if row[1]]
    denied = [row for row in rows if not row[1]]

    # Group by (side, reason) for the summary.
    def group_by(
        subset: list[tuple[Path, bool, str]]
    ) -> dict[str, list[Path]]:
        groups: dict[str, list[Path]] = {}
        for rel, _inc, reason in subset:
            groups.setdefault(reason, []).append(rel)
        return groups

    print("dpl release bundle - DRY RUN (nothing is written)")
    print(f"  project : {name}")
    print(f"  version : {version}  (stamped from pyproject.toml)")
    print(f"  source  : {project_root}")
    print()
    print(f"WOULD SHIP - {len(shipped)} files")
    for reason, paths in sorted(group_by(shipped).items()):
        print(f"  [{reason}] x{len(paths)}")
        if verbose:
            for rel in paths:
                print(f"      {rel.as_posix()}")
    print()
    print(f"WOULD EXCLUDE - {len(denied)} files (default-deny protects you)")
    for reason, paths in sorted(group_by(denied).items()):
        print(f"  [{reason}] x{len(paths)}")
        if verbose:
            for rel in paths:
                print(f"      {rel.as_posix()}")
    print()
    print("Fabricated empty at build time (not from the repo):")
    for d in RUNTIME_SCAFFOLD_DIRS:
        print(f"  {d}/")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_release.py",
        description=(
            "Build the dpl release bundle (tar.gz + zip + SHA256SUMS). "
            "Phase 1: allowlist inspection only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a build would stage and exclude, then exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="with --dry-run: list every file, not just per-rule counts",
    )
    args = parser.parse_args(argv)

    validate_source_tree(PROJECT_ROOT)

    if not args.dry_run:
        print("Nothing implemented beyond --dry-run yet (Phase 1).")
        return 2
    dry_run(PROJECT_ROOT, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
