"""
Shared pytest fixtures for the pipeline test suite.

Conventions enforced here (industry standard, and the basis of the
Phase 0 "offline, hermetic" guarantee):

  * Every test builds its own world inside pytest's tmp_path - throwaway
    keys, throwaway vaults. Nothing ever touches the developer's real
    secrets/ directory or the real bin/ contents in place.
  * Real binaries are used (copied into the temp tree), never mocked
    crypto - the tests must prove exactly what ships.
  * The pass_mgr module-level cache is reset around EVERY test (autouse
    fixture) so tests are independent and order-insensitive.

IMPORT WIRING (read this before touching sys.path lines):
  Requires two empty marker files, which stop the test folder named
  'pass_mgr' from shadowing the real module:
      test/__init__.py
      test/pass_mgr/__init__.py
  With those present, pytest imports test modules as 'test.pass_mgr.*'
  and never puts test/ itself on the search path.

TWO PROJECT FIXTURES (choose deliberately):
  * `project`     - temp tree WITH the pinned binaries copied in. Use for
                    SOPS/provider tests that exercise the real binary.
                    Fails loudly if bin/ is empty (by design: a machine
                    without verified binaries must not pass as green).
  * `project_nobin` - temp tree WITHOUT binaries. Use for tests that
                    need no external tools (config, Fernet) so those
                    suites stay green on any machine, at any stage of
                    bootstrap. Binary-using methods on this instance
                    (add_age_identity, add_sops_vault) raise if called.
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# --- make the real pass_mgr importable -------------------------------------
# The module lives at src/dpl/lib/pass_mgr/. For the module name to be
# 'pass_mgr', the directory DIRECTLY CONTAINING it must be on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = PROJECT_ROOT / "src" / "dpl" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import pass_mgr  # noqa: E402
from pass_mgr import reset as _reset_pass_mgr  # noqa: E402

# Binaries live in the real bin/, with their exact upstream release names.
BIN_DIR = PROJECT_ROOT / "bin"

#: Substring used to find each tool in bin/ by its release-name prefix.
BINARY_HINTS = {
    "sops": "sops-",
    "age-keygen": "age-keygen-",
}


def find_binary(tool: str, bin_dir: Path = BIN_DIR) -> Path:
    """Locate this platform's pinned tool in bin/.

    Selection is PLATFORM-AWARE: a bare prefix match is unsafe on trees
    that carry several platforms' binaries - on Windows it picked the
    darwin/ELF build and every subprocess died with WinError 193
    (2026-11-05 certification finding). Patterns mirror
    run_offline_tests.sh's platform detection.
    """
    pattern = _platform_binary_pattern(tool)
    if bin_dir.is_dir():
        platform_matches = sorted(
            p for p in bin_dir.iterdir()
            if fnmatch.fnmatch(p.name, pattern)
        )
    else:
        platform_matches = []

    if not platform_matches:
        available = (
            sorted(p.name for p in bin_dir.iterdir())
            if bin_dir.is_dir()
            else ["(bin/ directory missing)"]
        )
        raise FileNotFoundError(
            f"{tool} not found for this platform in {bin_dir} "
            f"(looked for pattern '{pattern}'). Files present: "
            f"{available}. Copy the pinned binary for YOUR platform, "
            f"keeping its exact filename from sops_manifest.txt, then "
            f"re-run the test suite."
        )
    return platform_matches[0]


# ---------------------------------------------------------------------------
# Autouse hygiene: cold pass_mgr for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _cold_pass_mgr():
    """Reset the module-level dispatcher before and after every test.

    Without this, one test's init() would leak its cached vault into the
    next test - order-dependence, the classic test-suite disease.
    """
    _reset_pass_mgr()
    yield
    _reset_pass_mgr()


# ---------------------------------------------------------------------------
# Temp project tree builder
# ---------------------------------------------------------------------------

class TempProject:
    """A disposable pipeline tree with the pass_mgr layout, for one test.

    Provides write_config(), add_sops_vault(), and add_fernet_envelope();
    everything lands under pytest's tmp_path, so tests never touch real
    secrets/ or bin/ contents.

    include_binaries=False gives a binary-free tree for suites that need
    none (config, Fernet).
    """

    def __init__(
        self,
        root: Path,
        source_bin_dir: Path = BIN_DIR,
        include_binaries: bool = True,
    ):
        self.root = root
        self.source_bin_dir = source_bin_dir
        self.include_binaries = include_binaries
        self._bin_copied: dict[str, Path] = {}
        if include_binaries:
            self._setup_bin()
        else:
            (self.root / "bin").mkdir(parents=True, exist_ok=True)
        self.secrets_dir = root / "secrets"
        self.secrets_dir.mkdir(parents=True, exist_ok=True)

    # -- structure --------------------------------------------------------

    def _setup_bin(self) -> None:
        """Copy pinned binaries into the temp tree.

        Copying (rather than referencing) keeps path resolution entirely
        inside the temp project root, exercising resolve_relative_path
        exactly as production does.
        """
        bin_dir = self.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for tool in BINARY_HINTS:
            src = find_binary(tool, self.source_bin_dir)
            dst = bin_dir / src.name
            shutil.copy2(src, dst)  # copy2 preserves the exec bit
            self._bin_copied[tool] = dst
        # Manifest: copied if present, absent otherwise - tests can add
        # their own manifest variants to exercise version pinning.
        manifest_src = self.source_bin_dir / "sops_manifest.txt"
        if manifest_src.is_file():
            shutil.copy2(manifest_src, bin_dir / "sops_manifest.txt")

    def binary_path(self, tool: str) -> str:
        """Relative (config-style) path to the copied binary."""
        if tool not in self._bin_copied:
            raise RuntimeError(
                f"binary '{tool}' not copied into this temp tree - use the "
                f"`project` fixture (not `project_nobin`) for binary-using "
                f"tests."
            )
        return f"bin/{self._bin_copied[tool].name}"

    @property
    def manifest_path(self) -> str:
        return "bin/sops_manifest.txt"

    # -- config ----------------------------------------------------------

    def write_config(self, environment: str, config: dict) -> Path:
        path = self.root / "config" / f"pass_mgr.{environment}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    def write_config_default(
        self,
        environment: str = "laptop",
        default_method: str = "sops",
        secrets: dict | None = None,
    ) -> Path:
        """Convenience: the canonical well-formed config from the design.
        Requires binaries (references the sops binary path)."""
        secrets = secrets or {"oracle.stg_pass": {}, "ldap.service_pass": {}}
        return self.write_config(
            environment,
            {
                "version": 1,
                "default_method": default_method,
                "methods": {
                    "sops": {
                        "binary": self.binary_path("sops"),
                        "manifest": self.manifest_path,
                        "age_key_file": "secrets/age.key",
                        "encrypted_file": "secrets/secrets.sops.yaml",
                    },
                    "fernet": {
                        "key_file": "secrets/fernet.key",
                        "encrypted_file": "secrets/secrets.fernet.bin",
                    },
                },
                "secrets": secrets,
            },
        )

    # -- vaults -------------------------------------------------------------

    def add_age_identity(self) -> tuple[Path, str]:
        """Generate a throwaway age identity; returns (key_file, recipient).

        Runs the real age-keygen binary - no keys are ever committed,
        not even test keys (design §7, Phase 0).
        """
        key_file = self.secrets_dir / "age.key"
        result = subprocess.run(
            [str(self._bin_copied["age-keygen"]), "-o", str(key_file)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        
        # age-keygen writes the identity to the file and prints the public
        # key ("# public key: age1...") - on stderr per the man page,
        # though we scan both streams defensively across versions.
        recipient = ""
        output_lines = (result.stdout or "").splitlines() + (
            result.stderr or ""
        ).splitlines()
        for line in output_lines:
            lowered = line.lower()
            if "public key:" in lowered:
                recipient = lowered.split("public key:")[1].strip()
        if not recipient.startswith("age1"):
            raise RuntimeError(
                f"age-keygen output did not contain a recipient: "
                f"{result.stdout!r} {result.stderr!r}"
            )
        return key_file, recipient

    def add_sops_vault(
        self,
        entries: dict,
        identity_file: Path,
        recipient: str,
        name: str = "secrets.sops.yaml",
    ) -> Path:
        """Encrypt a namespaced vault with the real sops binary.

        entries is the PLAINTEXT mapping (nested namespaces allowed):
        {"oracle": {"stg_pass": "x"}} - mirrors production creation flow.

        ENCRYPT/DECRYPT ASYMMETRY (why both identity_file and recipient
        are parameters): encryption needs only the PUBLIC recipient key
        (--age <pub>); decryption - performed later by SopsProvider -
        needs the PRIVATE identity file via SOPS_AGE_KEY_FILE. Same pair
        of math keys, different directions.

        Environment hygiene: the child process gets this machine's
        environment MINUS SOPS_AGE_KEY_FILE, so a developer's real
        identity can never leak into a test. Remove only what can
        interfere - not everything.
        """
        plain = self.secrets_dir / f"{name}.plain.yaml"
        plain.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
        vault = self.secrets_dir / name

        child_env = {
            k: v for k, v in os.environ.items() if k != "SOPS_AGE_KEY_FILE"
        }
        proc = subprocess.run(
            [
                str(self._bin_copied["sops"]),
                "--encrypt",
                "--age",
                recipient,
                str(plain),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env=child_env,
        )
        vault.write_text(proc.stdout, encoding="utf-8")
        plain.unlink(missing_ok=True)
        return vault

    def add_fernet_envelope(
        self,
        entries: dict[str, str],
        name: str = "secrets.fernet.bin",
    ) -> Path:
        """Create the Fernet backup envelope with a fresh key.

        Envelope format per design §5.2 - the version field is present now
        solely so v2 rotation can detect old envelopes. Needs NO binaries
        (pure-Python cryptography) - safe on project_nobin.
        """
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        (self.secrets_dir / "fernet.key").write_bytes(key)
        envelope = {
            "version": 1,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "entries": entries,
        }
        token = Fernet(key).encrypt(json.dumps(envelope).encode("utf-8"))
        blob = self.secrets_dir / name
        blob.write_text(token.decode("ascii"), encoding="utf-8")
        return blob


@pytest.fixture
def project(tmp_path: Path) -> TempProject:
    """A disposable pipeline tree WITH pinned binaries copied in.

    Use for tests that exercise the real sops binary. Fails loudly when
    bin/ is empty - a machine without verified binaries must not look
    green.
    """
    return TempProject(tmp_path)


@pytest.fixture
def project_nobin(tmp_path: Path) -> TempProject:
    """A disposable pipeline tree WITHOUT binaries.

    Use for config and Fernet tests - they need no external tools, so
    they stay green on any machine at any bootstrap stage. Calling a
    binary-using method on this instance raises RuntimeError.
    """
    return TempProject(tmp_path, include_binaries=False)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_entries() -> dict:
    """The canonical fixture vault contents, namespaced like production."""
    return {
        "oracle": {"stg_pass": "0ra-stg-pw-7f3"},
        "ldap": {"service_pass": "ld4p-svc-pw-9k1"},
    }


@pytest.fixture
def flat_sample_entries(sample_entries: dict) -> dict:
    """The same secrets, flattened to dotted names (Fernet envelope form)."""
    return {
        "oracle.stg_pass": "0ra-stg-pw-7f3",
        "ldap.service_pass": "ld4p-svc-pw-9k1",
    }

def _platform_binary_pattern(stem: str) -> str:
    """Mirror run_offline_tests.sh. A bare '<stem>-*' glob is unsafe: a
    tree carrying several platforms' binaries picks the wrong one
    (WinError 193 on Windows)."""
    if sys.platform.startswith(("win32", "msys", "cygwin")) or os.environ.get("MSYSTEM"):
        return f"{stem}-*amd64.exe"
    if sys.platform == "darwin":
        return f"{stem}-*darwin*"
    return f"{stem}-*linux.amd64"