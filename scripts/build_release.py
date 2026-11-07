#!/usr/bin/env python3
"""Build the dpl release bundle.

Doctrine (locked in design, 2026-11):

  * DEFAULT-DENY. Only paths on the allowlists below are staged into
    the release tree. Everything not explicitly listed is excluded.
    The dry-run makes every exclusion visible, because a filter you
    cannot see is a filter you cannot trust.
  * NO hidden file ships at any depth (belt: the classifier denies
    them; braces: the staging verifier re-checks every staged file).
  * TEST CODE NEVER SHIPS TO PRODUCTION. tests/ AND the green
    certification scripts (run_offline_tests.*) live in the separate
    COMPANION TEST KIT, never in the production bundle. The receiver
    certifies by overlaying the kit onto an unpack of the bundle in a
    throwaway directory, then deleting that whole directory.
  * bin/ ships its MANIFEST ONLY. Executables are never bundled; the
    deployer downloads the pinned tools per the manifest URLs.
    Consequently a clean-room certification lands in FERNET-ONLY
    mode - which is REQUIRED: a full-strength GREEN in the clean room
    means binaries leaked into the bundle.
  * Empty runtime directories (logs/, secrets/, ...) are FABRICATED
    in the staging tree at build time, as is README-FIRST.txt (so the
    instructions always carry the version of the archive they ship in).
    Scaffold-dir verification applies to the BUNDLE only - the kit
    has no scaffolds and opts out explicitly.
  * Archives are METADATA-NORMALIZED: fixed timestamps, uid/gid 0,
    sorted member order, mtime-less gzip header. Two builds of the
    same tree on the same platform produce byte-identical archives.
    (Honest caveat: DEFLATE output may differ across zlib versions.)

Phase plan for this file:
  Phase 1: CLI, version stamp, allowlist engine, --dry-run. (DONE)
  Phase 2: staging tree + scaffolding + re-verification. (DONE)
  Phase 3: tar.gz + zip + SHA256SUMS, member verification. (DONE)
  Phase 4: test kit archives, README-FIRST.txt fabrication, and the
           CLEAN ROOM: unpack bundle, overlay kit, run the kit's own
           green script against the unpacked code, require the
           honest FERNET-ONLY GREEN banner. (DONE)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

# The repo root is the parent of scripts/ (where this file lives).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Allowlists - THE definition of what ships. Default-deny everywhere else.
# ---------------------------------------------------------------------------

#: Directories copied RECURSIVELY - every file inside ships.
WHOLE_DIR_ALLOWLIST = ("config", "docs", "scripts")

#: Root-level files that ship. NOTE: run_offline_tests.* live in the
#: TEST KIT, not here - certification is test tooling.
#: environment.yml ships so a receiver can build the exact runtime.
ROOT_FILE_ALLOWLIST = (
    "LICENSE",
    "environment.yml",
    "pyproject.toml",
    "setup_secrets.bat",
    "setup_secrets.sh",
    "vault.bat",
    "vault.sh",
)

#: Within src/, ONLY the package subtree ships.
SRC_PACKAGE_PREFIX = ("src", "dpl")

#: bin/ ships exactly one file: the checksum manifest.
BIN_MANIFEST_PATH = "bin/sops_manifest.txt"

#: Test-kit contents: everything under tests/, plus the green scripts.
KIT_WHOLE_DIR = "tests"
KIT_ROOT_FILES = ("run_offline_tests.bat", "run_offline_tests.sh")

#: Runtime directories fabricated EMPTY at build time (BUNDLE only).
RUNTIME_SCAFFOLD_DIRS = (
    "logs",
    "secrets",
    "secrets/backups",
    "secrets/logs",
)

#: Marker file placed in each fabricated runtime dir.
GITKEEP_NAME = ".gitkeep"

#: Receiver instructions, fabricated at build time (version-substituted).
README_FIRST_NAME = "README-FIRST.txt"

#: Top-level repo entries the dry-run walker prunes entirely.
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
# Archive determinism knobs
# ---------------------------------------------------------------------------

FIXED_MTIME = 1704067200  # 2024-01-01T00:00:00Z
ZIP_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # zip format epoch floor

# ---------------------------------------------------------------------------
# README-FIRST.txt - fabricated, never committed (see doctrine above)
# ---------------------------------------------------------------------------

README_FIRST_TEMPLATE = """dpl data pipeline - READ THIS FIRST
=====================================

