from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape as _escape_html
import re
import smtplib
import ssl
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..config import Settings, get_settings
from ..repositories import (
    EmailDeliveryRecord,
    EmailDeliverySource,
    RepositoryProtocol,
    ReportStorage,
)


MAX_SMTP_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0)
_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.*?))?\s*$")
_HORIZONTAL_RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_FENCE_MARKER = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`~]*)$")
_BLOCKQUOTE_LINE = re.compile(r"^ {0,3}>\s?(.*)$")
_UNORDERED_ITEM = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_ORDERED_ITEM = re.compile(r"^ {0,3}(\d{1,9})[.)]\s+(.*)$")
_HEADING_UNDERLINE = re.compile(r"^ {0,3}=+\s*$")
_RULE_LINE = re.compile(r"^ {0,3}-{2,}\s*$")
_DIVIDER_CELL = re.compile(r"^:?-+:?$")
_BARE_URL = re.compile(r"https?://[^\s<>()\[\]\"'*_`]+")
_LINK_TAIL = re.compile(r"[.,;:!?\u3001\u3002\uff01\uff0c\uff1a\uff1b\uff1f\u201d\u2019]+$")
_INLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("code", re.compile(r"`([^`\n]+)`")),
    ("mdlink", re.compile(r"!?\[(?P<label>[^\]]*)\]\((?P<href>[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)")),
    ("angle_link", re.compile(r"<(?P<href>https?://[^>\s]+)>")),
    ("strong", re.compile(r"\*\*(?P<inner>[^*\n]+)\*\*")),
    ("em", re.compile(r"\*(?P<inner>[^*\n]+)\*")),
    ("strike", re.compile(r"~~(?P<inner>[^~\n]+)~~")),
    ("bare_link", _BARE_URL),
)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_GROUPED_INLINE_KINDS = frozenset({"mdlink", "angle_link", "strong", "em", "strike"})


def _closing_parenthesis(text: str, start: int, end: int) -> int | None:
    """Return the index just past the `)` that closes the group opened at `start`.

    Quoted spans are skipped so a quote inside a malformed target cannot make a
    later `)` look like the terminator.
    """
    depth = 0
    position = start
    while position < end:
        character = text[position]
        if character in {'"', "'"}:
            closing_quote = text.find(character, position + 1, end)
            position = end if closing_quote == -1 else closing_quote + 1
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return None

_INLINE_STYLES = {
    "body": "margin:0;padding:0;background:#f5f7f5;",
    "card": (
        "max-width:640px;margin:0 auto;padding:28px 30px;"
        "background:#ffffff;border:1px solid #dce3df;border-radius:8px;"
    ),
    "eyebrow": (
        "margin:0 0 6px;color:#236b51;font-size:11px;font-weight:700;"
        "letter-spacing:.08em;text-transform:uppercase;"
    ),
    "title": "margin:0;color:#202a27;font-size:22px;line-height:1.35;",
    "date": "margin:8px 0 0;color:#6b7772;font-size:12px;",
    "divider": "height:1px;margin:20px 0;border:0;background:#dce3df;",
    "content": "color:#34413a;font-size:15px;line-height:1.75;",
    "h2": "margin:26px 0 10px;color:#202a27;font-size:18px;line-height:1.4;",
    "h3": "margin:22px 0 9px;color:#202a27;font-size:16px;line-height:1.4;",
    "h4": "margin:18px 0 8px;color:#202a27;font-size:14px;line-height:1.4;",
    "h5": "margin:16px 0 8px;color:#4d5a54;font-size:13px;",
    "h6": "margin:16px 0 8px;color:#4d5a54;font-size:13px;",
    "heading1": "margin:0 0 14px;color:#202a27;font-size:23px;line-height:1.35;",
    "paragraph": "margin:0 0 13px;",
    "list": "margin:0 0 14px;padding-left:22px;",
    "list_item": "margin:5px 0;",
    "quote": (
        "margin:0 0 14px;padding:10px 14px;border-left:3px solid #b7d6c4;"
        "background:#f4f9f5;color:#40544a;"
    ),
    "code_block": (
        "margin:0 0 14px;padding:12px 14px;background:#f4f8f5;border:1px solid #dce3df;"
        "border-radius:5px;color:#2c3a33;font-family:Consolas,'Courier New',monospace;"
        "font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;"
    ),
    "code_label": (
        "display:inline-block;margin-bottom:8px;padding:1px 7px;border-radius:3px;"
        "background:#e4f2e9;color:#35614d;font-size:10px;font-weight:700;letter-spacing:.04em;"
        "text-transform:uppercase;"
    ),
    "code_inline": (
        "padding:1px 5px;border-radius:3px;background:#f1f5f2;color:#2c4c3d;"
        "font-family:Consolas,'Courier New',monospace;font-size:13px;"
    ),
    "link": "color:#236b51;text-decoration:underline;",
    "strong": "color:#22302a;",
    "del": "color:#7d8983;",
    "table": "width:100%;margin:0 0 15px;border-collapse:collapse;font-size:13px;",
    "table_head": "padding:8px 10px;border:1px solid #dce3df;background:#e4f2e9;color:#274a3a;",
    "table_cell": "padding:8px 10px;border:1px solid #dce3df;",
    "footer": "max-width:640px;margin:0 auto;padding:16px 4px 0;color:#818b85;font-size:11px;",
}
_CONTENT_MAX_NODES = 4000


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    delivery: EmailDeliveryRecord
    duplicate: bool


