"""
pass_mgr configuration - loading and validation of the ACTIVE
environment's vault config.

PT-040 4a: the environment word is retired from the vault's API and
filenames. The active environment folder (resolved once by
dpl.lib.environment at startup) supplies the path:
<env-folder>/pass_mgr.yaml. The file carries no environment in its
name - its environment comes from WHERE it sits, never its name.

Exactly one pass_mgr config exists per environment, and exactly one
environment is active per machine (the resolver guarantees this), so
the config path is always unambiguous. Production policy is tier-
driven: the caller passes is_production=True only when the resolved
tier is 'production'.

Validation philosophy (mirrors the rest of the pipeline): fail loudly at
load time, with error messages that name the offending file, key, and
value so a misconfiguration is diagnosed in seconds, not minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ._errors import (
    PassMgrConfigError,
    PassMgrProductionPolicyError,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Filename the vault config must have inside the active env folder.
PASS_MGR_FILENAME = "pass_mgr.yaml"

#: Config schema version understood by this loader. Anything else is a
#: hard error - see design doc §3, reserved for v2 key rotation.
SUPPORTED_CONFIG_VERSION = 1

#: Top-level keys allowed in a pass_mgr config file. Exhaustive on purpose:
#: unknown keys are rejected so typos cannot silently become dead config.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "default_method", "methods", "secrets"})

#: Keys allowed inside a single `secrets` entry's mapping.
ALLOWED_SECRET_KEYS = frozenset({"method"})

#: Method ids that are categorically insecure and may never be referenced
#: from the production config. Detected at load time, not at call time.
INSECURE_METHODS = frozenset({"plain"})

#: Labels whose config values are key-material paths and may therefore
#: live OUTSIDE the project tree, expressed as ~/... paths. Membership
#: is checked explicitly - do not infer from name suffixes (see the
#: fernet.key_file incident: `.key_file` failed an `_key_file` suffix
#: check and the tilde branch silently never fired).
KEY_PATH_LABELS = frozenset({"sops.age_key_file", "fernet.key_file"})

# --------------------------------------------------------------------------
# Validated config object
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PassMgrConfig:
    """Immutable, validated view of the active environment's vault config."""

    #: Active environment folder (e.g. config/development). Carries no
    #: environment WORD - it is the resolved folder Path from
    #: dpl.lib.environment; error messages may display it.
    env_folder: Path
    is_production: bool
    default_method: str
    #: Raw per-method config sections keyed by method id, exactly as they
    #: appear in the file. Providers receive their own section and perform
    #: their own deep validation (design doc §5) - this loader only
    #: enforces cross-provider rules.
    method_sections: dict[str, dict[str, Any]]
    #: Secret name -> resolved method id, after applying default_method
    #: and per-secret overrides.
    secret_methods: dict[str, str]
    #: Project root; all relative paths in method sections resolve
    #: against this when providers consume them.
    base_dir: Path

    def method_for(self, secret_name: str) -> str:
        """Return the method id responsible for `secret_name`."""
        try:
            return self.secret_methods[secret_name]
        except KeyError:
            raise PassMgrConfigError(
                f"pass_mgr: secret '{secret_name}' is not defined in "
                f"{self.env_folder / PASS_MGR_FILENAME}. Check the "
                f"`secrets` mapping."
            ) from None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_config(env_folder: str | Path, project_root: Path | None = None,
                *, is_production: bool | None = None) -> PassMgrConfig:
    """Load and validate the ACTIVE environment's vault config.

    PT-040 4a: the environment word is gone from the API. Callers pass
    the resolved environment FOLDER; the config file is
    <env_folder>/pass_mgr.yaml - its environment comes from where it
    sits, never its name.

    Args:
        env_folder: the active environment folder (typically
            ResolvedEnvironment.folder from dpl.lib.environment).
        project_root: root of the pipeline tree. Defaults to the parent of
            this package's parent (i.e. src/dpl/ -> project root). Override
            in tests.
        is_production: production policy switch. When None (default),
            derived from the folder name - TRUE only when the folder is
            literally named 'production' (the resolver's fixed vocabulary
            guarantees the shape; this derivation is defensive only and
            would require breaking the resolver's validation to lie).

    Returns:
        A fully validated PassMgrConfig.

    Raises:
        PassMgrConfigError: file missing, YAML syntax error, or any
            structural/validation violation.
        PassMgrProductionPolicyError: an insecure method is referenced
            anywhere in the production config.
    """
    root = _resolve_project_root(project_root)
    folder = Path(env_folder)
    config_path = folder / PASS_MGR_FILENAME

    if is_production is None:
        is_production = folder.name == "production"

    if not config_path.is_file():
        raise PassMgrConfigError(
            f"pass_mgr: config file not found: {config_path}. The active "
            f"environment folder must contain pass_mgr.yaml - copy "
            f"config/_templates/ and fill in the placeholders, or fix "
            f"config/environment.yaml."
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PassMgrConfigError(
            f"pass_mgr: YAML syntax error in {config_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise PassMgrConfigError(
            f"pass_mgr: config file {config_path} must contain a YAML "
            f"mapping at top level, got {type(raw).__name__}."
        )

    return _validate(raw, config_path, root, is_production=is_production)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _resolve_project_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return Path(project_root).resolve()
    # This file lives at <root>/src/dpl/lib/pass_mgr/_config.py.
    return Path(__file__).resolve().parents[4]


def _validate(
    raw: dict[str, Any],
    config_path: Path,
    root: Path,
    *,
    is_production: bool = False,
) -> PassMgrConfig:
    # 4a: production policy is tier-driven - the caller derives it from
    # the resolved environment (vocabulary-checked upstream), never from
    # a filename word.

    # --- top-level shape -------------------------------------------------
    unknown_keys = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise PassMgrConfigError(
            f"pass_mgr: unknown top-level key(s) in {config_path.name}: "
            f"{sorted(unknown_keys)}. Allowed keys: "
            f"{sorted(ALLOWED_TOP_LEVEL_KEYS)}. Typo protection - fix or "
            f"remove the offending key."
        )

    missing_keys = ALLOWED_TOP_LEVEL_KEYS - set(raw)
    if missing_keys:
        raise PassMgrConfigError(
            f"pass_mgr: missing required top-level key(s) in "
            f"{config_path.name}: {sorted(missing_keys)}."
        )

    # --- version ----------------------------------------------------------
    version = raw["version"]
    if version != SUPPORTED_CONFIG_VERSION:
        raise PassMgrConfigError(
            f"pass_mgr: unsupported config schema version {version!r} in "
            f"{config_path.name}; this loader supports "
            f"{SUPPORTED_CONFIG_VERSION}."
        )

    # --- methods -----------------------------------------------------------
    methods = raw["methods"]
    if not isinstance(methods, dict) or not methods:
        raise PassMgrConfigError(
            f"pass_mgr: `methods` in {config_path.name} must be a non-empty "
            f"mapping of method id -> config section."
        )
    for method_id, section in methods.items():
        if not isinstance(section, dict):
            raise PassMgrConfigError(
                f"pass_mgr: method '{method_id}' in {config_path.name} must "
                f"map to a mapping, got {type(section).__name__}."
            )

    # --- default_method ----------------------------------------------------
    default_method = raw["default_method"]
    if not isinstance(default_method, str) or default_method not in methods:
        raise PassMgrConfigError(
            f"pass_mgr: default_method '{default_method}' in "
            f"{config_path.name} is not a defined method. Defined: "
            f"{sorted(methods)}."
        )

    # --- secrets -------------------------------------------------------------
    secrets = raw["secrets"]
    if not isinstance(secrets, dict) or not secrets:
        raise PassMgrConfigError(
            f"pass_mgr: `secrets` in {config_path.name} must be a non-empty "
            f"mapping of secret name -> entry."
        )

    secret_methods: dict[str, str] = {}
    for name, entry in secrets.items():
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise PassMgrConfigError(
                f"pass_mgr: secret '{name}' in {config_path.name} must map "
                f"to a mapping ({{}} or {{method: <id>}}), got "
                f"{type(entry).__name__}."
            )
        bad_keys = set(entry) - ALLOWED_SECRET_KEYS
        if bad_keys:
            raise PassMgrConfigError(
                f"pass_mgr: secret '{name}' in {config_path.name} has "
                f"unknown key(s) {sorted(bad_keys)}; allowed: "
                f"{sorted(ALLOWED_SECRET_KEYS)}."
            )
        method_id = entry.get("method", default_method)
        if method_id not in methods:
            raise PassMgrConfigError(
                f"pass_mgr: secret '{name}' in {config_path.name} "
                f"references undefined method '{method_id}'. Defined "
                f"methods: {sorted(methods)}."
            )
        secret_methods[name] = method_id

    # --- production policy ---------------------------------------------------
    referenced = set(secret_methods.values()) | {default_method}
    insecure_referenced = referenced & INSECURE_METHODS
    if is_production and insecure_referenced:
        raise PassMgrProductionPolicyError(
            f"pass_mgr: insecure method(s) {sorted(insecure_referenced)} "
            f"referenced in the PRODUCTION config {config_path.name}. "
            f"This is refused at load time by design - production secrets "
            f"must use sops (or fernet as fallback)."
        )

    return PassMgrConfig(
        env_folder=config_path.parent,
        is_production=is_production,
        default_method=default_method,
        method_sections=dict(methods),
        secret_methods=secret_methods,
        base_dir=root,
    )


# --------------------------------------------------------------------------
# Path hygiene (used by providers via config.base_dir)
# --------------------------------------------------------------------------

def _expand_tilde_for_keys(raw_value: str, what: str) -> str:
    """Expand leading ~ to the user's home directory.
    
    Called ONLY for key-file paths (age_key_file, key_file).
    Returns the input unchanged if it doesn't start with ~.
    """
    if not isinstance(raw_value, str):
        raise PassMgrConfigError(
            f"pass_mgr: key path '{what}' must be a string, got {raw_value!r}."
        )
    if raw_value.startswith("~/"):
        from pathlib import Path
        expanded = str(Path.home() / raw_value[2:])
        return expanded
    return raw_value

def resolve_relative_path(raw_value: Any, what: str, base_dir: Path) -> Path:
    """Resolve a config-supplied path.

    Key-file paths (age_key_file, key_file) may use ~/... - expanded to
    the user's home, but the result must stay INSIDE home. Everything
    else (binaries, vaults, manifest) must be relative and stay inside
    the project tree, as before.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise PassMgrConfigError(
            f"pass_mgr: path for '{what}' must be a non-empty string, got "
            f"{raw_value!r}."
        )
    if raw_value.startswith("~"):
        if what not in KEY_PATH_LABELS:
            raise PassMgrConfigError(
                f"pass_mgr: '~' paths are only permitted for key files, "
                f"not for '{what}'."
            )
        expanded = Path(raw_value).expanduser().resolve()
        home = Path.home().resolve()
        if expanded != home and home not in expanded.parents:
            raise PassMgrConfigError(
                f"pass_mgr: key path for '{what}' escapes the home "
                f"directory ('{raw_value}' -> '{expanded}'). Not permitted."
            )
        return expanded

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise PassMgrConfigError(
            f"pass_mgr: path for '{what}' must be relative to the project "
            f"root (got absolute path '{raw_value}')."
        )
    resolved = (base_dir / candidate).resolve()
    root = base_dir.resolve()
    if not (resolved == root or root in resolved.parents):
        raise PassMgrConfigError(
            f"pass_mgr: path for '{what}' resolves outside the project "
            f"root ('{raw_value}' -> '{resolved}'). Not permitted."
        )
    return resolved