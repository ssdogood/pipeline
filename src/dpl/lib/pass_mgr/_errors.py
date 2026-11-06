"""
pass_mgr error taxonomy - typed exceptions for secret retrieval failures.

Each subclass is distinct enough that callers can catch specific conditions
(e.g., "binary missing" vs "decryption failed") without parsing message text.

All errors inherit from PassMgrError, enabling broad catch blocks when
granularity isn't needed.
"""


class PassMgrError(Exception):
    """Base class for all pass_mgr failures."""


class PassMgrConfigError(PassMgrError):
    """Invalid configuration file - syntax error, unknown key, undefined method, etc."""


class PassMgrProductionPolicyError(PassMgrError):
    """Attempted use of an insecure method (e.g., 'plain') in production environment."""


class PassMgrSecretNotFoundError(PassMgrError):
    """Requested secret name does not exist in the loaded vault."""


class PassMgrBinaryMissingError(PassMgrError):
    """Required external binary (sops) not found at configured path."""


class PassMgrVersionMismatchError(PassMgrError):
    """External binary version does not match pinned version in manifest."""


class PassMgrKeyFileMissingError(PassMgrError):
    """Required key/identity file not found at configured path."""


class PassMgrDecryptError(PassMgrError):
    """Decryption failed - wrong key, corrupted ciphertext, or timeout."""
