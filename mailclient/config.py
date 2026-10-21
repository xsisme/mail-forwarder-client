"""Configuration, per-user Windows DPAPI credentials and instance locking."""
from __future__ import annotations

import base64
import ctypes
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


def data_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    path = base / "MailForwarderClient"
    path.mkdir(parents=True, exist_ok=True)
    return path


def split_list(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in re.split(r"[,，;；\r\n]+", value) if x.strip()))


@dataclass
class Config:
    pop_host: str = ""
    pop_port: int = 995
    pop_security: str = "SSL/TLS"
    pop_user: str = ""
    pop_password: str = ""
    pop_verify_tls: bool = True
    pop_ca_file: str = ""
    wecom_enabled: bool = True
    wecom_webhook: str = ""
    wecom_type: str = "markdown"
    wecom_userids: str = ""
    wecom_mobiles: str = ""
    smtp_enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_security: str = "SSL/TLS"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""
    poll_seconds: int = 15
    timeout_seconds: int = 20
    test_latest_first_run: bool = True
    keywords: str = "验证码,校验码,动态码,口令,code,verification,OTP"
    excluded_senders: str = ""
    remember_credentials: bool = sys.platform == "win32"

    @property
    def account_id(self) -> str:
        value = f"{self.pop_host.lower()}:{self.pop_port}|{self.pop_user.casefold()}"
        return hashlib.sha256(value.encode()).hexdigest()

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(k for k, on in (("wecom", self.wecom_enabled), ("smtp", self.smtp_enabled)) if on)

    @property
    def recipients(self) -> list[str]:
        return split_list(self.smtp_to) or ([self.smtp_user] if self.smtp_user else [])

    @property
    def wecom_mentioned_userids(self) -> list[str]:
        return list(dict.fromkeys(item.removeprefix("@") for item in split_list(self.wecom_userids)))

    @property
    def excluded_sender_addresses(self) -> set[str]:
        return {address.casefold() for address in split_list(self.excluded_senders)}

    def validate_excluded_senders(self) -> None:
        for address in split_list(self.excluded_senders):
            if not re.fullmatch(r"[^\s@,;<>\"()\\*]+@[^\s@,;<>\"()\\*]+\.[^\s@,;<>\"()\\*]+", address):
                raise ConfigError("排除发件人请填写完整邮箱地址，用逗号分隔；不要填显示姓名、域名或通配符。")

    def validate(self, require_channels: bool = True) -> None:
        self.validate_excluded_senders()
        if not self.pop_host.strip() or not self.pop_user.strip() or not self.pop_password:
            raise ConfigError("请填写公司 POP3 服务器、邮箱账号和密码。")
        if any(c in self.pop_host for c in "/\\ \t\r\n") or self.pop_host != self.pop_host.strip():
            raise ConfigError("POP3 服务器只填写域名或 IP，不带协议、端口或路径。")
        for name, value in (("POP3 端口", self.pop_port), ("SMTP 端口", self.smtp_port)):
            if type(value) is not int or not 1 <= value <= 65535:
                raise ConfigError(f"{name}应在 1–65535 之间。")
        for name, value in (("POP3", self.pop_security), ("SMTP", self.smtp_security)):
            if value not in ("SSL/TLS", "STARTTLS", "不加密"):
                raise ConfigError(f"{name} 加密方式无效。")
        if not 3 <= self.poll_seconds <= 3600:
            raise ConfigError("检查间隔应在 3–3600 秒之间。")
        if not 3 <= self.timeout_seconds <= 120:
            raise ConfigError("网络超时应在 3–120 秒之间。")
        if self.pop_ca_file and not Path(self.pop_ca_file).is_file():
            raise ConfigError("所选 CA 证书文件不存在。")
        if not require_channels:
            return
        if not self.channels:
            raise ConfigError("请至少启用一个转发通道。")
        if self.wecom_enabled:
            url = urlparse(self.wecom_webhook)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ConfigError("请填写有效的 HTTPS 企业微信群机器人 Webhook 地址。")
            if self.wecom_type not in ("markdown", "text"):
                raise ConfigError("企业微信消息格式应为 markdown 或 text。")
            members = self.wecom_mentioned_userids
            if len(members) > 20:
                raise ConfigError("提醒成员每次最多填写 20 个 UserID。")
            for member in members:
                if member.casefold() == "all":
                    raise ConfigError("提醒成员请填写具体 UserID；提醒全员请用 text 模式，并在提醒手机号中填 @all。")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", member):
                    raise ConfigError("提醒成员需填写企微账号 UserID（如 zhangsan），用逗号分隔；不要填显示姓名或 <@...> 标记。")
            for number in split_list(self.wecom_mobiles):
                if number != "@all" and not re.fullmatch(r"\+?\d{5,20}", number):
                    raise ConfigError("提醒手机号请用逗号分隔，提醒全员填 @all。")
        if self.smtp_enabled:
            if len(self.recipients) != 1:
                raise ConfigError("本版本收件邮箱只填写一个地址；留空则发给自己。")
            if not self.smtp_host.strip() or not self.smtp_password:
                raise ConfigError("请填写 SMTP 服务器及授权码 / 密码。")
            if any(c in self.smtp_host for c in "/\\ \t\r\n"):
                raise ConfigError("SMTP 服务器只填写域名或 IP。")
            for address in [self.smtp_user, *self.recipients]:
                if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", address):
                    raise ConfigError("SMTP 发件账号及收件地址需填写完整邮箱地址。")