You have unpacked a PRODUCTION copy of the dpl data pipeline
extraction project (version {version}). Read this before doing
anything else.

WHAT IS DELIBERATELY NOT IN THIS DIRECTORY
------------------------------------------
1. TESTS. Test code never ships to production. That is a design rule
   of this project, not an omission.

2. SOPS and age EXECUTABLES. bin/ contains only sops_manifest.txt -
   the SHA-256 checksums and official download URLs of the pinned
   tools. Fernet (pure Python) is the default encryption engine, so
   this tree is fully usable without them. If you want SOPS support,
   download the tools exactly as described in bin/sops_manifest.txt
   and let the checksum verify them.

3. secrets/ and logs/ CONTENTS. These directories exist but are empty
   by design. They will be populated by setup_secrets.sh and by
   normal operation respectively.

VERIFY INTEGRITY BEFORE TRUSTING THESE FILES
--------------------------------------------
The release ships a SHA256SUMS file beside the archives. From the
directory where you downloaded them:

    shasum -a 256 -c SHA256SUMS          (macOS / Linux)

Compare every "OK". If any line fails, do not use the files.

SETUP - BUILD THE PYTHON ENVIRONMENT (once per machine)
-------------------------------------------------------
Prerequisite for BOTH certification and day-to-day running.

Division of labour: environment.yml provides the Python
INTERPRETER; the package's own dependency list (pyproject.toml)
provides every PACKAGE. Nothing is declared in two places, so
nothing can drift between machines.

ALL commands in this section are run from INSIDE the unpacked
dpl-{version}/ folder - so first:

    cd dpl-{version}

a. Install Miniconda if conda is not already on the machine
   (free download: docs.conda.io).

b. Build and activate the environment:

       conda env create -f environment.yml
       conda activate dpl

c. Install the pipeline and ALL its dependencies in one command.
   Use the [test] flavour unless this machine will never certify
   (it adds pytest, required by the certification below):

       pip install ".[test]"

   NOTE - OLDER CPUs: if polars aborts with
   "Windows fatal exception 0xc000001d" or warns "Missing
   required CPU features", the processor is pre-AVX2. Fix once:

       pip uninstall -y polars
       pip install polars-lts-cpu

d. Point the pipeline at its root. This is NOT needed for the
   certification below (the test suite finds the tree it runs in),
   but it IS required for normal operation, and especially for
   cron. Point it at the PRODUCTION unpack - the tree you will
   KEEP - never at a throwaway certification directory:

       export DPL_HOME=/path/to/your/production/dpl-{version}

   Make it permanent by adding both lines to your shell profile:
       conda activate dpl
       export DPL_HOME=/path/to/your/production/dpl-{version}

   Advanced (optional): DPL_LOG_DIR overrides the default logs/
   location; DPL_SETTINGS overrides the default settings file.

HOW TO CERTIFY THIS TREE (about 2 minutes - recommended)
--------------------------------------------------------
Although tests are not in this tree, you can still certify your
unpack of it using the COMPANION TEST KIT from the same release.
Prerequisite: the SETUP section above must already be done on this
machine - certification runs the test suite, which needs the dpl
environment (but NOT DPL_HOME).

1. Create a throwaway directory (your "certification room").
   Work there, not in your production unpack.

2. Unpack a FRESH copy of this bundle INSIDE the certification
   room, so you have cert-room/dpl-{version}/ .

3. Download the test kit from the same release:
   dpl-{version}-testkit.tar.gz (or dpl-{version}-testkit.zip on
   Windows).

4. Unpack the test kit INTO THE SAME certification room. It overlays
   the same dpl-{version}/ folder, adding tests/ and the
   certification scripts on top.

5. Run the certification:

       cd dpl-{version}
       conda activate dpl
       ./run_offline_tests.sh            (macOS / Linux)
       run_offline_tests.bat             (Windows)

