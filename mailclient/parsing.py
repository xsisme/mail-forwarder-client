"""MIME parsing and conservative numeric verification-code extraction."""
from __future__ import annotations

from dataclasses import dataclass, field
import email
from email import policy
from email.header import decode_header
import html
import re


@dataclass
class Attachment:
    name: str
    data: bytes
    content_type: str
    cid: str = ""


@dataclass
class Mail:
    subject: str
    sender: str = ""
    date: str = ""
    plain: str = ""
    html: str = ""
    code: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)


def decode_header_text(value) -> str:
    if not value:
        return ""
    result = []
    try:
        parts = decode_header(str(value))
    except Exception:
        return str(value)
    for part, encoding in parts:
        if isinstance(part, bytes):
            try:
                part = part.decode(encoding or "utf-8", errors="replace")
            except LookupError:
                part = part.decode("gb18030", errors="replace")
        result.append(str(part))
    return "".join(result).replace("\r", " ").replace("\n", " ").strip()


def html_to_text(value: str) -> str:
    value = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<br\s*/?>|</(?:p|div|tr|li|h[1-6])>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in html.unescape(value).splitlines()]
    return "\n".join(line for line in lines if line)


CODE_WORD = r"(?:验证码|校验码|动态码|安全码|口令|\bverification(?:\s+code)?\b|\bpasscode\b|\bOTP\b|\bcode\b)"


def extract_code(subject: str, body: str) -> str | None:
    text = subject + "\n" + body
    # Only use short keyword neighbourhoods. No unconstrained four/six-digit fallback.
    patterns = [rf"{CODE_WORD}[^\d\r\n]{{0,40}}(?<![A-Za-z0-9])(\d{{4,8}})(?![A-Za-z0-9])",
                rf"(?<![A-Za-z0-9])(\d{{4,8}})(?![A-Za-z0-9])[^\d\r\n]{{0,24}}{CODE_WORD}"]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return None


def parse_mail(raw: bytes) -> Mail:
    message = email.message_from_bytes(raw, policy=policy.default)
    mail = Mail(subject=decode_header_text(message.get("Subject")) or "(无主题)",
                sender=decode_header_text(message.get("From")), date=decode_header_text(message.get("Date")))
    plain, rich = [], []
    attachment_total = 0

    def visit(part):
        nonlocal attachment_total
        disposition = part.get_content_disposition()
        name = part.get_filename()
        if name or disposition == "attachment" or (part.get("Content-ID") and part.get_content_maintype() != "text"):
            name = decode_header_text(name) or "attachment.bin"
            data = part.get_payload(decode=True)
            if data is None and part.get_content_type() == "message/rfc822":
                data = b"\r\n".join(p.as_bytes(policy=policy.SMTP) for p in part.get_payload())
            if data is None:
                return
            if len(data) > 10 * 1024 * 1024 or attachment_total + len(data) > 16 * 1024 * 1024:
                mail.omitted.append(name)
                return
            attachment_total += len(data)
            mail.attachments.append(Attachment(name, data, part.get_content_type(), str(part.get("Content-ID") or "")))
            return
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child)
            return
        if part.get_content_type() not in ("text/plain", "text/html"):
            return
        data = part.get_payload(decode=True) or b""
        try:
            value = data.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            value = data.decode("gb18030", errors="replace")
        (rich if part.get_content_type() == "text/html" else plain).append(value)

    visit(message)
    mail.html = "\n".join(rich).strip()
    mail.plain = "\n".join(plain).strip() or html_to_text(mail.html)
    mail.code = extract_code(mail.subject, mail.plain)
    return mail


def matches_keywords(mail: Mail, keywords: list[str]) -> bool:
    text = (mail.subject + "\n" + mail.plain).casefold()
    return not keywords or any(word.casefold() in text for word in keywords)


def truncate_utf8(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    suffix = "\n…（内容过长，已截断）".encode("utf-8")
    if len(suffix) > limit:
        return raw[:limit].decode("utf-8", errors="ignore")
    return raw[:limit - len(suffix)].decode("utf-8", errors="ignore") + suffix.decode("utf-8")
