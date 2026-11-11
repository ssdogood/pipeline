"""Unit tests for dpl/lib/email_service/config.py (stage 4a).

Runs under pytest AND standalone:
    python tests/unit/test_email_config.py

Strategy: each test writes a small YAML config (mutating a known-good
BASE text) into tmp_path together with a templates directory, then
asserts load_email_config either succeeds with the expected fields or
raises ConfigError whose collected messages name every fault.

Two guarantees pinned explicitly:
  - the COLLECTOR guarantee: server-section faults do NOT hide
    caller-section faults (every error in the file surfaces together);
  - capability gates: BCC/HTML/domain policy are server-ceiling AND
    caller-opt-in (logical AND), enforced at load time.

Fixture discipline: hostile payloads (addresses with display names,
embedded newlines, quotes) are SERIALIZED into the YAML via json.dumps
(a JSON string is a valid YAML double-quoted scalar), so the literal
value reaches the validator intact - pasting them raw makes YAML itself
reject the file, which tests the YAML parser instead of the validator.
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from dpl.lib.email_service.config import (
    RESERVED_TEMPLATE_KEYS,
    ConfigError,
    load_email_config,
)

BASE = """\
email:
  host: smtp.example.org
  mode: ssl
  auth:
    scheme: basic
    username_ref: smtp/username
    password_ref: smtp/password
  sender: dpl-pipeline@example.org
  allow_html: false
  allow_bcc: true
  allowed_recipient_domains: []
  attachments:
    allowed_extensions: [csv, xlsx, log, txt, parquet]
    max_total_mb: 10
  retries: 2
  timeout_sec: 30
  dry_run: true
  templates_dir: templates
  outbox_dir: outbox
callers:
  daily_report:
    enabled: true
    to: [ops@example.org]
    cc: [lead@example.org]
    bcc: []
    template: summary.txt
    use_html: false
    data:
      environment: laptop