6. EXPECT THIS RESULT - GREEN, labelled (FERNET-ONLY):
   every suite passes; the SOPS/Age-dependent tests are SKIPPED,
   visibly listed, because the executables are not shipped.
   Two deviations mean different things:
     - a full-strength GREEN (no FERNET-ONLY label) means someone
       placed binaries in bin/ - investigate before trusting.
     - anything RED means the tree is damaged or altered - do not
       use it.

7. WHEN DONE: DELETE THE ENTIRE CERTIFICATION ROOM. It contained
   test code. Never run production from a certified directory.

8. For production, use your ORIGINAL untouched unpack of this bundle
   (or unpack the archive again into its final location) - the one
   DPL_HOME points at.

DAY-TO-DAY OPERATION
--------------------
Consult the operations documentation shipped in docs/, and the
helper scripts at the top level (vault.sh, setup_secrets.sh).
"""


# ---------------------------------------------------------------------------
# Version stamp
# ---------------------------------------------------------------------------

def read_project_meta(project_root: Path) -> tuple[str, str]:
    """Return (name, version) from pyproject.toml.

    Single source of truth: the version stamped on the archives is the
    version declared by the project itself.
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

def _hygiene_deny(parts: tuple[str, ...], suffix: str) -> bool:
    return "__pycache__" in parts or suffix in (".pyc", ".pyo")


def classify(rel: Path) -> tuple[bool, str]:
    """Decide whether one repo-relative file path ships in the BUNDLE."""
    parts = rel.parts
    top = parts[0]

    if _hygiene_deny(parts, rel.suffix):
        return False, "bytecode/cache (hygiene)"
    if any(part.startswith(".") for part in parts):
        return False, "hidden entry at any depth (default-deny)"

    if top in WHOLE_DIR_ALLOWLIST:
        return True, f"{top}/ (recursive allowlist)"
    if top == "src":
        if len(parts) >= 2 and parts[1] == SRC_PACKAGE_PREFIX[1]:
            return True, "src/dpl/ (package code)"
        return False, "src/ outside dpl/ (default-deny)"
    if top == "bin":
        if rel.as_posix() == BIN_MANIFEST_PATH:
            return True, "bin/sops_manifest.txt (executables never ship)"
        return False, "bin/ executable (never bundled)"
    if rel.as_posix() in ROOT_FILE_ALLOWLIST:
        return True, "root file allowlist"
    return False, "default-deny"


def classify_kit(rel: Path) -> tuple[bool, str]:
    """Decide whether one repo-relative file ships in the TEST KIT.

    Same hygiene rules as the bundle (no bytecode, no hidden files
    at any depth) so the kit can never smuggle strays either.
    """
    parts = rel.parts
    top = parts[0]

    if _hygiene_deny(parts, rel.suffix):
        return False, "bytecode/cache (hygiene)"
    if any(part.startswith(".") for part in parts):
        return False, "hidden entry at any depth (default-deny)"

    if top == KIT_WHOLE_DIR:
        return True, "tests/ (kit)"
    if rel.as_posix() in KIT_ROOT_FILES:
        return True, "green script (kit)"
    return False, "default-deny (kit)"


def walk_classified(
    project_root: Path, classifier=classify
) -> list[tuple[Path, bool, str]]:
    """Walk the repo, returning (rel_path, included?, reason) per file."""
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
            included, reason = classifier(rel)
            results.append((rel, included, reason))
    results.sort(key=lambda row: row[0].as_posix())
    return results


# ---------------------------------------------------------------------------
# Source-tree validation - fail before building, not after shipping
# ---------------------------------------------------------------------------