class EmailServiceError(RuntimeError):
    """Base class for errors that prevent preparing a report email."""


class ReportNotFoundError(EmailServiceError):
    pass


class ReportContentUnavailableError(EmailServiceError):
    pass


class _SMTPFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class EmailService:
    """Send persisted reports and keep an idempotent delivery audit trail."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        report_storage: ReportStorage,
        *,
        settings: Settings | None = None,
        smtp_factory: Callable[..., Any] | None = None,
        smtp_ssl_factory: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.repository = repository
        self.report_storage = report_storage
        self.settings = settings or get_settings()
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.smtp_ssl_factory = smtp_ssl_factory or (
            smtp_factory if smtp_factory is not None else smtplib.SMTP_SSL
        )
        self.sleeper = sleeper or time.sleep
        self.timeout = timeout

    def send_report(
        self,
        report_id: int | None = None,
        *,
        resend: bool = False,
        trigger_source: EmailDeliverySource = "manual_api",
        idempotency_key: str | None = None,
        max_smtp_attempts: int | None = None,
        retry_existing: bool = False,
    ) -> EmailSendResult:
        if report_id is None:
            reports = self.repository.list_reports(limit=1)
            if not reports:
                raise ReportNotFoundError("没有已完成的日报")
            report = reports[0]
        else:
            report = self.repository.get_report(report_id)
            if report is None:
                raise ReportNotFoundError("日报不存在")

        try:
            markdown = self.report_storage.read_report(report.file_path)
        except (OSError, UnicodeError, ValueError):
            raise ReportContentUnavailableError("日报正文不可用，未发送邮件") from None

        recipient = self.repository.get_preferences().email.strip()
        subject = _email_subject(report.title, report.report_date.isoformat())
        body = build_report_email_body(
            title=report.title,
            report_date=report.report_date.isoformat(),
            markdown=markdown,
        )
        html_body = build_report_email_html(
            title=report.title,
            report_date=report.report_date.isoformat(),
            markdown=markdown,
        )
        key = idempotency_key or _delivery_key(report.id, recipient, resend)
        if resend and idempotency_key is None:
            key = f"{key}:{uuid4()}"

        delivery, created = self.repository.reserve_email_delivery(
            report.id,
            recipient,
            subject,
            trigger_source,
            key,
        )
        if not created:
            if delivery.status == "sent" or not retry_existing:
                return EmailSendResult(delivery=delivery, duplicate=True)

        if not recipient:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="recipient_not_configured",
                error_message="请先在设置中保存有效的收件邮箱，再明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)
        try:
            _validate_email_address(recipient)
        except ValueError:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="invalid_recipient",
                error_message="设置中的收件邮箱无效，请修正后明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)

        try:
            host, port, username, password, sender = self.settings.require_smtp()
            _validate_email_address(sender)
        except ValueError:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="smtp_not_configured",
                error_message="SMTP 配置不完整或发件地址无效，请配置后明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)

        message = _build_message(sender, recipient, subject, body, html_body)
        if max_smtp_attempts is None:
            max_smtp_attempts = MAX_SMTP_ATTEMPTS
        if isinstance(max_smtp_attempts, bool) or not isinstance(max_smtp_attempts, int) or not 1 <= max_smtp_attempts <= MAX_SMTP_ATTEMPTS:
            raise ValueError("max_smtp_attempts must be between 1 and 3")
        prior_attempts = self.repository.list_email_delivery_attempts(delivery.id)
        first_attempt = (prior_attempts[-1].attempt_number + 1) if prior_attempts else 1
        for attempt_number in range(first_attempt, first_attempt + max_smtp_attempts):
            try:
                self._send_smtp_message(
                    host,
                    port,
                    username,
                    password.get_secret_value(),
                    message,
                    recipient,
                )
            except _SMTPFailure as failure:
                self.repository.record_email_delivery_attempt(
                    delivery.id,
                    attempt_number,
                    "failed",
                    failure.code,
                    failure.message,
                )
                should_retry = failure.retryable and attempt_number < first_attempt + max_smtp_attempts - 1
                delivery = self.repository.update_email_delivery(
                    delivery.id,
                    "pending" if should_retry else "failed",
                    error_code=failure.code,
                    error_message=failure.message,
                    retry_count=attempt_number - 1,
                )
                if should_retry:
                    self.sleeper(_RETRY_DELAYS[min(attempt_number - 1, len(_RETRY_DELAYS) - 1)])
                    continue
                return EmailSendResult(delivery=delivery, duplicate=False)
            else:
                self.repository.record_email_delivery_attempt(
                    delivery.id, attempt_number, "sent"
                )
                delivery = self.repository.update_email_delivery(
                    delivery.id,
                    "sent",
                    retry_count=attempt_number - 1,
                )
                return EmailSendResult(delivery=delivery, duplicate=False)

        raise AssertionError("SMTP retry loop ended without a result")

    def _send_smtp_message(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        message: EmailMessage,
        recipient: str,
    ) -> None:
        client = None
        try:
            if self.settings.smtp_use_ssl:
                client = self.smtp_ssl_factory(
                    host,
                    port,
                    timeout=self.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                client = self.smtp_factory(host, port, timeout=self.timeout)
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
            client.ehlo()
            client.login(username, password)
            refused = client.send_message(message, to_addrs=[recipient])
            if refused:
                code = next(iter(refused.values()))[0]
                raise _classify_recipient_refusal(code)
        except _SMTPFailure:
            raise
        except smtplib.SMTPAuthenticationError:
            raise _SMTPFailure(
                "smtp_authentication_failed",
                "SMTP 认证失败，请检查账号和授权码。",
                retryable=False,
            ) from None
        except smtplib.SMTPRecipientsRefused as error:
            code = next(iter(error.recipients.values()))[0] if error.recipients else 550
            raise _classify_recipient_refusal(code) from None
        except smtplib.SMTPResponseException as error:
            raise _classify_response_error(error.smtp_code) from None
        except (OSError, TimeoutError, smtplib.SMTPServerDisconnected):
            raise _SMTPFailure(
                "temporary_network_error",
                "SMTP 网络暂时不可用，系统已按策略重试。",
                retryable=True,
            ) from None
        except smtplib.SMTPException:
            raise _SMTPFailure(
                "smtp_error",
                "SMTP 服务拒绝了邮件请求。",
                retryable=False,
            ) from None
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass


def build_report_email_body(*, title: str, report_date: str, markdown: str) -> str:
    """Render an email exclusively from a completed, persisted report."""
    return f"{title}\n日期：{report_date}\n\n{markdown.rstrip()}\n"


def build_report_email_html(*, title: str, report_date: str, markdown: str) -> str:
    """Render the HTML alternative of a report email.

    The whole document is built from escaped text, so stored report content can
    never inject markup into the message.
    """
    return (
        "<!doctype html><html lang=\"zh-CN\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_escape_html(title)}</title>"
        "</head>"
        f"<body style=\"{_INLINE_STYLES['body']}\">"
        f"<div style=\"{_INLINE_STYLES['card']}\">"
        f"<p style=\"{_INLINE_STYLES['eyebrow']}\">AgentNews 日报</p>"
        f"<h1 style=\"{_INLINE_STYLES['title']}\">{_escape_html(title)}</h1>"
        f"<p style=\"{_INLINE_STYLES['date']}\">{_escape_html(report_date)}</p>"
        f"<hr style=\"{_INLINE_STYLES['divider']}\">"
        f"<div style=\"{_INLINE_STYLES['content']}\">"
        f"{markdown_to_email_html(markdown)}"
        "</div>"
        "</div>"
        f"<div style=\"{_INLINE_STYLES['footer']}\">本邮件由 AgentNews 依据已保存日报自动生成，仅发送给已配置的收件邮箱。</div>"
        "</body></html>"
    )


def markdown_to_email_html(markdown: str) -> str:
    """Convert stored report Markdown into self-contained HTML.

    Only the syntax the digest prompt actually emits is rendered; anything else
    stays escaped plain text.
    """
    source = markdown if isinstance(markdown, str) else ""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rendered = _RenderedHtml()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "":
            index += 1
            continue

        fence = _match_fence(line)
        if fence is not None:
            body: list[str] = []
            index += 1
            while index < len(lines):
                closing = _match_fence(lines[index])
                if (
                    closing is not None
                    and closing[0] == fence[0]
                    and len(closing[0]) >= len(fence[0])
                    and closing[1] == ""
                ):
                    break
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            rendered.add(_code_block_html(fence[1], "\n".join(body)))
            continue

        if _HORIZONTAL_RULE.match(line) and index == 0:
            rendered.add(f"<hr style=\"{_INLINE_STYLES['divider']}\">")
            index += 1
            continue

        heading = _ATX_HEADING.match(line)
        if heading is not None:
            rendered.add(_heading_html(len(heading.group(1)), (heading.group(2) or "").strip()))
            index += 1
            continue

        if _HORIZONTAL_RULE.match(line):
            rendered.add(f"<hr style=\"{_INLINE_STYLES['divider']}\">")
            index += 1
            continue

        if _BLOCKQUOTE_LINE.match(line):
            quoted: list[str] = []
            while index < len(lines):
                match = _BLOCKQUOTE_LINE.match(lines[index])
                if match is None:
                    break
                quoted.append(match.group(1))
                index += 1
            rendered.add(f"<blockquote style=\"{_INLINE_STYLES['quote']}\">")
            rendered.add_inline(markdown_to_email_html("\n".join(quoted)))
            rendered.add("</blockquote>")
            continue

        if index + 1 < len(lines) and _is_table_row(line) and _is_table_divider(lines[index + 1]):
            cursor = index + 2
            rows: list[list[str]] = []
            while cursor < len(lines) and lines[cursor].strip() and _is_table_row(lines[cursor]):
                rows.append(_split_cells(lines[cursor]))
                cursor += 1
            rendered.add(_table_html(_split_cells(line), _table_alignments(lines[index + 1]), rows))
            index = cursor
            continue

        bullet = _UNORDERED_ITEM.match(line)
        ordered = _ORDERED_ITEM.match(line)
        if bullet is not None or ordered is not None:
            is_ordered = ordered is not None
            markers: list[str] = []
            while index < len(lines):
                match = _ORDERED_ITEM.match(lines[index]) if is_ordered else _UNORDERED_ITEM.match(lines[index])
                if match is None:
                    break
                markers.append(match.group(2) if is_ordered else match.group(1))
                index += 1
            rendered.add(_list_html(is_ordered, markers))
            continue

        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip():
            current = lines[index]
            if _HEADING_UNDERLINE.match(current) and len(paragraph) == 1:
                rendered.add(_heading_html(1, paragraph[0].strip()))
                paragraph = []
                index += 1
                break
            if _RULE_LINE.match(current):
                break
            if (
                paragraph
                and index + 1 < len(lines)
                and _is_table_row(current)
                and _is_table_divider(lines[index + 1])
            ):
                break
            if paragraph and (
                _UNORDERED_ITEM.match(current)
                or _ORDERED_ITEM.match(current)
                or _ATX_HEADING.match(current)
                or _BLOCKQUOTE_LINE.match(current)
                or _match_fence(current)
            ):
                break
            paragraph.append(current)
            index += 1
        if paragraph:
            text = "\n".join(item.strip() for item in paragraph)
            rendered.add(f"<p style=\"{_INLINE_STYLES['paragraph']}\">{_inline_html(text)}</p>")

    return rendered.html()


@dataclass(slots=True)
class _RenderedHtml:
    parts: list[str]
    count: int = 0

    def __init__(self) -> None:
        self.parts = []
        self.count = 0

    def add(self, value: str) -> None:
        self.parts.append(value)
        self.count += 1

    def add_inline(self, value: str) -> None:
        self.parts.append(value)

    def html(self) -> str:
        if self.count > _CONTENT_MAX_NODES:
            return f"<pre style=\"{_INLINE_STYLES['code_block']}\">" + _escape_html("".join(self.parts)) + "</pre>"
        return "".join(self.parts)


def _match_fence(line: str) -> tuple[str, str] | None:
    match = _FENCE_MARKER.match(line)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _is_table_divider(line: str) -> bool:
    if not line.strip() or "-" not in line:
        return False
    cells = _split_cells(line)
    if not cells:
        return False
    return all(_DIVIDER_CELL.match(cell) is not None for cell in cells)


def _split_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _table_alignments(divider: str) -> list[str]:
    alignments = []
    for cell in _split_cells(divider):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            alignments.append("center")
        elif right:
            alignments.append("right")
        else:
            alignments.append("left")
    return alignments


def _alignment_style(alignment: str) -> str:
    return f"text-align:{alignment};" if alignment in {"center", "right"} else ""


def _safe_href(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith("<") and candidate.endswith(">"):
        # CommonMark allows `[label](<url>)`; stored reports used that form.
        candidate = candidate[1:-1].strip()
    if not candidate or any(character in candidate for character in ("\n", "\r", " ", '"', "'", "<", ">")):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return candidate


def _next_inline(text: str) -> tuple[str, int, int, dict[str, str]] | None:
    """Return the next inline construct as (kind, start, end, groups).

    The earliest match wins; ties are broken by the longest construct. Inline
    code is handled first so its content is never styled as Markdown.
    """
    code = _CODE_SPAN.search(text)
    best_start: int | None = None
    best_end = 0
    best_kind: str | None = None
    best_groups: dict[str, str] = {}
    for kind, pattern in _INLINE_PATTERNS:
        if kind == "code":
            continue
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groupdict() if kind in _GROUPED_INLINE_KINDS else {}
        groups = {name: value for name, value in groups.items() if value is not None}
        if kind == "mdlink":
            # [label](url) may itself contain balanced parentheses, so walk to
            # the `)` that matches the one opening the target.
            opening = match.start() + match.group(0).rfind("](") + 1
            closing = _closing_parenthesis(text, opening, len(text))
            if closing is None:
                continue
            groups["href"] = text[opening + 1 : closing - 1]
            start, end = match.start(), closing
        else:
            start, end = match.start(), match.end()
        if best_start is None or start < best_start or (start == best_start and end > best_end):
            best_start, best_end, best_kind, best_groups = start, end, kind, groups
    if code is not None and (best_start is None or code.start() < best_start):
        return "code", code.start(), code.end(), {"inner": code.group(1)}
    if best_start is None or best_kind is None:
        return None
    return best_kind, best_start, best_end, best_groups


def _trim_url_tail(candidate: str) -> str:
    value = candidate
    previous = None
    while previous != value:
        previous = value
        value = _LINK_TAIL.sub("", value)
        if value.endswith(")"):
            if value.count(")") > value.count("("):
                value = value[:-1]
            else:
                break
        if value.endswith("]"):
            value = value[:-1]
    return value


def _inline_html(text: str) -> str:
    output: list[str] = []
    remaining = text
    while remaining:
        found = _next_inline(remaining)
        if found is None:
            output.append(_escape_html(remaining))
            break
        kind, start, end, groups = found
        output.append(_escape_html(remaining[:start]))
        if kind == "code":
            output.append(
                f"<code style=\"{_INLINE_STYLES['code_inline']}\">{_escape_html(groups['inner'])}</code>"
            )
        elif kind == "strong":
            output.append(f"<strong style=\"{_INLINE_STYLES['strong']}\">{_inline_html(groups['inner'])}</strong>")
        elif kind == "em":
            output.append(f"<em>{_inline_html(groups['inner'])}</em>")
        elif kind == "strike":
            output.append(f"<del style=\"{_INLINE_STYLES['del']}\">{_inline_html(groups['inner'])}</del>")
        elif kind in {"mdlink", "angle_link"}:
            # A rejected target keeps only its label: the raw URL never lands in
            # the message, so no dangerous scheme is even visible as text.
            label = groups.get("label") or groups["href"]
            output.append(_link_html(_safe_href(groups["href"]), _inline_html(label)))
        elif kind == "bare_link":
            candidate = _trim_url_tail(remaining[start:end])
            output.append(_link_html(_safe_href(candidate), _escape_html(candidate)))
        remaining = remaining[end:]
    return "".join(output)


def _link_html(href: str | None, label: str) -> str:
    if href is None:
        return label
    return (
        f"<a href=\"{_escape_html(href, quote=True)}\" style=\"{_INLINE_STYLES['link']}\" "
        f"target=\"_blank\" rel=\"noopener noreferrer\">{label}</a>"
    )


def _heading_html(level: int, text: str) -> str:
    tag = f"h{min(max(level, 1), 6)}"
    style = _INLINE_STYLES["heading1"] if tag == "h1" else _INLINE_STYLES.get(tag, _INLINE_STYLES["h4"])
    return f"<{tag} style=\"{style}\">{_inline_html(text)}</{tag}>"


def _list_html(ordered: bool, items: list[str]) -> str:
    tag = "ol" if ordered else "ul"
    parts = [f"<{tag} style=\"{_INLINE_STYLES['list']}\">"]
    for item in items:
        parts.append(f"<li style=\"{_INLINE_STYLES['list_item']}\">{_inline_html(item)}</li>")
    parts.append(f"</{tag}>")
    return "".join(parts)


def _code_block_html(language: str, code: str) -> str:
    label = (
        f"<span style=\"{_INLINE_STYLES['code_label']}\">{_escape_html(language[:20])}</span>"
        if language
        else ""
    )
    return f"<pre style=\"{_INLINE_STYLES['code_block']}\">{label}{_escape_html(code)}</pre>"


def _table_html(header: list[str], alignments: list[str], rows: list[list[str]]) -> str:
    parts = [f"<table style=\"{_INLINE_STYLES['table']}\"><thead><tr>"]
    for index, cell in enumerate(header):
        alignment = alignments[index] if index < len(alignments) else "left"
        parts.append(
            f"<th style=\"{_INLINE_STYLES['table_head']}{_alignment_style(alignment)}\">"
            f"{_inline_html(cell)}</th>"
        )
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for index in range(len(header)):
            cell = row[index] if index < len(row) else ""
            alignment = alignments[index] if index < len(alignments) else "left"
            parts.append(
                f"<td style=\"{_INLINE_STYLES['table_cell']}{_alignment_style(alignment)}\">"
                f"{_inline_html(cell)}</td>"
            )
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def classify_smtp_error(error: BaseException) -> tuple[str, str, bool]:
    """Return a stable, secret-free error code, message and retry policy."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            "smtp_authentication_failed",
            "SMTP 认证失败，请检查账号和授权码。",
            False,
        )
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        failure = _classify_recipient_refusal(
            next(iter(error.recipients.values()))[0] if error.recipients else 550
        )
        return failure.code, failure.message, failure.retryable
    if isinstance(error, smtplib.SMTPResponseException):
        failure = _classify_response_error(error.smtp_code)
        return failure.code, failure.message, failure.retryable
    if isinstance(error, (OSError, TimeoutError, smtplib.SMTPServerDisconnected)):
        return (
            "temporary_network_error",
            "SMTP 网络暂时不可用，系统已按策略重试。",
            True,
        )
    return "smtp_error", "SMTP 服务拒绝了邮件请求。", False


