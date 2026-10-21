"""Native Tk desktop client. All networking runs outside Tk's UI thread."""
from __future__ import annotations

from dataclasses import fields, replace
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

from . import __version__
from .config import Config, ConfigError, ConfigStore
from .cancellation import CancellationEvent
from .engine import Forwarder
from .transports import diagnose, safe_error
from .ios_layout import IOSLayout
from .ios_widgets import COLORS


class ClientWindow(IOSLayout):
    CLOSE_GRACE_SECONDS = 2.0

    def __init__(self, root: tk.Tk, folder: Path):
        self.root, self.folder = root, folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.store = ConfigStore(folder)
        self.load_warning = ""
        try:
            cfg, self.load_warning = self.store.load()
        except ConfigError as exc:
            cfg, self.load_warning = Config(), str(exc)
        self.active_config = cfg
        self.checked_config = None
        self.events = queue.Queue()
        self.stop_event = CancellationEvent()
        self.worker = None
        self.closing = False
        self.closed = False
        self.controls = []
        self.status_vars = {}
        self.status_labels = {}
        self.counters = {"discovered": 0, "delivered": 0, "failed": 0, "filtered": 0}
        self.logger = logging.getLogger(f"MailForwarderClient.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(folder / "forwarder.log", maxBytes=5 * 1024 * 1024,
                                           backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(self.handler)
        self.vars = {}
        for field in fields(Config):
            value = getattr(cfg, field.name)
            cls = tk.BooleanVar if type(value) is bool else tk.StringVar
            self.vars[field.name] = cls(root, value=value)
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain)
        if self.load_warning:
            self.post("log", level="WARNING", text=self.load_warning)
            self.root.after(200, lambda: messagebox.showwarning("配置读取提示", self.load_warning, parent=root))
        self.post("log", level="INFO", text="客户端已就绪。填写配置后，点击“登录并检测”。")

    def _browse_ca(self):
        filename = filedialog.askopenfilename(parent=self.root, title="选择公司 CA 证书", filetypes=[("证书", "*.pem *.crt *.cer"), ("所有文件", "*.*")])
        if filename:
            self.vars["pop_ca_file"].set(filename)

    def _channel_toggle(self):
        for key in ("wecom", "smtp"):
            enabled = self.vars[f"{key}_enabled"].get()
            self._status(key, "idle" if enabled else "off", "尚未检测" if enabled else "未启用")
        self.checked_config = None

    def collect(self) -> Config:
        values = {}
        defaults = Config()
        for field in fields(Config):
            value = self.vars[field.name].get()
            if type(getattr(defaults, field.name)) is int:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ConfigError("端口、检查间隔和网络超时需填写整数。")
            elif isinstance(value, str) and field.name not in ("pop_password", "smtp_password"):
                value = value.strip()
            values[field.name] = value
        cfg = Config(**values)
        cfg.validate_excluded_senders()
        return cfg

    def save(self):
        try:
            cfg = self.collect()
            self.store.save(cfg)
            self.active_config = cfg
            self.post("log", level="INFO", text="配置已保存。" + ("下次打开将自动回填已保存凭据。" if cfg.remember_credentials else "密码及 Webhook 仅在本次运行中使用。"))
            self.save_button.configure(text="已保存")
            self.root.after(1600, lambda: self.save_button.configure(text="保存配置") if not self.closing else None)
        except Exception as exc:
            messagebox.showerror("保存失败", safe_error(exc, self.active_config), parent=self.root)

    def launch(self, mode: str):
        if self.closing or (self.worker and self.worker.is_alive()):
            return
        try:
            cfg = self.collect()
            cfg.validate(require_channels=mode != "mailbox")
            self.store.save(cfg)
        except Exception as exc:
            messagebox.showerror("请检查配置", safe_error(exc, self.active_config), parent=self.root)
            return
        self.active_config = cfg
        self.stop_event.clear()
        self.set_busy(True)
        self.phase_var.set("正在检测…")
        for key in ("network", "login", "mailbox"):
            self._status(key, "idle", "等待检测")
        if mode != "mailbox":
            for key, enabled in (("wecom", cfg.wecom_enabled), ("smtp", cfg.smtp_enabled)):
                self._status(key, "idle" if enabled else "off", "等待检测" if enabled else "未启用")
        already_checked = self.checked_config == cfg

        def job():
            try:
                if mode == "mailbox":
                    ok = diagnose(cfg, self.post, mailbox_only=True, stop_event=self.stop_event)
                    self.post("phase", text="检测已停止" if ok.cancelled else ("邮箱检测完成" if ok else "邮箱检测失败"))
                else:
                    if mode == "start" and already_checked:
                        ok, can_start = True, True
                    else:
                        result = diagnose(cfg, self.post, stop_event=self.stop_event)
                        ok, can_start = bool(result), result.can_forward
                    if can_start:
                        self.post("diagnosed", config=replace(cfg))
                    if mode == "start" and can_start and not self.stop_event.is_set():
                        if not ok:
                            self.post("log", level="WARNING", text="部分通道检测失败：可用通道继续转发，失败通道会按轮询重试。")
                        self.post("phase", text="正在转发 · 自动轮询")
                        engine = Forwarder(cfg, self.folder, self.stop_event, self.post)
                        engine.last_wecom = time.monotonic()
                        engine.run()
                        self.post("phase", text="转发已停止")
                    else:
                        text = "检测通过 · 可开始转发" if ok else ("部分通道异常 · 可启动重试" if can_start else "检测未通过 · 请查看日志")
                        self.post("phase", text="检测已停止" if self.stop_event.is_set() else text)
            except Exception as exc:
                self.post("log", level="ERROR", text="任务已停止：" + safe_error(exc, cfg))
                self.post("phase", text="任务异常 · 请查看日志")
            finally:
                self.post("finished")

        self.worker = threading.Thread(target=job, name="mail-forwarder", daemon=True)
        self.worker.start()

    def set_busy(self, busy):
        for widget, normal_state in self.controls:
            widget.configure(state="disabled" if busy else normal_state)
        for widget in (self.check_button, self.start_button, self.mail_test_button, self.save_button):
            widget.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def stop(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.phase_var.set("正在停止 · 正在取消网络操作…")

    def post(self, kind: str, **data):
        # Only queue/log from workers. No Tk call is allowed in this method.
        if self.closed:
            return
        if kind == "log":
            text = safe_error(RuntimeError(str(data.get("text", ""))), self.active_config)
            data["text"] = text
            level = getattr(logging, data.get("level", "INFO"), logging.INFO)
            self.logger.log(level, text)
        self.events.put({"kind": kind, **data})

    def _status(self, key, level, text):
        if key not in self.status_vars:
            return
        self.status_vars[key].set(text)
        color = COLORS.get(level, COLORS["idle"])
        self.status_labels[key].configure(text_color=color)
        self.status_dots[key].configure(text_color=color)

    def _update_stats(self):
        c = self.counters
        self.stats_var.set(f"发现 {c['discovered']} 封   已投递 {c['delivered']} 次   过滤 {c['filtered']} 封   失败 {c['failed']} 次")
        self.check_time_var.set("最近检查  " + self.last_check)

    def _drain(self):
        if self.closed:
            return
        for _ in range(300):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["kind"]
            if kind == "status":
                self._status(event["key"], event["level"], event["text"])
            elif kind == "log":
                from datetime import datetime
                level = event.get("level", "INFO")
                line = f"{datetime.now():%H:%M:%S}  [{level}]  {event['text']}\n"
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line, level)
                if int(self.log_text.index("end-1c").split(".")[0]) > 1200:
                    self.log_text.delete("1.0", "201.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            elif kind == "counter":
                self.counters[event["key"]] += event["amount"]
                self._update_stats()
            elif kind == "checked":
                self.last_check = event["time"]
                self._update_stats()
            elif kind == "phase":
                if not self.closing:
                    self.phase_var.set(event["text"])
            elif kind == "diagnosed":
                self.checked_config = event["config"]
            elif kind == "finished":
                # A finished event can arrive just before Thread.is_alive() flips.
                self.root.after(50, self._finish_when_stopped)
        self.root.after(100, self._drain)

    def _finish_when_stopped(self):
        if self.closing or self.closed:
            return
        if self.worker and self.worker.is_alive():
            self.root.after(50, self._finish_when_stopped)
        elif not self.closing:
            self.set_busy(False)

    def open_folder(self):
        try:
            if sys.platform == "win32":
                os.startfile(str(self.folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.folder)])
            else:
                subprocess.Popen(["xdg-open", str(self.folder)])
        except Exception as exc:
            messagebox.showinfo("日志与配置目录", f"{self.folder}\n\n{exc}", parent=self.root)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.close_deadline = time.monotonic() + self.CLOSE_GRACE_SECONDS
        self.stop_event.set()
        if self.worker and self.worker.is_alive():
            self.set_busy(True)
            self.stop_button.configure(state="disabled")
            self.phase_var.set("正在关闭 · 正在取消网络操作…")
            self.root.after(50, self._close_when_stopped)
        else:
            self._destroy_root()

    def _close_when_stopped(self):
        # DNS, connection setup and TLS handshakes may still be in OS calls.
        # Never wait indefinitely in Tk; the worker is a daemon and checks
        # cancellation before starting each subsequent network operation.
        if self.closed:
            return
        if not self.worker.is_alive() or time.monotonic() >= self.close_deadline:
            self._destroy_root()
        else:
            self.root.after(50, self._close_when_stopped)

    def _destroy_root(self):
        if self.closed:
            return
        # CTk's Windows mainloop first runs a nested Tk update to paint the
        # titlebar. A close event there must not destroy the root while that
        # initialization still needs to query it.
        if not getattr(self.root, "_window_exists", True):
            self.root.after(50, self._destroy_root)
            return
        self.closed = True
        # Destroying Tk deletes Python callback commands. Cancel their timers
        # first so already pending callbacks cannot run against a dead window.
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def shutdown(self):
        self.closed = True
        self.stop_event.set()
        self.logger.removeHandler(self.handler)
        self.handler.close()


def main(argv=None) -> int:
    # Keep compatibility with callers of the previous entry point.
    from .startup import main as run
    return run(argv)