def validate_source_tree(project_root: Path) -> None:
    """Every allowlist entry (bundle AND kit) MUST exist in the repo."""
    missing: list[str] = []
    for entry in WHOLE_DIR_ALLOWLIST:
        if not (project_root / entry).is_dir():
            missing.append(f"{entry}/")
    if not (project_root.joinpath(*SRC_PACKAGE_PREFIX)).is_dir():
        missing.append("src/dpl/")
    for entry in (*ROOT_FILE_ALLOWLIST, BIN_MANIFEST_PATH):
        if not (project_root / entry).is_file():
            missing.append(entry)
    if not (project_root / KIT_WHOLE_DIR).is_dir():
        missing.append(f"{KIT_WHOLE_DIR}/")
    for entry in KIT_ROOT_FILES:
        if not (project_root / entry).is_file():
            missing.append(entry)
    if missing:
        sys.exit(
            "FATAL: allowlist entries missing from the repo:\n  "
            + "\n  ".join(missing)
        )


# ---------------------------------------------------------------------------
# Staging (shared machinery)
# ---------------------------------------------------------------------------

def _copy_classified(
    project_root: Path, staging_root: Path, classifier
) -> list[Path]:
    shipped_rel: list[Path] = []
    for rel, included, _reason in walk_classified(project_root, classifier):
        if not included:
            continue
        src = project_root / rel
        dst = staging_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        shipped_rel.append(rel)
    return shipped_rel


def _verify_staged(
    staging_root: Path,
    classifier,
    *,
    allowed_fabricated=None,
    scaffold_dirs: tuple[str, ...] = (),
) -> int:
    """Independently re-check a staged tree. Returns staged file count."""
    allowed_fabricated = allowed_fabricated or {}
    staged_count = 0
    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(staging_root):
        rel_dir = Path(dirpath).relative_to(staging_root)
        at_root = not rel_dir.parts
        dirnames[:] = sorted(dirnames)
        for fname in sorted(filenames):
            rel = rel_dir / fname if not at_root else Path(fname)
            posix = rel.as_posix()
            staged_count += 1
            # Fabricated files (markers, README-FIRST) are exempt.
            if posix in allowed_fabricated:
                continue
            if rel.name == GITKEEP_NAME:
                parent = rel.parent.as_posix()
                if parent in scaffold_dirs:
                    continue
                violations.append(f"stray {GITKEEP_NAME}: {posix}")
                continue
            ok, _reason = classifier(rel)
            if not ok:
                violations.append(f"default-deny file present: {posix}")
    if staged_count == 0:
        sys.exit("FATAL: staged tree is empty - refusing to certify nothing.")
    if violations:
        sys.exit(
            "FATAL: staged tree violates release doctrine:\n  "
            + "\n  ".join(sorted(violations))
        )
    return staged_count


def build_staging(project_root: Path, staging_root: Path, version: str) -> list[Path]:
    """Materialize the BUNDLE staging tree, incl. fabrications."""
    shipped_rel = _copy_classified(project_root, staging_root, classify)

    for scaffold in RUNTIME_SCAFFOLD_DIRS:
        d = staging_root / scaffold
        d.mkdir(parents=True, exist_ok=True)
        (d / GITKEEP_NAME).write_text(
            "Empty by design - fabricated at build time.\n", encoding="utf-8"
        )
    (staging_root / README_FIRST_NAME).write_text(
        README_FIRST_TEMPLATE.format(version=version), encoding="utf-8"
    )
    return shipped_rel


def build_kit_staging(project_root: Path, kit_root: Path) -> list[Path]:
    """Materialize the TEST KIT staging tree (tests + green scripts)."""
    return _copy_classified(project_root, kit_root, classify_kit)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Archiving
# ---------------------------------------------------------------------------

def _sorted_staged_entries(staging_root: Path) -> list[tuple[Path, str]]:
    entries = []
    for p in staging_root.rglob("*"):
        rel = p.relative_to(staging_root).as_posix()
        entries.append((p, rel))
    entries.sort(key=lambda row: row[1])
    return entries


def _normalize_tarinfo(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    ti.mtime = FIXED_MTIME
    ti.mode = ti.mode & 0o777
    return ti


def build_tar_gz(staging_root: Path, out_path: Path, prefix: str) -> None:
    with out_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as tf:
                for path, rel in _sorted_staged_entries(staging_root):
                    tf.add(
                        str(path),
                        arcname=f"{prefix}/{rel}",
                        recursive=False,
                        filter=_normalize_tarinfo,
                    )


def build_zip(staging_root: Path, out_path: Path, prefix: str) -> None:
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, rel in _sorted_staged_entries(staging_root):
            if path.is_dir():
                info = zipfile.ZipInfo(f"{prefix}/{rel}/", ZIP_FIXED_TIMESTAMP)
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, "")
            else:
                info = zipfile.ZipInfo(f"{prefix}/{rel}", ZIP_FIXED_TIMESTAMP)
                info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())


