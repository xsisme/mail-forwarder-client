"""POP3, WeCom webhook, SMTP and staged diagnostics."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import html
import json
import poplib
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.request

from .config import Config, SECRET_KEYS, split_list
from .parsing import Mail, truncate_utf8


def silent_emit(kind, **data):
    pass


class TransportError(RuntimeError):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


class Cancelled(Exception):
    pass


def check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise Cancelled()


@dataclass
class DiagnosticResult:
    mailbox_ok: bool = False
    channels: dict[str, bool] = field(default_factory=dict)
    cancelled: bool = False

    def __bool__(self):
        return not self.cancelled and self.mailbox_ok and all(self.channels.values())

    @property
    def can_forward(self):
        return not self.cancelled and self.mailbox_ok and any(self.channels.values())


def safe_error(exc: Exception, cfg: Config) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        message = "服务器 TLS 证书校验失败；请检查域名、系统时间或导入公司 CA 证书。"
    elif isinstance(exc, socket.gaierror):
        message = "域名解析失败；请检查服务器地址、DNS 及公司 VPN。"
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        message = "连接超时；请检查 VPN、服务器端口及网络策略。"
    elif isinstance(exc, ConnectionRefusedError):
        message = "连接被拒绝；请检查端口及服务是否开启。"
    elif isinstance(exc, smtplib.SMTPAuthenticationError):
        message = "SMTP 登录失败；请检查账号、授权码 / 密码及 SMTP 服务权限。"
    elif isinstance(exc, urllib.error.HTTPError):
        message = f"转发接口 HTTP 错误：{exc.code}。"
    elif isinstance(exc, urllib.error.URLError):
        message = f"HTTPS 通道连接失败：{exc.reason}"
    else:
        message = str(exc)
    for key in SECRET_KEYS:
        secret = getattr(cfg, key)
        if secret:
            message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"(?i)([?&]key=)[^\s&\"'<>]+", r"\1[已隐藏]", message)
    return message.replace("\r", " ").replace("\n", " ")[:800]


def pop_context(cfg: Config) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cfg.pop_ca_file or None)
    if not cfg.pop_verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def close_pop(connection, immediate=False):
    if connection is None:
        return
    if immediate:
        try:
            connection.close()
        except Exception:
            pass
        return
    try:
        connection.quit()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass


@contextmanager
def pop_session(cfg: Config, emit=silent_emit, stop_event=None):
    connection = None
    stage = "network"
    try:
        check_cancel(stop_event)
        emit("status", key="network", level="busy", text="正在连接邮箱…")
        if cfg.pop_security == "SSL/TLS":
            connection = poplib.POP3_SSL(cfg.pop_host, cfg.pop_port, timeout=cfg.timeout_seconds,
                                          context=pop_context(cfg))
        else:
            connection = poplib.POP3(cfg.pop_host, cfg.pop_port, timeout=cfg.timeout_seconds)
            if cfg.pop_security == "STARTTLS":
                connection.stls(context=pop_context(cfg))
        emit("status", key="network", level="ok", text="邮箱连接正常")
        check_cancel(stop_event)
        stage = "login"
        emit("status", key="login", level="busy", text="正在验证账号…")
        try:
            connection.user(cfg.pop_user)
            check_cancel(stop_event)
            connection.pass_(cfg.pop_password)
        except poplib.error_proto as exc:
            raise TransportError("login", "POP3 登录被拒绝；请检查账号、密码及 POP3 权限。") from exc
        emit("status", key="login", level="ok", text="账号登录成功")
        stage = "mailbox"
    except (TransportError, Cancelled):
        raise
    except Exception as exc:
        raise TransportError(stage, safe_error(exc, cfg)) from exc
    finally:
        # Setup errors must release the socket even when login is rejected.
        if stage != "mailbox":
            close_pop(connection, bool(stop_event is not None and stop_event.is_set()))
    try:
        yield connection
    finally:
        close_pop(connection, bool(stop_event is not None and stop_event.is_set()))


def uidl_list(connection) -> list[tuple[int, str]]:
    _, lines, _ = connection.uidl()
    result = []
    for line in lines:
        pieces = line.decode("ascii").split(None, 1)
        if len(pieces) != 2 or not pieces[0].isdigit() or not pieces[1]:
            raise TransportError("mailbox", "服务器返回了无效 UIDL 列表，已暂停本轮以保护去重状态。")
        result.append((int(pieces[0]), pieces[1]))
    if len({uid for _, uid in result}) != len(result):
        raise TransportError("mailbox", "服务器返回重复 UIDL，无法可靠去重。")
    return sorted(result)


def wecom_payload(cfg: Config, mail: Mail) -> dict:
    attachment_names = [item.name for item in mail.attachments]
    attachment_names += [name + "（超限未附带）" for name in mail.omitted]
    attachments = "、".join(attachment_names)
    title = f"【验证码：{mail.code}】{mail.subject}" if mail.code else f"【新邮件】{mail.subject}"
    if cfg.wecom_type == "text":
        content = f"{title}\n发件人：{mail.sender}\n时间：{mail.date}\n"
        if attachments:
            content += f"附件：{attachments}\n"
        content += "----------------\n" + (mail.plain or "(正文为空)")
        mobiles = split_list(cfg.wecom_mobiles)
        body = {"content": truncate_utf8(content, 2000),
                "mentioned_mobile_list": [item for item in mobiles if item != "@all"]}
        members = cfg.wecom_mentioned_userids
        if "@all" in mobiles:
            members = [*members, "@all"]
        if members:
            body["mentioned_list"] = members
        return {"msgtype": "text", "text": body}
    banner = f'### 验证码：<font color="warning">{mail.code}</font>\n' if mail.code else "### 收到新邮件\n"
    content = (banner + f"> 主题：{html.escape(mail.subject)}\n"
               f"> 发件人：{html.escape(mail.sender)}\n> 时间：{html.escape(mail.date)}\n")
    if attachments:
        content += f"> 附件：{html.escape(attachments)}\n"
    content += "\n" + (mail.plain or "(正文为空)")
    members = cfg.wecom_mentioned_userids
    if members:
        # A plain @nickname is only text. Keep actual <@userid> tags on a
        # separate first line so long mail bodies cannot truncate the mentions.
        content = " ".join(f"<@{member}>" for member in members) + "\n\n" + content
    return {"msgtype": "markdown", "markdown": {"content": truncate_utf8(content, 4000)}}


def send_wecom(cfg: Config, mail: Mail) -> None:
    data = json.dumps(wecom_payload(cfg, mail), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(cfg.wecom_webhook, data=data,
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(request, timeout=cfg.timeout_seconds, context=ssl.create_default_context()) as response:
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("errcode") != 0:
        if isinstance(payload, dict):
            raise TransportError("wecom", f"企业微信返回错误 {payload.get('errcode')}：{payload.get('errmsg', '未知错误')}")
        raise TransportError("wecom", "企业微信返回了无效响应。")


def smtp_hello_name() -> str:
    """SMTP commands are ASCII even when the Windows computer name is Chinese."""
    try:
        hostname = socket.getfqdn().rstrip(".").encode("idna").decode("ascii")
        if "." in hostname and len(hostname) <= 253 and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in hostname.split(".")):
            return hostname
    except (OSError, UnicodeError):
        pass
    return "[127.0.0.1]"


def message_id_domain(address: str) -> str:
    # Do not use email.utils.make_msgid()'s implicit socket.getfqdn():
    # an unencoded non-ASCII hostname produces an invalid Message-ID header.
    try:
        domain = address.rpartition("@")[2].encode("idna").decode("ascii")
        if domain and re.fullmatch(r"[A-Za-z0-9.-]+", domain):
            return domain
    except UnicodeError:
        pass
    return "localhost"


@contextmanager
def smtp_session(cfg: Config):
    connection = None
    try:
        context = ssl.create_default_context()
        local_hostname = smtp_hello_name()
        if cfg.smtp_security == "SSL/TLS":
            connection = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                           timeout=cfg.timeout_seconds, context=context,
                                           local_hostname=local_hostname)
        else:
            connection = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds,
                                      local_hostname=local_hostname)
            connection.ehlo()
            if cfg.smtp_security == "STARTTLS":
                connection.starttls(context=context)
                connection.ehlo()
        connection.login(cfg.smtp_user, cfg.smtp_password)
        yield connection
    finally:
        if connection:
            try:
                connection.quit()
            except Exception:
                connection.close()


def build_forward(cfg: Config, mail: Mail) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = (f"【验证码:{mail.code}】" if mail.code else "【公司邮件】") + mail.subject
    message["From"] = cfg.smtp_user
    message["To"] = ", ".join(cfg.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=message_id_domain(cfg.smtp_user))
    banner = f"原发件人：{mail.sender}\n原时间：{mail.date}\n原主题：{mail.subject}\n"
    if mail.omitted:
        banner += "附件超限未转发：" + "、".join(mail.omitted) + "\n"
    message.set_content(banner + "\n" + (mail.plain or "(正文为空)"))
    rich_part = None
    if mail.html:
        top = '<div style="padding:12px;background:#eef2ff;font-family:sans-serif">'
        top += "<br>".join(html.escape(line) for line in banner.strip().splitlines()) + "</div><br>"
        message.add_alternative(top + mail.html, subtype="html")
        rich_part = message.get_payload()[-1]
    for item in mail.attachments:
        main, sub = (item.content_type.split("/", 1) + ["octet-stream"])[:2]
        if item.cid and rich_part is not None:
            rich_part.add_related(item.data, maintype=main, subtype=sub,
                                  cid=item.cid, filename=item.name, disposition="inline")
        else:
            message.add_attachment(item.data, maintype=main, subtype=sub, filename=item.name)
    return message


def send_smtp(cfg: Config, mail: Mail) -> None:
    message = build_forward(cfg, mail)
    if len(message.as_bytes()) > 25 * 1024 * 1024:
        raise TransportError("smtp", "转发邮件编码后超过 25 MB；请使用企业微信通知或缩小原邮件附件。")
    with smtp_session(cfg) as connection:
        # A non-empty return value means partial recipient rejection, not full delivery.
        rejected = connection.send_message(message, to_addrs=cfg.recipients)
        if rejected:
            raise TransportError("smtp", "部分收件地址被 SMTP 拒绝；已接受的地址可能收到邮件，请检查收件地址。")


def test_mail() -> Mail:
    return Mail(subject="邮件转发助手 · 通道测试", sender="邮件转发助手",
                date=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                plain="这是一条由客户端主动发送的测试通知。收到此消息表示当前转发通道可用。")


def diagnose(cfg: Config, emit=silent_emit, mailbox_only: bool = False, stop_event=None) -> DiagnosticResult:
    """Explicit UI action. Enabled routes receive real, non-sensitive test messages."""
    result = DiagnosticResult()
    if stop_event is not None and stop_event.is_set():
        result.cancelled = True
        return result
    emit("log", level="INFO", text="开始检测公司邮箱连接及登录。")
    try:
        emit("status", key="network", level="busy", text="正在检查 DNS / TCP…")
        try:
            socket.getaddrinfo(cfg.pop_host, cfg.pop_port, type=socket.SOCK_STREAM)
            with socket.create_connection((cfg.pop_host, cfg.pop_port), timeout=cfg.timeout_seconds):
                pass
        except Exception as exc:
            raise TransportError("network", safe_error(exc, cfg)) from exc
        emit("status", key="network", level="ok", text="DNS / TCP 可达")
        check_cancel(stop_event)
        with pop_session(cfg, emit, stop_event) as connection:
            count, _ = connection.stat()
            check_cancel(stop_event)
            uids = uidl_list(connection)
            emit("status", key="mailbox", level="ok", text=f"{count} 封邮件 · UIDL 正常")
            if len(uids) != count:
                emit("log", level="WARNING", text="服务器 STAT 与 UIDL 数量不一致，转发以 UIDL 列表为准。")
        emit("log", level="INFO", text="公司邮箱登录及邮件列表读取成功。")
        result.mailbox_ok = True
    except Cancelled:
        result.cancelled = True
        emit("log", level="INFO", text="检测已取消。")
        return result
    except Exception as exc:
        key = exc.stage if isinstance(exc, TransportError) else "mailbox"
        emit("status", key=key, level="error", text=safe_error(exc, cfg))
        emit("log", level="ERROR", text="邮箱检测失败：" + safe_error(exc, cfg))
    if mailbox_only:
        return result
    for channel, enabled, sender in (("wecom", cfg.wecom_enabled, send_wecom),
                                      ("smtp", cfg.smtp_enabled, send_smtp)):
        if not enabled:
            emit("status", key=channel, level="off", text="未启用")
            continue
        if stop_event is not None and stop_event.is_set():
            result.cancelled = True
            return result
        result.channels[channel] = False
        emit("status", key=channel, level="busy", text="正在发送测试通知…")
        try:
            sender(cfg, test_mail())
            result.channels[channel] = True
            text = "接口已接受测试消息" if channel == "wecom" else "SMTP 已接受测试邮件"
            emit("status", key=channel, level="ok", text=text)
            emit("log", level="INFO", text=text + "；请在对应通道确认收到。")
        except Exception as exc:
            text = safe_error(exc, cfg)
            emit("status", key=channel, level="error", text=text)
            emit("log", level="ERROR", text=f"{'企业微信' if channel == 'wecom' else 'SMTP'}通道检测失败：{text}")
    return result