def _classify_recipient_refusal(code: int) -> _SMTPFailure:
    if 400 <= code < 500:
        return _SMTPFailure(
            "temporary_network_error",
            "SMTP 暂时拒收邮件，系统已按策略重试。",
            retryable=True,
        )
    return _SMTPFailure(
        "invalid_recipient",
        "SMTP 服务拒绝了收件地址，请核对后明确重发。",
        retryable=False,
    )


def _classify_response_error(code: int) -> _SMTPFailure:
    if 400 <= code < 500:
        return _SMTPFailure(
            "temporary_network_error",
            "SMTP 暂时不可用，系统已按策略重试。",
            retryable=True,
        )
    return _SMTPFailure(
        "smtp_rejected",
        "SMTP 服务拒绝了邮件请求。",
        retryable=False,
    )


def _validate_email_address(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 254
        or value.count("@") != 1
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("invalid email address")
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or ".." in local
        or ".." in domain
        or not _EMAIL_PATTERN.fullmatch(value)
    ):
        raise ValueError("invalid email address")


def _email_subject(title: str, report_date: str) -> str:
    clean_title = " ".join(title.split())[:180]
    return f"AgentNews 日报 | {report_date} | {clean_title}"


def _delivery_key(report_id: int, recipient: str, resend: bool) -> str:
    normalized_recipient = recipient.strip().casefold()
    suffix = f"resend:{uuid4()}" if resend else "default"
    return f"report:{report_id}:recipient:{normalized_recipient}:{suffix}"


def _build_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message