def snapshot_staged_files(staging_root: Path, prefix: str) -> dict[str, str]:
    snap: dict[str, str] = {}
    for path, rel in _sorted_staged_entries(staging_root):
        if path.is_file():
            snap[f"{prefix}/{rel}"] = sha256_of(path)
    return snap


def _verify_archive_members(
    names_and_digests: dict[str, str],
    snapshot: dict[str, str],
    required_dirs: list[str],
    label: str,
    dirs_present: set[str] | None = None,
) -> int:
    """Compare archive members against the staging snapshot.

    All directory-name comparisons are slash-normalized on BOTH sides
    (tar and zip append '/' to dir entry names) - learned the hard way
    2026-11-06: 'dpl-0.1.0/logs' != 'dpl-0.1.0/logs/' caused a false
    FATAL despite perfectly good archives.
    """
    problems: list[str] = []
    for arc in required_dirs:
        bare = arc.rstrip("/")
        in_files = bare in {n.rstrip("/") for n in names_and_digests}
        in_dirs = dirs_present is not None and bare in {
            d.rstrip("/") for d in dirs_present
        }
        if not in_files and not in_dirs:
            problems.append(f"empty scaffold dir missing from {label}: {arc}")
    for name, digest in names_and_digests.items():
        if name not in snapshot:
            problems.append(f"unexpected member in {label}: {name}")
        elif snapshot[name] != digest:
            problems.append(f"{label} member differs from staging: {name}")
    for name in snapshot:
        if name not in names_and_digests:
            problems.append(f"staged file missing from {label}: {name}")
    if problems:
        sys.exit(
            f"FATAL: {label} verification failed:\n  " + "\n  ".join(problems)
        )
    return len(names_and_digests)


def verify_tar_gz(
    tar_path: Path, snapshot: dict[str, str], prefix: str,
    scaffold_check: bool = True,
) -> int:
    with tarfile.open(tar_path, "r:gz") as tf:
        seen: dict[str, str] = {}
        dirs: set[str] = set()
        for m in tf.getmembers():
            if m.isfile():
                fh = tf.extractfile(m)
                assert fh is not None
                seen[m.name] = hashlib.sha256(fh.read()).hexdigest()
            elif m.isdir():
                dirs.add(m.name.rstrip("/"))
        # scaffold_check=False for the TEST KIT: it contains no runtime
        # scaffold dirs, and demanding them there was a false FATAL
        # (2026-11-06). Default True = fail-closed for future callers.
        required = (
            [f"{prefix}/{d}" for d in RUNTIME_SCAFFOLD_DIRS]
            if scaffold_check else []
        )
        return _verify_archive_members(seen, snapshot, required, "tar.gz", dirs)


def verify_zip(
    zip_path: Path, snapshot: dict[str, str], prefix: str,
    scaffold_check: bool = True,
) -> int:
    with zipfile.ZipFile(zip_path, "r") as zf:
        seen: dict[str, str] = {}
        dirs: set[str] = set()
        for name in zf.namelist():
            if name.endswith("/"):
                dirs.add(name.rstrip("/"))
            else:
                seen[name] = hashlib.sha256(zf.read(name)).hexdigest()
        required = (
            [f"{prefix}/{d}/" for d in RUNTIME_SCAFFOLD_DIRS]
            if scaffold_check else []
        )
        count = _verify_archive_members(seen, snapshot, required, "zip", dirs)
        green_arc = f"{prefix}/run_offline_tests.sh"
        # Only meaningful for the KIT zip - the bundle no longer carries
        # the green script. Generic guard:
        if green_arc in seen:
            mode = (zf.getinfo(green_arc).external_attr >> 16) & 0o777
            if not mode & 0o111:
                sys.exit(
                    f"FATAL: {green_arc} lost its executable bit in zip"
                )
        return count


