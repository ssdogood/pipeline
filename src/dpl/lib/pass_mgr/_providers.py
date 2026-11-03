"""
pass_mgr providers — SOPS (primary) and Fernet (backup).

Each provider owns exactly one whole-file vault per environment and returns
the complete name->secret mapping from load(). The dispatcher (in
__init__.py) invokes load() at most once per process and caches the result.

Provider responsibility split (per design doc §5):
    - the config loader validated cross-provider references;
    - each provider validates its OWN section deeply, here, at construction
      time, so a missing 'binary:' key fails at startup, not at fetch time.

Failure taxonomy mapping (every failure names the file/binary/version):

    binary absent / not executable ......... PassMgrBinaryMissingError
    binary version != manifest pin .......... PassMgrVersionMismatchError
    key / identity file absent ............. PassMgrKeyFileMissingError
    decryption refused / bad ciphertext ..... PassMgrDecryptError
    subprocess timeout ..................... PassMgrDecryptError
    malformed vault content ................. PassMgrDecryptError
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, ClassVar

import yaml
from cryptography.fernet import Fernet, InvalidToken

from ._config import PassMgrConfig, resolve_relative_path
from ._errors import (
    PassMgrBinaryMissingError,
    PassMgrConfigError,
    PassMgrDecryptError,
    PassMgrKeyFileMissingError,
    PassMgrVersionMismatchError,
)

#: Hard wall-clock limit on any single sops invocation. A hung child under
#: cron must fail loudly, never park the extract forever (design §5.1).
SOPS_TIMEOUT_SECONDS = 10

#: Envelope schema version understood by the Fernet provider. Reserved for
#: v2 key rotation — see design §5.2. Nothing branches on it yet.
FERNET_ENVELOPE_VERSION = 1


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def flatten_namespaced(mapping: Any) -> dict[str, str]:
    """Flatten a namespaced vault mapping into dotted names.

    {'oracle': {'stg_pass': 'x'}} -> {'oracle.stg_pass': 'x'}
    Flat leaf entries pass through unchanged. Raises PassMgrDecryptError
    on non-string leaves — vaults must contain only string secret values.
    """
    flat: dict[str, str] = {}

    def _walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            if not node:
                raise PassMgrDecryptError(
                    f"pass_mgr: empty namespace '{prefix or '<root>'}' in "
                    f"vault — nothing to retrieve."
                )
            for key, value in node.items():
                _walk(value, f"{prefix}.{key}" if prefix else str(key))
        else:
            if not isinstance(node, str):
                raise PassMgrDecryptError(
                    f"pass_mgr: vault entry '{prefix}' must be a string, "
                    f"got {type(node).__name__}."
                )
            flat[prefix] = node

    if not isinstance(mapping, dict) or not mapping:
        raise PassMgrDecryptError(
            "pass_mgr: decrypted vault is empty or not a mapping."
        )
    _walk(mapping, "")
    return flat


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------

class Provider:
    """Base class for vault providers. Subclasses set SECTION_NAME."""

    #: Config key under `methods:` that activates this provider.
    SECTION_NAME: ClassVar[str]

    def __init__(self, config_section: dict[str, Any], config: PassMgrConfig):
        if not isinstance(config_section, dict):
            raise PassMgrConfigError(
                f"pass_mgr: method '{self.SECTION_NAME}' must map to a "
                f"mapping, got {type(config_section).__name__}."
            )
        self.config = config
        self.section = config_section

    def load(self) -> dict[str, str]:
        """Decrypt and return the full name->secret mapping."""
        raise NotImplementedError


def _require_key(section: dict[str, Any], key: str, method: str) -> Any:
    """Fetch a required key from a method section or fail naming both."""
    if key not in section:
        raise PassMgrConfigError(
            f"pass_mgr: method '{method}' is missing required key "
            f"'{key}'. Check the methods.{method} section of "
            f"pass_mgr.{self_env_suffix()}."
        )
    return section[key]


def self_env_suffix() -> str:
    """Best-effort config-file hint for error messages ('laptop.yaml')."""
    return "<env>.yaml"


# ---------------------------------------------------------------------------
# SOPS provider (primary)
# ---------------------------------------------------------------------------

class SopsProvider(Provider):
    """Whole-vault decryption via the pinned sops binary + age identity.

    Key backend is age, never GPG: an identity file is a single
    permission-controlled file, non-interactive under cron (design §5.1).
    """

    SECTION_NAME = "sops"

    def __init__(self, config_section: dict[str, Any], config: PassMgrConfig):
        super().__init__(config_section, config)

        self.binary = resolve_relative_path(
            _require_key(config_section, "binary", self.SECTION_NAME),
            f"{self.SECTION_NAME}.binary",
            config.base_dir,
        )
        self.age_key_file = resolve_relative_path(
            _require_key(config_section, "age_key_file", self.SECTION_NAME),
            f"{self.SECTION_NAME}.age_key_file",
            config.base_dir,
        )
        self.encrypted_file = resolve_relative_path(
            _require_key(config_section, "encrypted_file", self.SECTION_NAME),
            f"{self.SECTION_NAME}.encrypted_file",
            config.base_dir,
        )

        # manifest is optional in v1: version pinning degrades gracefully
        # when absent (checksums are still enforced out-of-band by the
        # green script), but present-and-malformed is a hard error.
        manifest_raw = config_section.get("manifest")
        self.manifest: Any | None = None
        if manifest_raw is not None:
            self.manifest = resolve_relative_path(
                manifest_raw, f"{self.SECTION_NAME}.manifest", config.base_dir
            )

        self._pinned_version: str | None = None

    # -- startup probes ---------------------------------------------------

    def _probe_binary(self) -> None:
        if not self.binary.exists():
            raise PassMgrBinaryMissingError(
                f"pass_mgr: sops binary not found at {self.binary}. Copy "
                f"the pinned binary from bin/ per sops_manifest.txt (exact "
                f"release filename, no renames)."
            )

    def _pinned_version_from_manifest(self) -> str | None:
        """Extract '# SOPS version: vX.Y.Z (pinned)' from the manifest."""
        if self._pinned_version is not None:
            return self._pinned_version
        if self.manifest is None:
            return None
        try:
            text = self.manifest.read_text(encoding="utf-8")
        except OSError as exc:
            raise PassMgrConfigError(
                f"pass_mgr: cannot read sops manifest {self.manifest}: {exc}"
            ) from exc
        match = re.search(r"#\s*SOPS version:\s*(\S+)", text)
        if not match:
            raise PassMgrConfigError(
                f"pass_mgr: manifest {self.manifest} has no "
                f"'# SOPS version:' pin comment."
            )
        self._pinned_version = match.group(1)
        return self._pinned_version

    def _probe_version(self) -> None:
        """Compare `sops --version` output against the manifest pin."""
        pinned = self._pinned_version_from_manifest()
        if pinned is None:
            return
        result = self._run_sops(["--version"])
        version_out = (result.stdout or result.stderr or "").strip()
        # sops --version prints e.g. 'sops 3.13.3 (abc123)'; tolerate both
        # 'v'-prefixed and bare version tokens.
        normalized_pinned = pinned.removeprefix("v")
        found = None
        m = re.search(r"(\d+\.\d+\.\d+)", version_out)
        if m:
            found = m.group(1)
        if found != normalized_pinned:
            raise PassMgrVersionMismatchError(
                f"pass_mgr: sops binary at {self.binary} reports version "
                f"'{found or version_out or '<no output>'}', but the pinned "
                f"version is '{normalized_pinned}'. Replace the binary "
                f"per sops_manifest.txt."
            )

    def _probe_key_file(self) -> None:
        if not self.age_key_file.exists():
            raise PassMgrKeyFileMissingError(
                f"pass_mgr: age identity file not found at "
                f"{self.age_key_file}. Restore secrets/age.key for this "
                f"environment."
            )

    def _probe_encrypted_file(self) -> None:
        if not self.encrypted_file.exists():
            raise PassMgrConfigError(
                f"pass_mgr: sops vault not found at {self.encrypted_file}. "
                f"Create it with `sops --encrypt --age <recipient>`."
            )

    # -- subprocess ---------------------------------------------------------

    def _run_sops(self, args: list[str]) -> subprocess.CompletedProcess:
        """Invoke the sops binary with a hard timeout."""
        try:
            return subprocess.run(
                [str(self.binary)] + args,
                capture_output=True,
                text=True,
                timeout=SOPS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: sops ({self.binary}) timed out after "
                f"{SOPS_TIMEOUT_SECONDS}s on: {' '.join(args)}"
            ) from exc
        except FileNotFoundError as exc:
            raise PassMgrBinaryMissingError(
                f"pass_mgr: sops binary disappeared between probe and "
                f"invocation: {self.binary}"
            ) from exc
        except PermissionError as exc:
            raise PassMgrBinaryMissingError(
                f"pass_mgr: sops binary not executable: {self.binary} "
                f"(chmod +x)."
            ) from exc

    # -- Provider.load -------------------------------------------------------

    def load(self) -> dict[str, str]:
        self._probe_binary()
        self._probe_key_file()
        self._probe_encrypted_file()
        self._probe_version()

        import os

        child_env = dict(os.environ)
        child_env["SOPS_AGE_KEY_FILE"] = str(self.age_key_file)

        try:
            result = subprocess.run(
                [str(self.binary), "--decrypt", str(self.encrypted_file)],
                capture_output=True,
                text=True,
                timeout=SOPS_TIMEOUT_SECONDS,
                check=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: sops decrypt timed out after "
                f"{SOPS_TIMEOUT_SECONDS}s on {self.encrypted_file}."
            ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise PassMgrDecryptError(
                f"pass_mgr: sops failed to decrypt "
                f"{self.encrypted_file} (exit {result.returncode}): "
                f"{stderr[:500] or 'no stderr captured'}. Common causes: "
                f"wrong age identity, tampered vault, or format drift."
            )

        try:
            parsed = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: sops decrypted OK but output is not valid YAML "
                f"({exc})."
            ) from exc

        return flatten_namespaced(parsed)


# ---------------------------------------------------------------------------
# Fernet provider (backup)
# ---------------------------------------------------------------------------

class FernetProvider(Provider):
    """Whole-envelope decryption of the JSON backup vault.

    Same interface, same cache slot, same error taxonomy as SOPS — a thin
    adapter by design (§5.2), kept viable as the certified fallback if
    the security team rejects SOPS.
    """

    SECTION_NAME = "fernet"

    def __init__(self, config_section: dict[str, Any], config: PassMgrConfig):
        super().__init__(config_section, config)

        self.key_file = resolve_relative_path(
            _require_key(config_section, "key_file", self.SECTION_NAME),
            f"{self.SECTION_NAME}.key_file",
            config.base_dir,
        )
        self.encrypted_file = resolve_relative_path(
            _require_key(config_section, "encrypted_file", self.SECTION_NAME),
            f"{self.SECTION_NAME}.encrypted_file",
            config.base_dir,
        )

    def load(self) -> dict[str, str]:
        if not self.key_file.exists():
            raise PassMgrKeyFileMissingError(
                f"pass_mgr: fernet key file not found at {self.key_file}. "
                f"Restore secrets/fernet.key for this environment."
            )
        if not self.encrypted_file.exists():
            raise PassMgrConfigError(
                f"pass_mgr: fernet envelope not found at "
                f"{self.encrypted_file}."
            )

        key_text = self.key_file.read_text(encoding="utf-8").strip()
        blob = self.encrypted_file.read_bytes()

        try:
            token = blob.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: fernet envelope {self.encrypted_file} is not "
                f"a valid fernet token."
            ) from exc

        try:
            plaintext = Fernet(key_text).decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: fernet decryption failed for "
                f"{self.encrypted_file} — wrong key or corrupted envelope."
            ) from exc
        except ValueError as exc:  # malformed key
            raise PassMgrConfigError(
                f"pass_mgr: fernet key file {self.key_file} does not "
                f"contain a valid key: {exc}"
            ) from exc

        try:
            envelope = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise PassMgrDecryptError(
                f"pass_mgr: fernet envelope payload is not valid JSON "
                f"({exc})."
            ) from exc

        if not isinstance(envelope, dict):
            raise PassMgrDecryptError(
                "pass_mgr: fernet envelope must be a JSON object."
            )

        version = envelope.get("version")
        if version != FERNET_ENVELOPE_VERSION:
            raise PassMgrDecryptError(
                f"pass_mgr: fernet envelope version {version!r} "
                f"unsupported (expected {FERNET_ENVELOPE_VERSION}). "
                f"This envelope may be from another rotation era."
            )

        entries = envelope.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise PassMgrDecryptError(
                "pass_mgr: fernet envelope has no 'entries' mapping."
            )
        for name, value in entries.items():
            if not isinstance(value, str):
                raise PassMgrDecryptError(
                    f"pass_mgr: fernet entry '{name}' must be a string, "
                    f"got {type(value).__name__}."
                )

        return dict(entries)


# ---------------------------------------------------------------------------
# Registry (mirrors the wrapper's SOURCE_TYPES pattern)
# ---------------------------------------------------------------------------

#: method id -> Provider class. The dispatcher instantiates lazily, one
#: instance per referenced method, and caches each vault per process.
PROVIDER_TYPES: dict[str, type[Provider]] = {
    SopsProvider.SECTION_NAME: SopsProvider,
    FernetProvider.SECTION_NAME: FernetProvider,
}