SECRET_KEYS = ("pop_password", "wecom_webhook", "smtp_password")


class WindowsVault:
    """DPAPI's default scope is the current Windows user; no machine scope."""

    def _transform(self, value: bytes, protect: bool) -> bytes:
        if sys.platform != "win32":
            raise ConfigError("记住密码使用 Windows 系统加密；当前系统请取消勾选后保存普通配置。")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypto = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        fn = crypto.CryptProtectData if protect else crypto.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p if protect else ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        buf = ctypes.create_string_buffer(value)
        source = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        description = "MailForwarderClient" if protect else None
        if not fn(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(target)):
            raise OSError(ctypes.get_last_error(), "Windows 凭据加密 / 解密失败")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))

    def seal(self, value: str) -> str:
        return base64.b64encode(self._transform(value.encode("utf-8"), True)).decode("ascii")

    def unseal(self, value: str) -> str:
        return self._transform(base64.b64decode(value, validate=True), False).decode("utf-8")


class ConfigStore:
    def __init__(self, folder: Path, vault=None):
        self.path = folder / "config.json"
        self.vault = vault or WindowsVault()

    def save(self, cfg: Config) -> None:
        data = asdict(cfg)
        secrets = {key: data.pop(key) for key in SECRET_KEYS}
        protected = {}
        if cfg.remember_credentials:
            protected = {key: self.vault.seal(value) for key, value in secrets.items() if value}
        payload = {"version": 1, "settings": data, "protected_secrets": protected}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if sys.platform != "win32":
                os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self) -> tuple[Config, str]:
        if not self.path.exists():
            return Config(), ""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cfg = Config()
            allowed = {f.name: f for f in fields(Config)}
            for key, value in data.get("settings", {}).items():
                if key in allowed and key not in SECRET_KEYS:
                    if type(value) is not type(getattr(cfg, key)):
                        raise ConfigError(f"配置字段 {key} 的类型不正确。")
                    setattr(cfg, key, value)
            warnings = []
            if cfg.remember_credentials:
                for key, value in data.get("protected_secrets", {}).items():
                    if key in SECRET_KEYS:
                        try:
                            setattr(cfg, key, self.vault.unseal(value))
                        except Exception:
                            warnings.append("部分已保存凭据无法解密，请在当前 Windows 账号下重新输入。")
            return cfg, " ".join(dict.fromkeys(warnings))
        except Exception as exc:
            # Do not overwrite the damaged file or silently change the mail baseline.
            raise ConfigError("配置文件无法读取，请打开数据目录备份 config.json，再检查文件。") from exc


class InstanceLock:
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        # Do not read an already locked byte: Windows rejects that read before locking.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ConfigError("客户端已经在运行，请切换到现有窗口。") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None
