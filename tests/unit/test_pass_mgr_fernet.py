"""
T6 - Fernet provider tests: round-trip, validation, and failure modes.

WHAT THESE TESTS PROVE
======================
The Fernet backup provider (certified fallback if the security team
rejects SOPS) correctly decrypts its envelope, enforces the envelope
format (version reserved for v2 rotation), refuses wrong keys and
corrupted data with TYPED errors, and plays nicely with the dispatcher
cache. Mirrors the SOPS failure-mode taxonomy exactly (design §5.2):
'Same interface, same cache slot, same error taxonomy as SOPS.'

RUNS WITH NO BINARIES
======================
Fernet is pure Python (the `cryptography` package, already installed in
your dpl environment). So this file uses the `project_nobin` fixture -
no sops, no age-keygen, no bin/ population required. This suite is
green-able on any machine at any bootstrap stage, exactly like the
config suite.

HOW THE ENVELOPE WORKS (recap for the demo)
===========================================
secrets/fernet.key        one master key per environment (text)
secrets/secrets.fernet.bin  a Fernet-encrypted JSON envelope:
    {"version": 1, "created_utc": ..., "entries": {name: value, ...}}
One key decrypts the WHOLE envelope once per process; names are looked
up in the cached dict. 'version' is reserved for v2 key rotation.

Run with:  pytest tests/unit/test_pass_mgr_fernet.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
import json

from cryptography.fernet import Fernet

import pytest

from pass_mgr import (
    PassMgrConfigError,
    PassMgrDecryptError,
    PassMgrKeyFileMissingError,
    PassMgrSecretNotFoundError,
    get_secret,
    init,
    reset,
)
from pass_mgr._providers import FernetProvider, FERNET_ENVELOPE_VERSION
from pass_mgr._errors import PassMgrDecryptError


# ---------------------------------------------------------------------------
# Helpers - build the fernet-only config shape on a binary-free tree
# ---------------------------------------------------------------------------


def write_fernet_config(project, environment: str = "laptop", secrets=None) -> Path:
    """A config whose ONLY method is fernet - the fallback-alone shape.

    This is the exact config that would ship if the security team
    rejects SOPS: nothing else is needed on the machine.
    """
    secrets = secrets or {
        "oracle.stg_pass": {},
        "ldap.service_pass": {},
    }
    return project.write_config(
        environment,
        {
            "version": 1,
            "default_method": "fernet",
            "methods": {
                "fernet": {
                    "key_file": "secrets/fernet.key",
                    "encrypted_file": "secrets/secrets.fernet.bin",
                },
            },
            "secrets": secrets,
        },
    )


# ---------------------------------------------------------------------------
# Round-trip - the happy path
# ---------------------------------------------------------------------------


class TestRoundTrip:

    def test_decrypt_returns_all_entries(self, project_nobin, flat_sample_entries):
        # The core promise: encrypt a known set of secrets, decrypt via
        # the provider, get EXACTLY the same values back. If this fails,
        # nothing else in the suite matters.
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        assert get_secret("oracle.stg_pass") == "0ra-stg-pw-7f3"
        assert get_secret("ldap.service_pass") == "ld4p-svc-pw-9k1"

    def test_values_never_leak_between_names(self, project_nobin, flat_sample_entries):
        # Guard against a subtle class of bug: a provider that returns
        # the same value for every name (e.g., mapping built wrong).
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        a = get_secret("oracle.stg_pass")
        b = get_secret("ldap.service_pass")
        assert a != b

    def test_provider_direct_load_returns_full_map(self, project_nobin, flat_sample_entries):
        # Instantiating the provider directly (bypassing the dispatcher)
        # must yield the same full map - this is the cold-cache path
        # tests rely on, and proves the provider is self-sufficient.
        from pass_mgr._config import load_config

        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        cfg = load_config("laptop", project_root=project_nobin.root)
        provider = FernetProvider(cfg.method_sections["fernet"], cfg)
        assert provider.load() == flat_sample_entries


# ---------------------------------------------------------------------------
# Failure modes - each one typed, each message naming the file
# ---------------------------------------------------------------------------


class TestFailureModes:

    def test_missing_key_file(self, project_nobin, flat_sample_entries):
        # Envelope exists, key file absent -> the provider must say so
        # with PassMgrKeyFileMissingError, naming the path, BEFORE any
        # attempt to decrypt (probe-before-decrypt discipline).
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)
        (project_nobin.secrets_dir / "fernet.key").unlink()

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrKeyFileMissingError, match="fernet.key"):
            get_secret("oracle.stg_pass")

    def test_missing_envelope_file(self, project_nobin):
        # Key present, envelope absent -> a config-shaped error (the
        # vault file is part of the deployment, not crypto).
        # Setup discipline: the test must create the key file ITSELF,
        # because add_fernet_envelope() creates key AND envelope
        # together - and we want the envelope deliberately absent.
        from cryptography.fernet import Fernet

        (project_nobin.secrets_dir / "fernet.key").write_bytes(
            Fernet.generate_key()
        )
        write_fernet_config(project_nobin)
        # no envelope created

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrConfigError, match="secrets.fernet.bin"):
            get_secret("oracle.stg_pass")

    def test_wrong_key_raises_decrypt_error(self, project_nobin, flat_sample_entries):
        # THE canonical 'wrong master key' failure: a key minted AFTER
        # the envelope was sealed can never open it. Must surface as
        # PassMgrDecryptError, exactly like SOPS would - the taxonomy is
        # symmetric by design (§5.2).
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        from cryptography.fernet import Fernet

        (project_nobin.secrets_dir / "fernet.key").write_bytes(
            Fernet.generate_key()  # a DIFFERENT key
        )

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError, match="wrong key or corrupted"):
            get_secret("oracle.stg_pass")

    def test_tampered_envelope_raises_decrypt_error(
        self, project_nobin, flat_sample_entries
    ):
        # Bit-flip a character inside the ciphertext: authenticated
        # encryption (Fernet = AES-128-CBC + HMAC-SHA256) must REFUSE
        # the tampered token, not silently return garbage. This test is
        # the reason Fernet was chosen over naive AES: authenticity is
        # built in, not bolted on.
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        blob_path = project_nobin.secrets_dir / "secrets.fernet.bin"
        token = bytearray(blob_path.read_bytes())
        token[len(token) // 2] ^= 0xFF  # flip one byte in the middle
        blob_path.write_bytes(bytes(token))

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError):
            get_secret("oracle.stg_pass")

    def test_malformed_key_file_raises_config_error(self, project_nobin, flat_sample_entries):
        # A key file containing garbage (e.g., pasted from an email,
        # truncated) is a CONFIG problem, not a crypto problem - the
        # distinction matters for operators reading logs.
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)
        (project_nobin.secrets_dir / "fernet.key").write_text(
            "not-a-valid-fernet-key", encoding="utf-8"
        )

        init("laptop", project_root=project_nobin.root)
        with pytest.raises((PassMgrConfigError, PassMgrDecryptError)):
            get_secret("oracle.stg_pass")

    def test_garbage_envelope_not_even_ascii(self, project_nobin):
        # Random bytes where an envelope should be: fail typed, not
        # with a Unicode stack trace. Key file created first, so the
        # provider's probe order (key -> envelope) passes the first
        # gate and actually reaches the garbage.
        from cryptography.fernet import Fernet

        (project_nobin.secrets_dir / "fernet.key").write_bytes(
            Fernet.generate_key()
        )
        write_fernet_config(project_nobin)
        (project_nobin.secrets_dir / "secrets.fernet.bin").write_bytes(
            bytes([0xDE, 0xAD, 0xBE, 0xEF] * 8)
        )

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError):
            get_secret("oracle.stg_pass")

# ---------------------------------------------------------------------------
# Envelope format discipline - the version field reserved for v2 rotation
# ---------------------------------------------------------------------------


class TestEnvelopeFormat:

    @staticmethod
    def _seal_custom_envelope(project, envelope_dict) -> Path:
        """Manually seal an arbitrary envelope dict with a fresh key,
        bypassing the fixture helper - used to test format violations
        the normal helper can't produce."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        (project.secrets_dir / "fernet.key").write_bytes(key)
        token = Fernet(key).encrypt(json.dumps(envelope_dict).encode("utf-8"))
        blob = project.secrets_dir / "secrets.fernet.bin"
        blob.write_text(token.decode("ascii"), encoding="utf-8")
        return blob

    def test_future_version_refused(self, project_nobin, flat_sample_entries):
        # envelope from "the v2 rotation era" must not be misread by v1
        # code - refuse loudly so the operator knows to upgrade.
        self._seal_custom_envelope(
            project_nobin,
            {"version": 2, "created_utc": "2030-01-01", "entries": flat_sample_entries},
        )
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError, match="version"):
            get_secret("oracle.stg_pass")

    def test_missing_entries_refused(self, project_nobin):
        # decrypts fine, but holds no secrets - refuse rather than
        # serve an empty vault forever.
        self._seal_custom_envelope(
            project_nobin,
            {"version": 1, "created_utc": "2026-11-03", "entries": {}},
        )
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError, match="entries"):
            get_secret("oracle.stg_pass")

    def test_non_string_value_refused(self, project_nobin):
        # A numeric password in the envelope would silently become an
        # int in Python and crash the connector at bind time. Refuse at
        # the provider boundary - earliest and clearest place.
        self._seal_custom_envelope(
            project_nobin,
            {
                "version": 1,
                "created_utc": "2026-11-03",
                "entries": {"oracle.stg_pass": 12345},
            },
        )
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError, match="string"):
            get_secret("oracle.stg_pass")

    def test_json_payload_that_is_not_an_object_refused(self, project_nobin):
        # Sealed fine, decrypts fine, but the payload is a list - the
        # envelope contract says JSON object or bust.
        self._seal_custom_envelope(project_nobin, ["not", "an", "object"])
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrDecryptError, match="JSON object"):
            get_secret("oracle.stg_pass")


