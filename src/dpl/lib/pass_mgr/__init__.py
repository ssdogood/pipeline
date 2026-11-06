"""
pass_mgr - the single authority for retrieving secrets by name.

Public surface:

    import pass_mgr
    pass_mgr.init(environment="laptop")          # once, at startup
    value = pass_mgr.get_secret("oracle.stg_pass")

Callers (connectors) pass a secret NAME and nothing else. They know no
method, no path, no key file, no binary. Everything else - config loading,
provider dispatch, decryption, caching - happens behind this facade.

Lifecycle (design doc §6):
    init() is called once per process with an explicit environment. Vault
    decryption is lazy - nothing is decrypted until the first get_secret()
    call - and each referenced provider's vault is decrypted at most ONCE
    per process, then held in a module-level cache. The 10-minute cron
    extracts therefore pay one decryption per provider per run.

Tests instantiate providers/loaders directly for cold-cache scenarios;
reset() exists solely so the test suite can recover a virgin dispatcher
between tests without process restarts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._config import PassMgrConfig, load_config
from ._errors import (
    PassMgrBinaryMissingError,
    PassMgrConfigError,
    PassMgrDecryptError,
    PassMgrKeyFileMissingError,
    PassMgrProductionPolicyError,
    PassMgrSecretNotFoundError,
    PassMgrVersionMismatchError,
)
from ._providers import PROVIDER_TYPES

__all__ = [
    # facade
    "init",
    "get_secret",
    "reset",
    # errors, for caller `except` clauses
    "PassMgrError",
    "PassMgrConfigError",
    "PassMgrDecryptError",
    "PassMgrBinaryMissingError",
    "PassMgrKeyFileMissingError",
    "PassMgrProductionPolicyError",
    "PassMgrSecretNotFoundError",
    "PassMgrVersionMismatchError",
]
# PassMgrError is re-exported too; keep the linters happy:
from ._errors import PassMgrError

# ---------------------------------------------------------------------------
# Module-level dispatcher state
# ---------------------------------------------------------------------------

class _Dispatcher:
    """Lazy, per-process vault cache for one environment."""

    def __init__(self, config: PassMgrConfig):
        self.config = config
        self._vaults: dict[str, dict[str, str]] = {}
        self._providers: dict[str, Any] = {}

    def _provider(self, method_id: str) -> Any:
        if method_id not in self._providers:
            provider_cls = PROVIDER_TYPES.get(method_id)
            if provider_cls is None:
                raise PassMgrConfigError(
                    f"pass_mgr: method '{method_id}' is referenced in "
                    f"config but has no provider implementation "
                    f"(known: {sorted(PROVIDER_TYPES)})."
                )
            section = self.config.method_sections.get(method_id)
            if section is None:
                raise PassMgrConfigError(
                    f"pass_mgr: method '{method_id}' has no config "
                    f"section in pass_mgr.{self.config.environment}.yaml."
                )
            self._providers[method_id] = provider_cls(section, self.config)
        return self._providers[method_id]

    def _vault(self, method_id: str) -> dict[str, str]:
        """Decrypt-and-cache, at most once per provider per process."""
        if method_id not in self._vaults:
            self._vaults[method_id] = self._provider(method_id).load()
        return self._vaults[method_id]

    def get_secret(self, name: str) -> str:
        method_id = self.config.method_for(name)
        vault = self._vault(method_id)
        try:
            return vault[name]
        except KeyError:
            raise PassMgrSecretNotFoundError(
                f"pass_mgr: secret '{name}' resolves to method "
                f"'{method_id}', but the {method_id} vault does not "
                f"contain it. Add it to the vault file and re-encrypt."
            ) from None


_dispatcher: _Dispatcher | None = None


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------

def init(environment: str, project_root: Path | None = None) -> None:
    """Initialize pass_mgr for this process.

    Loads and validates pass_mgr.<environment>.yaml - all configuration
    errors (bad YAML, unknown keys, undefined methods, insecure methods in
    production) surface HERE, at startup, never on first fetch. The
    environment is always explicit; nothing is inferred from the host.

    Idempotent: calling init() again with the same environment is a no-op;
    calling it with a DIFFERENT environment resets the cache deliberately
    (a config error if you were expecting the old one to persist).
    """
    global _dispatcher

    if _dispatcher is not None and _dispatcher.config.environment == environment:
        return

    config = load_config(environment, project_root=project_root)
    _dispatcher = _Dispatcher(config)


def get_secret(name: str) -> str:
    """Return the plaintext secret identified by `name`.

    Callers pass only a name - no method, no path, no key. Vault decryption
    is performed at most once per provider per process and cached in
    memory.

    Raises:
        PassMgrSecretNotFoundError: name not in config's secrets map, or
            not in the decrypted vault (two different failure layers,
            distinguished in the message).
        PassMgrConfigError / ProductionPolicyError: init() was skipped,
            or late config discovery - normally impossible after a
            successful init().
        PassMgrBinaryMissingError / VersionMismatchError /
            KeyFileMissingError / DecryptError: the deferred-cost failure
            modes, surfacing on FIRST access (lazy decryption).
    """
    if _dispatcher is None:
        raise PassMgrConfigError(
            "pass_mgr: get_secret() called before init(). Call "
            "pass_mgr.init(environment='laptop'|'prod') once at startup."
        )
    return _dispatcher.get_secret(name)


def reset() -> None:
    """Drop all cached state. Test-suite use only - no caller code ever
    calls this in production paths. Exists because the cache is
    deliberately once-per-process and pytest wants a cold dispatcher
    per test without subprocess gymnastics."""
    global _dispatcher
    _dispatcher = None