# ---------------------------------------------------------------------------
# Clean room (Phase 4)
# ---------------------------------------------------------------------------

#: Bootstrap proving `import dpl` resolves INSIDE the unpacked bundle.
#: The bundle code must be what pytest tests - an editable install of
#: dpl on the build machine would otherwise happily shadow it.
PREFLIGHT_SNIPPET = (
    "import sys\n"
    "from pathlib import Path\n"
    "expected = Path(sys.argv[1]).resolve()\n"
    "import dpl\n"
    "got = Path(dpl.__file__).resolve()\n"
    "if expected not in got.parents:\n"
    "    sys.exit(f'PREFLIGHT FAIL: dpl imported from {got} - expected '\n"
    "             f'under {expected}. Editable-install shadowing or '\n"
    "             f'PYTHONPATH problem; refusing to certify the wrong tree.')\n"
    "print(f'PREFLIGHT OK: dpl resolved from {got}')\n"
)


def _unpack_tar(tar_path: Path, dest: Path) -> None:
    """Trusted unpack: every member was hash-verified seconds ago."""
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(dest, filter="fully_trusted")
    except TypeError:  # Python < 3.11.4 - no filter kwarg
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(dest)


def run_cleanroom(
    room: Path, prefix: str, tar_path: Path, kit_tar_path: Path
) -> str:
    """Unpack bundle + kit overlays, certify with the kit's own script.

    Returns the pass/skip summary parsed from the transcript.
    """
    _unpack_tar(tar_path, room)
    _unpack_tar(kit_tar_path, room)
    bundle_dir = room / prefix

    for name in ("pyproject.toml", README_FIRST_NAME, KIT_WHOLE_DIR):
        if not (bundle_dir / name).exists():
            sys.exit(f"FATAL: clean room incomplete - {name} missing after unpack.")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(bundle_dir / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # 1. Resolution preflight: the bundle's OWN code must be importable
    #    AS dpl, else the clean room would certify the wrong tree.
    pre = subprocess.run(
        [sys.executable, "-c", PREFLIGHT_SNIPPET, str(bundle_dir / "src")],
        capture_output=True, text=True, env=env, cwd=bundle_dir, timeout=120,
    )
    if pre.returncode != 0:
        sys.exit("FATAL: clean-room preflight failed:\n" + (pre.stdout + pre.stderr))
    print(f"  clean   : preflight {pre.stdout.strip().splitlines()[-1]}")

    # 2. The overlay's own green script. 600s slack: the sops-free tree
    #    cannot hang on a binary, but Gatekeeper-class noise is free.
    proc = subprocess.run(
        [str(bundle_dir / "run_offline_tests.sh")],
        capture_output=True, text=True, env=env,
        cwd=bundle_dir, timeout=600,
    )
    transcript = proc.stdout + proc.stderr

    if proc.returncode != 0:
        sys.exit(
            "FATAL: clean-room certification finished RED (exit "
            f"{proc.returncode}):\n--- transcript tail ---\n"
            + "\n".join(transcript.splitlines()[-40:])
        )
    # 3. Banner discipline: FERNET-ONLY GREEN is the ONLY acceptable
    #    outcome. A bundle-shipped binary would flip this to
    #    full-strength GREEN - which here means a doctrine breach.
    if "GREEN" not in transcript:
        sys.exit("FATAL: clean room passed but printed no GREEN banner:\n"
                 + "\n".join(transcript.splitlines()[-20:]))
    if "FERNET-ONLY" not in transcript:
        sys.exit(
            "FATAL: clean room was FULL-STRENGTH green, not FERNET-ONLY. "
            "Binaries may have leaked into the bundle - DO NOT ship. "
            "Investigate bin/ staging immediately."
        )
    m = re.search(r"(\d+) passed(?:, (\d+) skipped)?", transcript)
    summary = m.group(0) if m else "(counts unparsed)"
    return summary


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def dry_run(project_root: Path, verbose: bool) -> None:
    name, version = read_project_meta(project_root)

    def section(title: str, classifier) -> None:
        rows = walk_classified(project_root, classifier)
        shipped = [r for r in rows if r[1]]
        denied = [r for r in rows if not r[1]]
        print(f"--- {title} ---")
        print(f"WOULD SHIP - {len(shipped)} files")
        if verbose:
            for rel, _inc, _reason in shipped:
                print(f"      {rel.as_posix()}")
        else:
            # Group by reason: one line per rule, with counts.
            groups: dict[str, int] = {}
            for _rel, _inc, reason in shipped:
                groups[reason] = groups.get(reason, 0) + 1
            for reason in sorted(groups):
                print(f"  [{reason}] x{groups[reason]}")
        print(f"WOULD EXCLUDE - {len(denied)} files")
        if verbose:
            for rel, _inc, reason in denied:
                print(f"      {rel.as_posix()}  ({reason})")
        print()

    print("dpl release bundle - DRY RUN (nothing is written)")
    print(f"  project : {name}")
    print(f"  version : {version}  (stamped from pyproject.toml)")
    print(f"  source  : {project_root}")
    print()
    section("PRODUCTION BUNDLE", classify)
    section("TEST KIT", classify_kit)
    print("Fabricated into the bundle at build time:")
    print(f"  {README_FIRST_NAME} (receiver instructions, version-stamped)")
    for d in RUNTIME_SCAFFOLD_DIRS:
        print(f"  {d}/{GITKEEP_NAME}")


def stage_mode(project_root: Path, verbose: bool) -> int:
    name, version = read_project_meta(project_root)
    staging_root = Path(tempfile.mkdtemp(prefix=f"dpl-release-{version}-stage-"))
    kit_root = Path(tempfile.mkdtemp(prefix=f"dpl-{version}-kit-stage-"))
    shipped = build_staging(project_root, staging_root, version)
    _verify_staged(
        staging_root, classify,
        allowed_fabricated={README_FIRST_NAME: "receiver instructions"},
        scaffold_dirs=RUNTIME_SCAFFOLD_DIRS,
    )
    kit_shipped = build_kit_staging(project_root, kit_root)
    kit_count = _verify_staged(kit_root, classify_kit)

    payload = sum((staging_root / rel).stat().st_size for rel in shipped)
    print("dpl release bundle - STAGED (no archives yet)")
    print(f"  project : {name}")
    print(f"  version : {version}  (stamped from pyproject.toml)")
    print(f"  bundle  : {staging_root}")
    print(f"    {len(shipped)} shipped files + {README_FIRST_NAME} "
          f"+ {len(RUNTIME_SCAFFOLD_DIRS)} scaffold dirs w/ .gitkeep"
          f"  ({payload:,} bytes shipped)")
    print(f"  kit     : {kit_root}")
    print(f"    {kit_count} staged files (tests + green scripts)")
    print(f"  verify  : both staged trees re-checked against doctrine - OK")
    if verbose:
        print()
        print("Bundle files:")
        for rel in shipped:
            print(f"      {rel.as_posix()}")
        print()
        print("Kit files:")
        for rel in kit_shipped:
            print(f"      {rel.as_posix()}")
    print()
    print("(--build stages, verifies, archives, clean-room certifies, cleans up.)")
    return 0


# ---------------------------------------------------------------------------
# Build mode (Phases 3+4)
# ---------------------------------------------------------------------------

def build_mode(project_root: Path, verbose: bool) -> int:
    name, version = read_project_meta(project_root)
    prefix = f"{name}-{version}"
    kit_prefix = prefix  # SAME prefix: kit overlays the unpacked bundle.

    staging_root = Path(tempfile.mkdtemp(prefix=f"dpl-release-{version}-stage-"))
    kit_root = Path(tempfile.mkdtemp(prefix=f"dpl-{version}-kit-stage-"))
    room = Path(tempfile.mkdtemp(prefix=f"dpl-{version}-cleanroom-"))
    try:
        # ---- stage & verify both trees -------------------------------
        shipped = build_staging(project_root, staging_root, version)
        _verify_staged(
            staging_root, classify,
            allowed_fabricated={README_FIRST_NAME: "receiver instructions"},
            scaffold_dirs=RUNTIME_SCAFFOLD_DIRS,
        )
        build_kit_staging(project_root, kit_root)
        _verify_staged(kit_root, classify_kit)

        bundle_snap = snapshot_staged_files(staging_root, prefix)
        kit_snap = snapshot_staged_files(kit_root, kit_prefix)

        # ---- archives --------------------------------------------------
        dist_dir = project_root / "dist"
        dist_dir.mkdir(exist_ok=True)
        targets = {
            "tar": dist_dir / f"{prefix}.tar.gz",
            "zip": dist_dir / f"{prefix}.zip",
            "kit_tar": dist_dir / f"{prefix}-testkit.tar.gz",
            "kit_zip": dist_dir / f"{prefix}-testkit.zip",
        }
        for t in targets.values():
            if t.exists():
                t.unlink()

        build_tar_gz(staging_root, targets["tar"], prefix)
        build_zip(staging_root, targets["zip"], prefix)
        build_tar_gz(kit_root, targets["kit_tar"], kit_prefix)
        build_zip(kit_root, targets["kit_zip"], kit_prefix)

        # The BUNDLE gets the scaffold-dir check; the KIT opts out
        # (it contains no scaffold dirs - demanding them there was a
        # false FATAL, 2026-11-06).
        verify_tar_gz(targets["tar"], bundle_snap, prefix)
        verify_zip(targets["zip"], bundle_snap, prefix)
        verify_tar_gz(targets["kit_tar"], kit_snap, kit_prefix,
                      scaffold_check=False)
        verify_zip(targets["kit_zip"], kit_snap, kit_prefix,
                   scaffold_check=False)

        # ---- clean room: unpack, overlay, certify ----------------------
        counts = run_cleanroom(room, prefix, targets["tar"], targets["kit_tar"])

        # ---- SHA256SUMS over all four artifacts -----------------------
        sums_path = dist_dir / "SHA256SUMS"
        lines = [
            f"{sha256_of(p)}  {p.name}" for p in targets.values()
        ]
        sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(kit_root, ignore_errors=True)
        shutil.rmtree(room, ignore_errors=True)

    print("dpl release bundle - BUILD COMPLETE & SELF-CERTIFIED")
    print(f"  project : {name}")
    print(f"  version : {version}  (stamped from pyproject.toml)")
    print(f"  bundle  : {len(shipped)} shipped + {README_FIRST_NAME} + "
          f"{len(RUNTIME_SCAFFOLD_DIRS)} scaffold dirs")
    print(f"  kit     : {len(kit_snap)} files (tests + green scripts)")
    print(f"  verify  : 4 archives reopened; every member hash-matched")
    print(f"  clean   : bundle unpacked + kit overlaid in a throwaway dir;")
    print(f"            kit's own green script run against the unpacked")
    print(f"            code - GREEN (FERNET-ONLY), {counts}")
    print()
    print("Artifacts in dist/:")
    for p in targets.values():
        print(f"  {p.name:<{len(prefix) + 13}} {p.stat().st_size:>10,} bytes  "
              f"sha256 {sha256_of(p)[:16]}...")
    print(f"  SHA256SUMS ({sums_path.stat().st_size} bytes)")
    print()
    print("Receiver-side check:  cd dist && shasum -a 256 -c SHA256SUMS")
    if verbose:
        print()
        print(sums_path.read_text(encoding="utf-8"), end="")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_release.py",
        description=(
            "Build the dpl release bundle + companion test kit, then "
            "clean-room self-certify. Phase 4: complete."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="inventory both artifacts, then exit")
    parser.add_argument("--stage", action="store_true",
                        help="stage and verify both trees, leave them for inspection")
    parser.add_argument("--build", action="store_true",
                        help="full build + clean-room self-certification")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="list every file")
    args = parser.parse_args(argv)

    validate_source_tree(PROJECT_ROOT)

    if args.dry_run:
        dry_run(PROJECT_ROOT, args.verbose)
        return 0
    if args.stage:
        return stage_mode(PROJECT_ROOT, args.verbose)
    if args.build:
        return build_mode(PROJECT_ROOT, args.verbose)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