"""

TEMPLATE_TEXT = (
    "Daily run report for ${run_id}\n"
    "Rows: ${row_count}\n"
    "Env: ${environment}\n"
    "Ref: ${send_id} at ${generated_at} by ${caller}\n"
)


# ----------------------------------------------------------------- helpers --

def setup_env(tmp: Path) -> Path:
    (tmp / "templates").mkdir(parents=True, exist_ok=True)
    (tmp / "templates" / "summary.txt").write_text(TEMPLATE_TEXT,
                                                   encoding="utf-8")
    cfg = tmp / "email_service.yaml"
    cfg.write_text(BASE.replace("templates_dir: templates",
                                f"templates_dir: {tmp / 'templates'}"),
                   encoding="utf-8")
    return cfg


def cfg_with_many(tmp: Path,
                  replacements: list[tuple[str, str]]) -> Path:
    """Write the BASE config with one or more (old, new) replacements.
    Anchors must be unique in BASE - asserted so a typo'd fixture
    fails loudly at setup, not mysteriously at assertion time."""
    setup_env(tmp)
    text = BASE
    text = text.replace("templates_dir: templates",
                        f"templates_dir: {tmp / 'templates'}")
    cfg = tmp / "email_service.yaml"
    for old, new in replacements:
        assert old in text, f"fixture anchor missing: {old!r}"
        text = text.replace(old, new)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def errors_of_many(tmp: Path,
                    replacements: list[tuple[str, str]]) -> list[str]:
    with pytest.raises(ConfigError) as ei:
        load_email_config(cfg_with_many(tmp, replacements))
    return ei.value.errors


def cfg_with(tmp: Path, old: str, new: str) -> Path:
    return cfg_with_many(tmp, [(old, new)])


def errors_of(tmp: Path, old: str, new: str) -> list[str]:
    return errors_of_many(tmp, [(old, new)])


# ------------------------------------------------------------------ valid ---

def test_valid_base_loads(tmp_path: Path) -> None:
    cfg = load_email_config(setup_env(tmp_path))
    assert cfg.server.host == "smtp.example.org"
    assert cfg.server.port == 465                 # ssl default
    assert cfg.server.mode == "ssl"
    assert cfg.server.auth_scheme == "basic"
    assert cfg.server.password_ref == "smtp/password"
    assert cfg.server.allow_bcc is True
    assert cfg.server.dry_run is True
    assert cfg.server.max_attach_mb == 10.0
    assert cfg.server.attach_extensions == (
        "csv", "xlsx", "log", "txt", "parquet")
    assert "daily_report" in cfg.callers
    assert cfg.callers["daily_report"].to == ("ops@example.org",)


def test_port_defaults_per_mode(tmp_path: Path) -> None:
    for mode, port in (("ssl", 465), ("starttls", 587), ("plain", 25)):
        cfg = load_email_config(cfg_with(
            tmp_path, "  mode: ssl\n", f"  mode: {mode}\n"))
        assert cfg.server.port == port, mode


def test_plain_mode_explicit_is_accepted(tmp_path: Path) -> None:
    cfg = load_email_config(cfg_with(
        tmp_path, "  mode: ssl\n", "  mode: plain\n"))
    assert cfg.server.mode == "plain"


def test_reserved_keys_constant(tmp_path: Path) -> None:
    assert RESERVED_TEMPLATE_KEYS == frozenset(
        {"generated_at", "send_id", "caller"})


def test_template_placeholders_extracted(tmp_path: Path) -> None:
    cfg = load_email_config(setup_env(tmp_path))
    ph = cfg.callers["daily_report"].placeholders
    assert "run_id" in ph and "row_count" in ph
    # the three reserved keys appear in the template text too
    assert {"send_id", "generated_at", "caller"} <= set(ph)


# ----------------------------------------------------- collect-all-errors ---

def test_collects_all_errors(tmp_path: Path) -> None:
    """Server faults AND caller faults must appear in ONE report -
    a server-section fault must not hide a caller-section fault."""
    errs = errors_of_many(tmp_path, [
        ("  host: smtp.example.org\n", ""),          # server fault 1
        ("  mode: ssl\n", "  mode: tls\n"),          # server fault 2
        ("    template: summary.txt\n",
         "    template: nosuch.txt\n"),              # caller fault
    ])
    joined = "\n".join(errs)
    assert "'email.host'" in joined
    assert "'email.mode'" in joined
    assert "nosuch.txt" in joined
    assert len(errs) >= 3, "all three faults must surface together"


def test_collected_errors_listed_in_message(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as ei:
        load_email_config(cfg_with(tmp_path, "  mode: ssl\n",
                                   "  mode: carrier-pigeon\n"))
    msg = str(ei.value)
    assert msg.startswith("1 email configuration error(s):")
    assert "carrier-pigeon" in msg


def test_missing_host_and_sender_both_reported(tmp_path: Path) -> None:
    errs = errors_of_many(tmp_path, [
        ("  host: smtp.example.org\n", ""),
        ("  sender: dpl-pipeline@example.org\n", ""),
    ])
    joined = "\n".join(errs)
    assert "'email.host'" in joined and "'email.sender'" in joined


# ---------------------------------------------------------- server section --

def test_unknown_global_key_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  retries: 2\n",
                     "  retries: 2\n  smpt_host: typo-o\n")
    assert any("smpt_host" in e for e in errs)


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  mode: ssl\n", "  mode: tls\n")
    assert any("mode" in e for e in errs)


def test_bad_port_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  mode: ssl\n", "  mode: ssl\n  port: 0\n")
    assert any("'email.port'" in e for e in errs)


def test_auth_literal_password_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    password_ref: smtp/password\n",
                     "    password_ref: smtp/password\n"
                     "    password: hunter2\n")
    assert any("FORBIDDEN" in e and "password" in e for e in errs)


def test_auth_basic_missing_refs_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path,
                     "    username_ref: smtp/username\n"
                     "    password_ref: smtp/password\n", "")
    joined = "\n".join(errs)
    assert "username_ref" in joined and "password_ref" in joined


def test_auth_none_needs_no_refs(tmp_path: Path) -> None:
    cfg = load_email_config(cfg_with(
        tmp_path, "    scheme: basic\n", "    scheme: none\n"))
    assert cfg.server.auth_scheme == "none"
    assert cfg.server.password_ref is None


def test_dry_run_required_no_default(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  dry_run: true\n", "")
    assert any("dry_run" in e and "REQUIRED" in e for e in errs)


def test_bad_bools_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  allow_bcc: true\n",
                     "  allow_bcc: sometimes\n")
    assert any("allow_bcc" in e for e in errs)


def test_invalid_domain_entry_rejected(tmp_path: Path) -> None:
    errs = errors_of(
        tmp_path, "  allowed_recipient_domains: []\n",
        "  allowed_recipient_domains: [example org]\n")
    assert any("invalid domain" in e for e in errs)


def test_bad_attachments_block_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    max_total_mb: 10\n",
                     "    max_total_mb: -3\n")
    assert any("max_total_mb" in e for e in errs)


def test_bad_retries_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  retries: 2\n", "  retries: -1\n")
    assert any("retries" in e for e in errs)


def test_missing_templates_dir_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  templates_dir: ",
                     "  templates_dir: nosuchdir/")
    assert any("templates_dir" in e for e in errs)


# --------------------------------------------------------- caller section ---

def test_caller_bcc_blocked_when_capability_off(tmp_path: Path) -> None:
    errs = errors_of_many(tmp_path, [
        ("  allow_bcc: true\n", "  allow_bcc: false\n"),
        ("    bcc: []\n", "    bcc: [auditor@example.org]\n"),
    ])
    assert any("BCC is disabled" in e for e in errs), \
        "caller bcc list must be rejected when the server gate is off"


def test_domain_policy_applies_to_to_cc_bcc(tmp_path: Path) -> None:
    errs = errors_of_many(tmp_path, [
        ("  allowed_recipient_domains: []\n",
         "  allowed_recipient_domains: [example.org]\n"),
        ("    cc: [lead@example.org]\n",
         "    cc: [lead@other.net]\n"),
        ("    bcc: []\n", "    bcc: [watcher@other.net]\n"),
    ])
    joined = "\n".join(errs)
    assert "lead@other.net" in joined, "CC checked against the policy"
    assert "watcher@other.net" in joined, "BCC checked against the policy"
    assert not any("ops@example.org" in e for e in errs), \
        "in-domain To recipient must NOT be flagged"


def test_invalid_address_syntax_rejected(tmp_path: Path) -> None:
    # Hostile payloads are SERIALIZED (json.dumps -> valid YAML double-
    # quoted scalar) so the literal string - display names, quotes,
    # embedded newline - reaches the validator intact.
    for bad in ("ops@@example.org", "ops@example",
                '"Ops Team" <ops@example.org>',
                "ops@example.org\nBcc: evil@attacker.net",
                "ops example@example.org"):
        errs = errors_of(tmp_path, "to: [ops@example.org]",
                         f"to: [{json.dumps(bad)}]")
        assert any("invalid recipient address" in e for e in errs), bad


def test_use_html_blocked_when_global_off(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    use_html: false\n",
                     "    use_html: true\n")
    assert any("allow_html" in e for e in errs)


def test_use_html_ok_when_global_on(tmp_path: Path) -> None:
    cfg = load_email_config(cfg_with(
        tmp_path, "  allow_html: false\n  ", "  allow_html: true\n  "))
    assert cfg.callers["daily_report"].use_html is False  # unchanged
    assert cfg.server.allow_html is True


def test_missing_template_file_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    template: summary.txt\n",
                     "    template: nosuch.txt\n")
    assert any("nosuch.txt" in e for e in errs)


def test_reserved_data_key_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "      environment: laptop\n",
                     "      environment: laptop\n"
                     "      generated_at: yesterday\n")
    assert any("RESERVED" in e and "generated_at" in e for e in errs)


def test_non_scalar_data_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "      environment: laptop\n",
                     "      environment: laptop\n"
                     "      nested: {a: 1}\n")
    assert any("scalar" in e for e in errs)


def test_unknown_caller_key_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    enabled: true\n",
                     "    enabled: true\n    templat: typo.txt\n")
    assert any("templat" in e for e in errs)


def test_enabled_required(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    enabled: true\n", "")
    assert any("enabled" in e and "required" in e for e in errs)


def test_caller_without_recipients_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "    to: [ops@example.org]\n",
                     "    to: []\n")
    assert any("at least one recipient" in e for e in errs)


def test_bad_caller_name_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "  daily_report:\n", "  Daily Report:\n")
    assert any("caller name" in e for e in errs)


def test_missing_callers_section_rejected(tmp_path: Path) -> None:
    errs = errors_of(tmp_path, "callers:\n", "")
    assert any("'callers' section is missing" in e for e in errs)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as ei:
        load_email_config(tmp_path / "absent.yaml")
    assert any("not found" in e for e in ei.value.errors)


# ------------------------------------------------------ standalone runner ----


def main() -> int:
    tests: list[tuple[str, Callable[..., object]]] = [
        ("valid_base_loads", test_valid_base_loads),
        ("port_defaults_per_mode", test_port_defaults_per_mode),
        ("plain_mode_explicit_is_accepted", test_plain_mode_explicit_is_accepted),
        ("reserved_keys_constant", test_reserved_keys_constant),
        ("template_placeholders_extracted",
         test_template_placeholders_extracted),
        ("collects_all_errors", test_collects_all_errors),
        ("collected_errors_listed_in_message",
         test_collected_errors_listed_in_message),
        ("missing_host_and_sender_both_reported",
         test_missing_host_and_sender_both_reported),
        ("unknown_global_key_rejected", test_unknown_global_key_rejected),
        ("unknown_mode_rejected", test_unknown_mode_rejected),
        ("bad_port_rejected", test_bad_port_rejected),
        ("auth_literal_password_rejected",
         test_auth_literal_password_rejected),
        ("auth_basic_missing_refs_rejected",
         test_auth_basic_missing_refs_rejected),
        ("auth_none_needs_no_refs", test_auth_none_needs_no_refs),
        ("dry_run_required_no_default", test_dry_run_required_no_default),
        ("bad_bools_rejected", test_bad_bools_rejected),
        ("invalid_domain_entry_rejected",
         test_invalid_domain_entry_rejected),
        ("bad_attachments_block_rejected",
         test_bad_attachments_block_rejected),
        ("bad_retries_rejected", test_bad_retries_rejected),
        ("missing_templates_dir_rejected",
         test_missing_templates_dir_rejected),
        ("caller_bcc_blocked_when_capability_off",
         test_caller_bcc_blocked_when_capability_off),
        ("domain_policy_applies_to_to_cc_bcc",
         test_domain_policy_applies_to_to_cc_bcc),
        ("invalid_address_syntax_rejected",
         test_invalid_address_syntax_rejected),
        ("use_html_blocked_when_global_off",
         test_use_html_blocked_when_global_off),
        ("use_html_ok_when_global_on", test_use_html_ok_when_global_on),
        ("missing_template_file_rejected",
         test_missing_template_file_rejected),
        ("reserved_data_key_rejected", test_reserved_data_key_rejected),
        ("non_scalar_data_rejected", test_non_scalar_data_rejected),
        ("unknown_caller_key_rejected", test_unknown_caller_key_rejected),
        ("enabled_required", test_enabled_required),
        ("caller_without_recipients_rejected",
         test_caller_without_recipients_rejected),
        ("bad_caller_name_rejected", test_bad_caller_name_rejected),
        ("missing_callers_section_rejected",
         test_missing_callers_section_rejected),
        ("missing_config_file", test_missing_config_file),
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for name, fn in tests:
            try:
                fn(Path(td))
                print(f"  PASS {name}")
            except Exception as exc:        # noqa: BLE001 - runner sentinel
                failures.append(name)
                print(f"  FAIL {name}: {exc}")
    total, bad = len(tests), len(failures)
    if bad:
        print(f"RESULT: {total - bad}/{total} passed, FAILED: {failures}")
        return 1
    print(f"RESULT: ALL GREEN ({total}/{total}) - stage 4a config verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
