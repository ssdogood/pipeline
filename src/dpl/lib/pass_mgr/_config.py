"""
pass_mgr configuration — loading and validation of per-environment config.

Exactly two config files exist, both in PROJECT_ROOT/config/:
    pass_mgr.laptop.yaml
    pass_mgr.prod.yaml

The environment is always an explicit parameter ('laptop' or 'prod');
nothing is inferred from hostnames or cwd.

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

#: Valid environment identifiers. Config file name is derived from these.
ENVIRONMENTS = ("laptop", "prod")

#: Environment identifier that triggers production policy enforcement.
PRODUCTION_ENVIRONMENT = "prod"

#: Config schema version understood by this loader. Anything else is a
#: hard error — see design doc §3, reserved for v2 key rotation.
SUPPORTED_CONFIG_VERSION = 1

#: Top-level keys allowed in a pass_mgr config file. Exhaustive on purpose:
#: unknown keys are rejected so typos cannot silently become dead config.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "default_method", "methods", "secrets"})

#: Keys allowed inside a single `secrets` entry's mapping.
ALLOWED_SECRET_KEYS = frozenset({"method"})

#: Method ids that are categorically insecure and may never be referenced
#: from the production config. Detected at load time, not at call time.
INSECURE_METHODS = frozenset({"plain"})


# --------------------------------------------------------------------------
# Validated config object
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PassMgrConfig:
    """Immutable, validated view of a pass_mgr environment config."""

    environment: str
    is_production: bool
    default_method: str
    #: Raw per-method config sections keyed by method id, exactly as they
    #: appear in the file. Providers receive their own section and perform
    #: their own deep validation (design doc §5) — this loader only
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
                f"pass_mgr: secret '{secret_name}' is not defined in the "
                f"{self.environment} config. Check the `secrets` mapping."
            ) from None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_config(environment: str, project_root: Path | None = None) -> PassMgrConfig:
    """Load and validate the pass_mgr config for `environment`.

    Args:
        environment: 'laptop' or 'prod'. Never inferred implicitly.
        project_root: root of the pipeline tree. Defaults to the parent of
            this package's parent (i.e. src/dpl/ -> project root). Override
            in tests.

    Returns:
        A fully validated PassMgrConfig.

    Raises:
        PassMgrConfigError: file missing, YAML syntax error, or any
            structural/validation violation.
        PassMgrProductionPolicyError: an insecure method is referenced
            anywhere in the production config.
    """
    if environment not in ENVIRONMENTS:
        raise PassMgrConfigError(
            f"pass_mgr: unknown environment '{environment}'. "
            f"Expected one of: {', '.join(ENVIRONMENTS)}."
        )

    root = _resolve_project_root(project_root)
    config_path = root / "config" / f"pass_mgr.{environment}.yaml"

    if not config_path.is_file():
        raise PassMgrConfigError(
            f"pass_mgr: config file not found: {config_path}. "
            f"Environments are named explicitly; create the file or fix "
            f"the environment name passed to load_config()."
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

    return _validate(raw, environment, config_path, root)


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
    environment: str,
    config_path: Path,
    root: Path,
) -> PassMgrConfig:
    is_production = environment == PRODUCTION_ENVIRONMENT

    # --- top-level shape -------------------------------------------------
    unknown_keys = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise PassMgrConfigError(
            f"pass_mgr: unknown top-level key(s) in {config_path.name}: "
            f"{sorted(unknown_keys)}. Allowed keys: "
            f"{sorted(ALLOWED_TOP_LEVEL_KEYS)}. Typo protection — fix or "
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
            f"This is refused at load time by design — production secrets "
            f"must use sops (or fernet as fallback)."
        )

    return PassMgrConfig(
        environment=environment,
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

    Key-file paths (age_key_file, key_file) may use ~/... — expanded to
    the user's home, but the result must stay INSIDE home. Everything
    else (binaries, vaults, manifest) must be relative and stay inside
    the project tree, as before.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise PassMgrConfigError(
            f"pass_mgr: path for '{what}' must be a non-empty string, got "
            f"{raw_value!r}."
        )
    if what.endswith("_key_file") and raw_value.startswith("~"):
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