# dpl/lib/email_service/composer.py
"""Message composition: MIME assembly, gates, dry-run outbox (stage 4c).

Responsibilities (and nothing else):
  - render the caller's template with reserved keys stamped at the
    compose-time clock tick (send_id, generated_at, caller)
  - build the MIME message through email.message.EmailMessage ONLY -
    no string-built headers anywhere
  - SUBJECT: comes from the merged data key 'subject' (a scalar like
    any other), then sanitized - every control char/newline replaced
    before it touches a header (header injection impossible by
    construction)
  - BCC: envelope-only. Addresses ride in envelope_recipients; the
    message headers carry To/Cc but never Bcc
  - ATTACHMENTS: existence, extension whitelist and total-size cap all
    enforced BEFORE any content is read and before any socket exists.
    One bad attachment fails the WHOLE send - no silent partial
    reports (agreed 'fail loudly' ruling)
  - DRY-RUN: terminal - writes the complete message as <send_id>.eml
    into the outbox dir (openable in any mail client), logs a masked
    summary, returns outcome=dry_run
  - LIVE: returns outcome=composed with the message attached; the
    transport stage (4d) finalizes to sent and records history

finished_at doctrine (ratified 2026-11-11): every TERMINAL outcome
stamped at resolution - dry_run successes and all compose failures
get finished_at=now_utc_z(); a live composed result keeps None
because it is not terminal yet (transport stamps it).

compose_email NEVER raises: every fault, including internal bugs,
becomes a failed SendResult (failure-isolation rule).
"""
from __future__ import annotations

import mimetypes
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from dpl.lib.email_service.config import CallerConfig, ServerConfig
from dpl.lib.email_service.result import (
    EC_BAD_ATTACHMENT,
    EC_BAD_SUBJECT,
    EC_BAD_TEMPLATE,
    EC_INTERNAL,
    EC_NONE,
    EC_OUTBOX,
    OUTCOME_COMPOSED,
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    SendResult,
)
from dpl.lib.email_service.template import (
    PLACEHOLDER_RE,
    TemplateError,
    render_template,
    stamp_reserved,
)
from dpl.lib.logger import get_run_logger
from dpl.lib.paths import get_project_root
from dpl.lib.timefmt import now_utc_z

log = get_run_logger("dpl.email.compose")

PROJECT_ROOT = get_project_root()
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class _Fault(Exception):
    """Internal only: carries an error_class + summary to the boundary."""

    def __init__(self, error_class: str, summary: str) -> None:
        super().__init__(summary)
        self.error_class = error_class
        self.summary = summary


def _new_send_id() -> str:
    return f"S-{uuid.uuid4().hex[:12]}"


def _sanitize_subject(value: object) -> str:
    """Plain-text-forever rule for subjects: control chars and newlines
    become spaces, runs collapse, ends trim. Injection impossible."""
    text = re.sub(_CONTROL_RE, " ", str(value))
    return re.sub(r" {2,}", " ", text).strip()


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _gather_attachments(caller: CallerConfig,
                        server: ServerConfig) -> list[Path]:
    """Validate every attachment BEFORE reading anything; one fault
    aborts the whole send (fail-loudly, no partial reports)."""
    paths: list[Path] = []
    total = 0
    cap_bytes = int(server.max_attach_mb * 1024 * 1024)
    for raw in caller.attachments:
        p = _resolve_path(raw)
        if not p.is_file():
            raise _Fault(
                EC_BAD_ATTACHMENT,
                f"attachment not found: {raw!r} (resolved {p})")
        ext = p.suffix.lower().lstrip(".")
        if ext not in server.attach_extensions:
            raise _Fault(
                EC_BAD_ATTACHMENT,
                f"attachment {p.name!r}: extension {ext!r} is not in "
                f"the whitelist {tuple(server.attach_extensions)}")
        size = p.stat().st_size
        total += size
        if total > cap_bytes:
            raise _Fault(
                EC_BAD_ATTACHMENT,
                f"attachments exceed the configured cap "
                f"{server.max_attach_mb} MB "
                f"({total} bytes with {p.name!r})")
        paths.append(p)
    return paths


def _build_message(server: ServerConfig, caller: CallerConfig,
                   subject: str, body: str,
                   attachments: list[Path]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = server.sender
    msg["To"] = ", ".join(caller.to)
    if caller.cc:
        msg["Cc"] = ", ".join(caller.cc)
    # NOTE: no Bcc header, ever - BCC rides in the envelope only.
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC), usegmt=True)
    msg["Auto-Submitted"] = "auto-generated"      # RFC 3834
    msg["X-dpl-Send-ID"] = "<filled by caller>"   # replaced below
    msg.set_content(body, subtype="html" if caller.use_html else "plain",
                    charset="utf-8")
    for p in attachments:
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, _, subtype = (ctype or "application/octet-stream") \
            .partition("/")
        with open(p, "rb") as fh:
            payload = fh.read()
        msg.add_attachment(payload, maintype=maintype,
                           subtype=subtype or "octet-stream",
                           filename=p.name)
    return msg