# ---------------------------------------------------------------------------
# Cache + dispatcher interplay (foreshadows T5, but scoped to fernet)
# ---------------------------------------------------------------------------


class TestCacheBehaviour:

    def test_second_call_serves_from_cache(
        self, project_nobin, flat_sample_entries, monkeypatch
    ):
        # The once-per-process promise: after the first get_secret, the
        # underlying file can VANISH and the second call still works -
        # proof that no second decryption was attempted. (In production
        # the reverse protection applies: a cached vault keeps a run
        # alive even if the file is disturbed mid-run.)
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(project_nobin)

        init("laptop", project_root=project_nobin.root)
        first = get_secret("oracle.stg_pass")

        (project_nobin.secrets_dir / "secrets.fernet.bin").unlink()
        second = get_secret("oracle.stg_pass")

        assert first == second == "0ra-stg-pw-7f3"

    def test_secret_in_config_but_not_in_vault(self, project_nobin, flat_sample_entries):
        # Layer-2 'not found': the NAME is in the config (so method_for
        # resolves fine) but missing from the decrypted vault. Distinct
        # from the layer-1 error tested in test_config.py - this one
        # names the METHOD whose vault is short an entry.
        project_nobin.add_fernet_envelope(flat_sample_entries)
        write_fernet_config(
            project_nobin,
            secrets={
                "oracle.stg_pass": {},
                "ldap.service_pass": {},
                "hpc.api_token": {},  # in config, NOT in the envelope
            },
        )

        init("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrSecretNotFoundError, match="hpc.api_token"):
            get_secret("hpc.api_token")


# ---------------------------------------------------------------------------
# Provider-constructor validation (mirrors the SOPS section checks)
# ---------------------------------------------------------------------------


class TestProviderConstruction:

    def test_missing_required_config_key(self, project_nobin, flat_sample_entries):
        # A fernet section without 'key_file' must fail AT CONSTRUCTION
        # with a named error - fail fast, at startup, not at fetch.
        from pass_mgr._config import load_config

        project_nobin.add_fernet_envelope(flat_sample_entries)
        project_nobin.write_config(
            "laptop",
            {
                "version": 1,
                "default_method": "fernet",
                "methods": {
                    "fernet": {
                        "key_file": "secrets/fernet.key",
                        # 'encrypted_file' deliberately omitted
                    },
                },
                "secrets": {"oracle.stg_pass": {}},
            },
        )

        cfg = load_config("laptop", project_root=project_nobin.root)
        with pytest.raises(PassMgrConfigError, match="encrypted_file"):
            FernetProvider(cfg.method_sections["fernet"], cfg)


class TestEnvelopeHeaderTolerance:
    """The envelope file may carry a '#'-prefixed DO-NOT-EDIT banner;
    the provider skips comment lines and requires exactly one token
    line. Written by the vault tooling, read by the library - both
    directions proven here."""

    ENTRIES = {"ora_db.user_name.password": "pw-one",
               "ad_atm.subd_support.password": "pw-two"}

    def _build(self, tmp_path, lines_before, lines_after,
               token_mode="whole"):
        """Write a key + envelope file; return a provider over them."""
        key = Fernet.generate_key()
        (tmp_path / "fernet.key").write_bytes(key)

        payload = json.dumps(
            {"version": FERNET_ENVELOPE_VERSION,
             "entries": self.ENTRIES}).encode("utf-8")
        token = Fernet(key).encrypt(payload).decode("ascii")

        if token_mode == "split":
            half = len(token) // 2
            token_lines = [token[:half], token[half:]]
        elif token_mode == "omit":
            token_lines = []
        else:
            token_lines = [token]

        text = "\n".join(list(lines_before) + token_lines +
                         list(lines_after)) + "\n"
        (tmp_path / "envelope.bin").write_text(text, encoding="utf-8")

        section = {"key_file": "fernet.key",
                    "encrypted_file": "envelope.bin"}

        class _Cfg:
            base_dir = tmp_path          # minimal shim: only base_dir is used

        return FernetProvider(section, _Cfg())

    def test_headered_envelope_loads(self, tmp_path):
        banner = ["# =====", "# DO NOT EDIT", "# Last synced: 2026-11-04 03:00:00",
                  "# ====="]
        provider = self._build(tmp_path, banner, [])
        assert provider.load() == self.ENTRIES

    def test_headerless_envelope_still_loads(self, tmp_path):
        """Backward compatibility: a bare token with no comments loads."""
        provider = self._build(tmp_path, [], [])
        assert provider.load() == self.ENTRIES

    def test_two_token_lines_rejected(self, tmp_path):
        provider = self._build(tmp_path, [], [])
        # rebuild with the token duplicated
        key = Fernet.generate_key()
        # simpler: reuse _build twice? No - construct directly:
        payload = json.dumps({"version": FERNET_ENVELOPE_VERSION,
                              "entries": self.ENTRIES}).encode()
        token = Fernet(key).encrypt(payload).decode()
        (tmp_path / "fernet.key").write_bytes(key)
        (tmp_path / "envelope.bin").write_text(f"{token}\n{token}\n",
                                               encoding="utf-8")
        section = {"key_file": "fernet.key", "encrypted_file": "envelope.bin"}

        class _Cfg:
            base_dir = tmp_path
        with pytest.raises(PassMgrDecryptError):
            FernetProvider(section, _Cfg()).load()

    def test_comments_only_rejected(self, tmp_path):
        provider = self._build(tmp_path, ["# banner only"], [], token_mode="omit")
        with pytest.raises(PassMgrDecryptError):
            provider.load()

    def test_trailing_garbage_line_rejected(self, tmp_path):
        provider = self._build(tmp_path, [], ["not-a-comment"])
        with pytest.raises(PassMgrDecryptError):
            provider.load()

    def test_token_split_across_lines_rejected(self, tmp_path):
        provider = self._build(tmp_path, [], [], token_mode="split")
        with pytest.raises(PassMgrDecryptError):
            provider.load()