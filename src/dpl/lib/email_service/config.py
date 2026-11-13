# dpl/lib/email_service/config.py
"""Email service configuration: load, validate, fail loudly - all at once.

Stage 4a of the email service. Owns Layers A (server), B (callers) and
C (template registry) from the requirements spec v0.5.

Every invalid condition is COLLECTED and reported together - a config
with eight faults produces one ConfigError listing all eight, never
one-at-a-time whack-a-mole. To keep that promise when the SERVER
section is faulty, _load_server ALWAYS returns a ServerConfig: bad
fields carry placeholder values (never usable - load_email_config
raises whenever any fault exists) so caller sections still get
validated and every fault in the file surfaces in a single report.

Validation happens ONLY here, at load time; nothing downstream may
discover a config fault at send time.

Config file shape (default: config/email_service.yaml):

    email:                              # Layer A - server (rarely touched)
      host: smtp.example.org
      port: 465                         # default per mode: ssl=465,
                                        # starttls=587, plain=25
      mode: ssl                         # ssl | starttls | plain
                                        # plain = EXPLICIT opt-in only,
                                        # never negotiated or fallen back
      auth:
        scheme: basic                   # basic | none
        username: relay-user            # literal is OK (public config);
                                        # OR username_ref (vault) - never both
        password_ref: smtp.password.secret  # vault ONLY - a literal
                                        # 'password:' key is an ERROR
      sender: dpl-pipeline@example.org
      allow_html: false                 # capability ceiling (default false)
      allow_bcc: false                  # capability gate (default false)
      allowed_recipient_domains: []     # [] = any domain; else To/CC/BCC
                                        # must be within these domains
      attachments:
        allowed_extensions: [csv, xlsx, log, txt, parquet]
        max_total_mb: 10
      retries: 2                        # transport retries (default 2)
      timeout_sec: 30
      dry_run: true                     # REQUIRED - no default, ever
      templates_dir: config/email_templates
      outbox_dir: logs/email_outbox     # dry-run .eml drops land here

    callers:                            # Layer B - per activity
      daily_report:
        enabled: true                   # REQUIRED - explicit toggle
        to: [ops@example.org]
        cc: []
        bcc: []
        template: daily_run_summary.txt # file under templates_dir
        use_html: false                 # honored only if allow_html=true
        data: {}                        # static template values
        attachments: []                 # paths; resolved at send time
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from dpl.lib.logger import get_run_logger
from dpl.lib.paths import get_project_root

log = get_run_logger("dpl.email.config")

PROJECT_ROOT = get_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "email_service.yaml"

#: reserved template keys - stamped by the service at compose time
#: (architecture: reserved-keys rule). Caller data or caller-config
#: `data` entries with these names are a load-time error.
RESERVED_TEMPLATE_KEYS = frozenset(
    {"generated_at", "send_id", "caller"})

#: Tier-1 callers that may never be disabled via config. Failure alerts
#: are constitutionally on: a silenced run_failed means failed runs can
#: pass silently, which is the one outcome this service exists to
#: prevent. 'enabled: false' on one of these is a LOUD load error.
PROTECTED_CALLERS = frozenset({"run_failed"})

_MODES = ("ssl", "starttls", "plain")
_DEFAULT_PORT = {"ssl": 465, "starttls": 587, "plain": 25}
_AUTH_SCHEMES = ("none", "basic")
_AUTH_FORBIDDEN_LITERALS = ("password",)
_DEFAULT_ATTACH_EXTENSIONS = ("csv", "xlsx", "log", "txt", "parquet")
_DEFAULT_MAX_ATTACH_MB = 10.0

_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]*[a-z0-9])?$")
_CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_GLOBAL_KEYS = frozenset({
    "host", "port", "mode", "auth", "sender", "allow_html", "allow_bcc",
    "allowed_recipient_domains", "attachments", "retries", "timeout_sec",
    "dry_run", "templates_dir", "outbox_dir",
})
_AUTH_KEYS = frozenset(
    {"scheme", "username", "username_ref", "password_ref"}
    | set(_AUTH_FORBIDDEN_LITERALS))
_ATTACH_KEYS = frozenset({"allowed_extensions", "max_total_mb"})
_CALLER_KEYS = frozenset({
    "enabled", "to", "cc", "bcc", "template", "use_html", "data",
    "attachments",
})
PROTECTED_CALLERS = frozenset({"run_failed"})

class ConfigError(Exception):
    """All email configuration faults, collected and reported together."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        heading = f"{len(errors)} email configuration error(s):"
        super().__init__(
            heading + "\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass(frozen=True)
class ServerConfig:
    """Layer A - transport and capability ceilings. Rarely changes."""
    host: str
    port: int
    mode: str
    auth_scheme: str
    username_ref: str | None
    password_ref: str | None
    sender: str
    allow_html: bool
    allow_bcc: bool
    allowed_domains: tuple[str, ...]
    attach_extensions: tuple[str, ...]
    max_attach_mb: float
    retries: int
    timeout_sec: float
    dry_run: bool
    templates_dir: Path
    outbox_dir: Path
    username: str | None = None  # literal login (public); None = refs only


@dataclass(frozen=True)
class CallerConfig:
    """Layer B - one activity's send policy and routing."""
    name: str
    enabled: bool
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    template: str
    template_path: Path
    placeholders: tuple[str, ...]
    use_html: bool
    data: dict[str, object]
    attachments: tuple[str, ...]


@dataclass(frozen=True)
class EmailServiceConfig:
    """Fully validated configuration. Constructed only when zero errors."""
    server: ServerConfig
    callers: dict[str, CallerConfig]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _addr_domain_ok(addr: str, domains: tuple[str, ...]) -> bool:
    return addr.rsplit("@", 1)[1].lower() in domains


def _check_addresses(addrs: tuple[str, ...], where: str,
                     errors: list[str], domains: tuple[str, ...]) -> None:
    for addr in addrs:
        if not _ADDRESS_RE.fullmatch(addr):
            errors.append(
                f"{where}: invalid recipient address {addr!r} "
                f"(expected user@domain.tld; control characters, "
                f"display names and spaces are not allowed)")
        elif domains and not _addr_domain_ok(addr, domains):
            errors.append(
                f"{where}: recipient {addr!r} is outside the allowed "
                f"recipient domains {sorted(domains)}")


def _read_yaml(path: Path, errors: list[str]) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        errors.append(f"email config file not found: {path}")
    except yaml.YAMLError as exc:
        errors.append(f"email config file {path} is not valid YAML: {exc}")
    return None


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else PROJECT_ROOT / p


def _placeholder_server() -> ServerConfig:
    """ServerConfig used only while a faulty config is being diagnosed:
    invalid fields carry inert values so caller validation can still
    run and report its own faults in the same pass. Never returned to
    a caller - load_email_config raises whenever faults exist."""
    return ServerConfig(
        host="", port=0, mode="ssl", auth_scheme="none",
        username_ref=None, password_ref=None, sender="",
        allow_html=False, allow_bcc=False, allowed_domains=(),
        attach_extensions=_DEFAULT_ATTACH_EXTENSIONS,
        max_attach_mb=_DEFAULT_MAX_ATTACH_MB, retries=2, timeout_sec=30.0,
        dry_run=False,
        templates_dir=PROJECT_ROOT / "config" / "email_templates",
        outbox_dir=PROJECT_ROOT / "logs" / "email_outbox")


def _load_server(raw: object, errors: list[str]) -> ServerConfig:
    """Validate the 'email' section. ALWAYS returns a ServerConfig.

    Faulty fields are reported AND given inert placeholder values so
    that caller-section validation is never skipped - every fault in
    the whole file surfaces together (collect-all guarantee). The
    returned config must never be USED: load_email_config raises
    ConfigError whenever errors is non-empty.
    """
    if not isinstance(raw, dict):
        errors.append("'email' section is missing or not a mapping")
        return _placeholder_server()
    unknown = sorted(set(raw) - _GLOBAL_KEYS)
    if unknown:
        errors.append(f"'email': unknown key(s) {unknown}")

    host = raw.get("host")
    if not isinstance(host, str) or not host.strip():
        errors.append("'email.host' is required (non-empty string)")
        host = ""
    sender = raw.get("sender")
    if not isinstance(sender, str) or not _ADDRESS_RE.fullmatch(sender):
        errors.append(f"'email.sender' must be a valid address, "
                      f"got {sender!r}")
        sender = ""

    mode = raw.get("mode")
    if mode not in _MODES:
        errors.append(f"'email.mode' must be one of {_MODES}, got {mode!r} "
                      f"(plain must be an explicit, deliberate choice)")
        mode = "ssl"

    port_raw = raw.get("port")
    if port_raw is None:
        port = _DEFAULT_PORT[mode]
    elif _is_int(port_raw) and 1 <= port_raw <= 65535:
        port = port_raw
    else:
        port = 0
        errors.append(f"'email.port' must be an integer 1-65535, "
                      f"got {port_raw!r}")

    auth = raw.get("auth")
    scheme = "none"
    user_ref: str | None = None
    pass_ref: str | None = None
    username: str | None = None
    if not isinstance(auth, dict):
        errors.append("'email.auth' section is required "
                      "(scheme: basic or none)")
    else:
        auth_unknown = sorted(set(auth) - _AUTH_KEYS)
        if auth_unknown:
            errors.append(f"'email.auth': unknown key(s) {auth_unknown}")
        literals = [k for k in _AUTH_FORBIDDEN_LITERALS if k in auth]
        if literals:
            errors.append(
                f"'email.auth': literal secret key(s) {literals} are "
                f"FORBIDDEN - credentials live only in the vault and are "
                f"referenced by *_ref keys (pass_mgr pattern)")
        scheme = auth.get("scheme")
        if scheme not in _AUTH_SCHEMES:
            errors.append(f"'email.auth.scheme' must be one of "
                          f"{_AUTH_SCHEMES}, got {scheme!r}")
            scheme = "none"
        elif scheme == "basic":
            user_ref = auth.get("username_ref")
            pass_ref = auth.get("password_ref")
            username = auth.get("username")
            if isinstance(username, str) and not username.strip():
                username = None
            if not isinstance(pass_ref, str) or not pass_ref.strip():
                errors.append(
                    "'email.auth.password_ref' is required when "
                    "scheme=basic (vault reference, never a literal)")
                pass_ref = None
            has_ref = isinstance(user_ref, str) and bool(user_ref.strip())
            has_lit = isinstance(username, str) and bool(username.strip())
            if has_ref and has_lit:
                errors.append(
                    "'email.auth' accepts EITHER a literal 'username:' "
                    "(public config) OR a vault 'username_ref:', not both")
            elif not has_ref and not has_lit:
                errors.append(
                    "'email.auth' needs a login identity when "
                    "scheme=basic: either a literal 'username:' or a "
                    "vault 'username_ref:'")
                user_ref = None
                username = None
            if not has_ref:
                user_ref = None

    allow_html = raw.get("allow_html", False)
    if not isinstance(allow_html, bool):
        errors.append(f"'email.allow_html' must be a boolean, "
                      f"got {allow_html!r}")
        allow_html = False
    allow_bcc = raw.get("allow_bcc", False)
    if not isinstance(allow_bcc, bool):
        errors.append(f"'email.allow_bcc' must be a boolean, "
                      f"got {allow_bcc!r}")
        allow_bcc = False

    dom_raw = raw.get("allowed_recipient_domains", [])
    domains: list[str] = []
    if not isinstance(dom_raw, list):
        errors.append("'email.allowed_recipient_domains' must be a list")
    else:
        for d in dom_raw:
            dl = str(d).strip().lower()
            if not _DOMAIN_RE.fullmatch(dl):
                errors.append(
                    f"'email.allowed_recipient_domains': invalid domain "
                    f"{d!r}")
            else:
                domains.append(dl)

    ext_raw = raw.get("attachments", {})
    extensions: tuple[str, ...] = _DEFAULT_ATTACH_EXTENSIONS
    max_mb = _DEFAULT_MAX_ATTACH_MB
    if not isinstance(ext_raw, dict):
        errors.append("'email.attachments' must be a mapping")
    else:
        att_unknown = sorted(set(ext_raw) - _ATTACH_KEYS)
        if att_unknown:
            errors.append(
                f"'email.attachments': unknown key(s) {att_unknown}")
        exts = ext_raw.get("allowed_extensions", list(extensions))
        if not isinstance(exts, list) or not exts or \
                not all(isinstance(e, str) and e == e.strip().lower()
                        and e for e in exts):
            errors.append(
                "'email.attachments.allowed_extensions' must be a "
                "non-empty list of lowercase extensions")
        else:
            extensions = tuple(exts)
        mmb = ext_raw.get("max_total_mb", max_mb)
        if not _is_num(mmb) or mmb <= 0:
            errors.append("'email.attachments.max_total_mb' must be a "
                          "positive number")
        else:
            max_mb = float(mmb)

    retries = raw.get("retries", 2)
    if not _is_int(retries) or retries < 0:
        errors.append(f"'email.retries' must be an integer >= 0, "
                      f"got {retries!r}")
        retries = 2
    timeout_sec = raw.get("timeout_sec", 30)
    if not _is_num(timeout_sec) or timeout_sec <= 0:
        errors.append(f"'email.timeout_sec' must be a positive number, "
                      f"got {timeout_sec!r}")
        timeout_sec = 30.0
    else:
        timeout_sec = float(timeout_sec)

    dry_run = raw.get("dry_run")
    if not isinstance(dry_run, bool):
        errors.append(
            "'email.dry_run' is REQUIRED (true = compose+log only; "
            "false = send for real) - set it explicitly, never inherit")
        dry_run = False

    td_raw = raw.get("templates_dir")
    templates_dir = _placeholder_server().templates_dir
    if not isinstance(td_raw, str) or not td_raw.strip():
        errors.append("'email.templates_dir' is required")
    else:
        templates_dir = _resolve(Path(td_raw))
        if not templates_dir.is_dir():
            errors.append(
                f"'email.templates_dir' does not exist or is not a "
                f"directory: {templates_dir}")
    ob_raw = raw.get("outbox_dir", "logs/email_outbox")
    outbox_dir = _placeholder_server().outbox_dir
    if isinstance(ob_raw, str) and ob_raw.strip():
        outbox_dir = _resolve(Path(ob_raw))
    else:
        errors.append(f"'email.outbox_dir' must be a non-empty path, "
                      f"got {ob_raw!r}")

    if mode == "plain":
        log.warning(
            "email mode=plain: transport WITHOUT TLS was explicitly "
            "configured - every send will log this warning")
    return ServerConfig(
        host=host, port=port, mode=mode, auth_scheme=scheme,
        username_ref=user_ref, password_ref=pass_ref, username=username,
        sender=sender,
        allow_html=allow_html, allow_bcc=allow_bcc,
        allowed_domains=tuple(domains), attach_extensions=extensions,
        max_attach_mb=max_mb, retries=retries, timeout_sec=timeout_sec,
        dry_run=dry_run, templates_dir=templates_dir,
        outbox_dir=outbox_dir)


def _load_caller(name: str, raw: object, server: ServerConfig,
                 errors: list[str]) -> CallerConfig | None:
    where = f"callers.{name}"
    if (name in PROTECTED_CALLERS
            and isinstance(raw, dict)
            and raw.get("enabled") is False):
        errors.append(
            f"{where}: Tier-1 protected caller - 'enabled: false' is not "
            f"permitted (failure alerts can never be switched off)")
    if not _CALLER_NAME_RE.fullmatch(name):
        errors.append(f"{where}: caller name must be lowercase "
                      f"alphanumeric/underscore/dash/dot, got {name!r}")
    if not isinstance(raw, dict):
        errors.append(f"{where}: section must be a mapping")
        return None
    unknown = sorted(set(raw) - _CALLER_KEYS)
    if unknown:
        errors.append(f"{where}: unknown key(s) {unknown}")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        errors.append(f"{where}.enabled is required (boolean toggle)")

    def recips(key: str) -> tuple[str, ...]:
        val = raw.get(key, [])
        if not isinstance(val, list) or \
                not all(isinstance(a, str) for a in val):
            errors.append(f"{where}.{key} must be a list of strings")
            return ()
        return tuple(val)

    to, cc, bcc = recips("to"), recips("cc"), recips("bcc")
    if not to:
        errors.append(f"{where}.to must contain at least one recipient")
    if bcc and not server.allow_bcc:
        errors.append(
            f"{where}.bcc lists {len(bcc)} recipient(s) but the server "
            f"capability allow_bcc=false - BCC is disabled "
            f"deployment-wide; remove the bcc list or enable it at "
            f"server level")
    _check_addresses(to, f"{where}.to", errors, server.allowed_domains)
    _check_addresses(cc, f"{where}.cc", errors, server.allowed_domains)
    _check_addresses(bcc, f"{where}.bcc", errors, server.allowed_domains)

    use_html = raw.get("use_html", False)
    if not isinstance(use_html, bool):
        errors.append(f"{where}.use_html must be a boolean")
        use_html = False
    elif use_html and not server.allow_html:
        errors.append(
            f"{where}.use_html=true but server allow_html=false - "
            f"either enable HTML at server level or remove this")

    tmpl_name = raw.get("template")
    tmpl_path: Path | None = None
    placeholders: tuple[str, ...] = ()
    if not isinstance(tmpl_name, str) or not tmpl_name.strip():
        errors.append(f"{where}.template is required (file name under "
                      f"{server.templates_dir})")
    elif not server.templates_dir.is_dir():
        # templates_dir itself is faulty - already reported at server
        # level; do not pile a per-caller cascade on top of it
        pass
    else:
        candidate = server.templates_dir / tmpl_name
        try:
            text = candidate.read_text(encoding="utf-8")
            tmpl_path = candidate
            seen: list[str] = []
            for ph in _PLACEHOLDER_RE.findall(text):
                if ph not in seen:
                    seen.append(ph)
            placeholders = tuple(seen)
        except OSError as exc:
            errors.append(
                f"{where}.template {tmpl_name!r} missing/unreadable "
                f"under {server.templates_dir}: {exc}")

    data = raw.get("data", {})
    if not isinstance(data, dict):
        errors.append(f"{where}.data must be a mapping of scalars")
        data = {}
    else:
        for k, v in data.items():
            if k in RESERVED_TEMPLATE_KEYS:
                errors.append(
                    f"{where}.data: key {k!r} is RESERVED (stamped by "
                    f"the service at compose time); use a different "
                    f"name for domain values")
            if not isinstance(v, (str, int, float, bool)):
                errors.append(
                    f"{where}.data[{k!r}] must be a scalar, got "
                    f"{type(v).__name__}")
    atts = raw.get("attachments", [])
    if not isinstance(atts, list) or \
            not all(isinstance(a, str) for a in atts):
        errors.append(f"{where}.attachments must be a list of file paths")
        atts = []

    if errors:
        return None
    assert isinstance(enabled, bool)
    assert isinstance(tmpl_name, str)
    assert tmpl_path is not None
    return CallerConfig(
        name=name, enabled=enabled, to=to, cc=cc, bcc=bcc,
        template=tmpl_name, template_path=tmpl_path,
        placeholders=placeholders, use_html=use_html,
        data=dict(data), attachments=tuple(atts))


def load_email_config(path: Path | str | None = None) -> EmailServiceConfig:
    """Load and validate the email service config. Raises ConfigError
    listing EVERY fault found; returns only a fully valid config.

    Callers are validated even when the server section is faulty (the
    server carries inert placeholders in that case) so the single
    ConfigError describes the whole file, not just the first broken
    section.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    errors: list[str] = []
    raw = _read_yaml(cfg_path, errors)
    server = _placeholder_server()
    callers: dict[str, CallerConfig] = {}
    if isinstance(raw, dict):
        server = _load_server(raw.get("email"), errors)
        c_raw = raw.get("callers")
        if not isinstance(c_raw, dict):
            errors.append("'callers' section is missing or not a mapping")
        else:
            for name, section in c_raw.items():
                cc = _load_caller(str(name), section, server, errors)
                if cc is not None:
                    callers[name] = cc
    elif not errors:
        errors.append(f"{cfg_path}: top level must be a mapping")

    if errors:
        raise ConfigError(errors)
    log.info("email config validated: %d caller(s), mode=%s, "
             "dry_run=%s", len(callers), server.mode, server.dry_run)
    return EmailServiceConfig(server=server, callers=callers)


__all__ = [
    "ConfigError",
    "CallerConfig",
    "EmailServiceConfig",
    "ServerConfig",
    "RESERVED_TEMPLATE_KEYS",
    "load_email_config",
]