def _write_outbox(msg: EmailMessage, send_id: str,
                  outbox_dir: Path) -> None:
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
        dest = outbox_dir / f"{send_id}.eml"
        with open(dest, "wb") as fh:
            fh.write(msg.as_bytes())
        log.info("dry-run: message written to %s", dest)
    except OSError as exc:
        raise _Fault(EC_OUTBOX, f"cannot write dry-run .eml to "
                                f"{outbox_dir}: {exc}") from exc


def compose_email(server: ServerConfig, caller: CallerConfig,
                  data: Mapping[str, object] | None,
                  run_id: str = "-") -> SendResult:
    """Compose one email for one caller. Never raises.

    data merges OVER the caller config's static `data` (call-time wins).
    The merged mapping must carry a scalar 'subject'; all ${placeholders}
    the template declares must resolve. Returns OUTCOME_COMPOSED (live)
    or OUTCOME_DRY_RUN (+ .eml in the outbox), or OUTCOME_FAILED with
    a precise error_class. finished_at is stamped on every terminal
    outcome here; a live composed result carries finished_at=None.
    """
    send_id = _new_send_id()
    created_at = now_utc_z()
    recipient_count = (len(caller.to) + len(caller.cc) + len(caller.bcc))
    mode = "dry_run" if server.dry_run else "live"

    def fail(error_class: str, summary: str) -> SendResult:
        log.warning("compose failed (caller=%s, send_id=%s): %s",
                    caller.name, send_id, summary)
        return SendResult(
            send_id=send_id, caller=caller.name, created_at=created_at,
            mode=mode, outcome=OUTCOME_FAILED, error_class=error_class,
            attempts_used=0, recipient_count=recipient_count,
            subject="", error_summary=summary, run_id=run_id,
            message=None, envelope_recipients=(),
            finished_at=now_utc_z())      # compose failure IS terminal

    try:
        if not isinstance(data, Mapping):
            raise _Fault(EC_INTERNAL,
                         f"data must be a mapping, got "
                         f"{type(data).__name__}")
        merged: dict[str, object] = {**caller.data, **data}
        reserved = stamp_reserved(caller.name, send_id, created_at)

        template_text = caller.template_path.read_text(encoding="utf-8")
        body = render_template(template_text, merged, reserved)

        if "subject" not in merged:
            raise _Fault(EC_BAD_SUBJECT,
                         "merged data must carry a 'subject' value "
                         "(add it to the caller config 'data' or the "
                         "send call)")
        subject = _sanitize_subject(merged["subject"])
        if not subject:
            raise _Fault(EC_BAD_SUBJECT,
                         "subject is empty after sanitizing")

        attachments = _gather_attachments(caller, server)
        msg = _build_message(server, caller, subject, body, attachments)
        del msg["X-dpl-Send-ID"]           # placeholder replaced atomically
        msg["X-dpl-Send-ID"] = send_id
        envelope = tuple(caller.to) + tuple(caller.cc) + tuple(caller.bcc)

        if server.dry_run:
            _write_outbox(msg, send_id, server.outbox_dir)
            log.info("dry-run complete: caller=%s send_id=%s "
                     "recipients=%d subject=%r",
                     caller.name, send_id, recipient_count, subject)
            return SendResult(
                send_id=send_id, caller=caller.name, created_at=created_at,
                mode=mode, outcome=OUTCOME_DRY_RUN, error_class=EC_NONE,
                attempts_used=1, recipient_count=recipient_count,
                subject=subject, error_summary=None, run_id=run_id,
                message=None, envelope_recipients=envelope,
                finished_at=now_utc_z())   # dry-run IS terminal

        log.info("composed: caller=%s send_id=%s recipients=%d "
                 "attachments=%d", caller.name, send_id,
                 recipient_count, len(attachments))
        return SendResult(
            send_id=send_id, caller=caller.name, created_at=created_at,
            mode=mode, outcome=OUTCOME_COMPOSED, error_class=EC_NONE,
            attempts_used=0, recipient_count=recipient_count,
            subject=subject, error_summary=None, run_id=run_id,
            message=msg, envelope_recipients=envelope,
            finished_at=None)              # not terminal until transport

    except _Fault as fault:
        return fail(fault.error_class, fault.summary)
    except TemplateError as exc:
        detail = "; ".join(str(exc).split("; "))[:300]
        return fail(EC_BAD_TEMPLATE, detail)
    except OSError as exc:
        return fail(EC_BAD_TEMPLATE,
                    f"template unreadable at compose time: {exc}")
    except Exception as exc:  # broad by design: never-raises boundary
        log.exception("internal compose error (send_id=%s)", send_id)
        return fail(EC_INTERNAL, f"unexpected {type(exc).__name__}: {exc}")


def extract_placeholders(text: str) -> tuple[str, ...]:
    """Convenience for tests/tools: ordered unique placeholders in a
    template text (same grammar as config.py's loader)."""
    seen: list[str] = []
    for ph in PLACEHOLDER_RE.findall(text):
        if ph not in seen:
            seen.append(ph)
    return tuple(seen)


__all__ = ["compose_email", "extract_placeholders"]
