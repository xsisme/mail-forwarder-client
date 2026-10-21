"""Visible desktop startup, offline checks and diagnostics for windowed EXEs."""
from __future__ import annotations

import argparse
from dataclasses import fields
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import time
import traceback

from . import __version__
from .config import Config, ConfigError, InstanceLock, SECRET_KEYS, data_directory


SMOKE_RESULT = "ui-smoke-result.json"
SMOKE_SCHEMA = 2
SMOKE_VISIBLE_MS = 2000


class WindowVisibilityCheck:
    """Allow initial mapping/repainting, then require two stable visible seconds."""
    def __init__(self, started, timeout=10):
        self.started = started
        self.timeout = timeout
        self.visible_since = None
        self.visible_for_ms = 0

    def observe(self, visible, now):
        if not visible:
            self.visible_since = None
            self.visible_for_ms = 0
        elif self.visible_since is None:
            self.visible_since = now
        if self.visible_since is not None:
            self.visible_for_ms = int((now - self.visible_since) * 1000)
        if now - self.started >= self.timeout:
            raise RuntimeError("窗口自检超时：10 秒内未达到连续 2 秒可见，请查看窗口状态记录。")
        return visible and now - self.visible_since >= SMOKE_VISIBLE_MS / 1000


def _show_error(text, root=None):
    try:
        from tkinter import messagebox
        if root is not None and root.winfo_exists():
            root.deiconify()
            messagebox.showerror("邮件转发助手", text, parent=root)
        else:
            messagebox.showerror("邮件转发助手", text)
    except Exception:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "邮件转发助手", 0x10)
        elif sys.stderr is not None:
            print(text, file=sys.stderr)


def _log_exception(logger, exc_type, exc, tb, window):
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    if window is not None:
        for key in SECRET_KEYS:
            value = getattr(window.active_config, key, "")
            if value:
                details = details.replace(value, "[已隐藏]")
    details = re.sub(r"(?i)([?&]key=)[^\s&\"'<>]+", r"\1[已隐藏]", details)
    logger.error("客户端异常：\n%s", details)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="邮件转发助手")
    parser.add_argument("--data-dir", type=Path, help="自定义配置和状态目录")
    parser.add_argument("--ui-smoke-test", action="store_true", help="离线检查窗口持续可见，然后自动关闭")
    args = parser.parse_args(argv)
    folder = args.data_dir
    root = window = lock = logger = handler = None
    exit_code = 0
    samples = []
    smoke_passed = False
    visibility = None
    started = time.monotonic()
    try:
        folder = (folder or data_directory()).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(folder / "client.lock")
        lock.acquire()
        logger = logging.getLogger(f"MailForwarderClient.startup.{id(lock)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(folder / "startup.log", maxBytes=1024 * 1024,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.info("启动 v%s；Python %s；平台 %s；冻结程序=%s；界面检查=%s",
                    __version__, sys.version.split()[0], sys.platform,
                    bool(getattr(sys, "frozen", False)), args.ui_smoke_test)
        if args.ui_smoke_test:
            (folder / SMOKE_RESULT).unlink(missing_ok=True)

        # Import the UI only after the diagnostic log is available.
        import customtkinter as ctk
        from .gui import ClientWindow

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()

        def quit_when_mainloop_ready():
            # quit() during CTk's nested startup update is reset when Tk later
            # enters its actual mainloop. Defer until that prelude has finished.
            if not getattr(root, "_window_exists", True):
                root.after(50, quit_when_mainloop_ready)
            else:
                root.quit()

        def callback_error(exc_type, exc, tb):
            nonlocal exit_code
            exit_code = 1
            _log_exception(logger, exc_type, exc, tb, window)
            if window is not None:
                window.stop_event.set()
            quit_when_mainloop_ready()

        root.report_callback_exception = callback_error
        window = ClientWindow(root, folder)

        def show_window():
            root.deiconify()
            root.lift()
            root.focus_set()

        # Do not withdraw this CTk root or call update() before mainloop().
        # CTk's Windows titlebar setup hides it again on first mainloop entry.
        # An earlier withdraw() tells CTk to leave it hidden at that point.
        root.after_idle(show_window)

        if args.ui_smoke_test:
            # A build-check window must not accept changes to its test settings.
            root.title(f"邮件转发助手 {__version__} · 打包自检，完成后自动关闭")
            window.phase_var.set("正在打包自检 · 请勿操作，稍后自动关闭")
            window.set_busy(True)
            window.stop_button.configure(state="disabled")
            if (len(window.status_vars) != 5 or len(window.vars) != len(fields(Config))
                    or len(window.notebook.tabs()) != 4):
                raise RuntimeError("界面自检失败：配置、状态或导航控件绑定不完整。")
            current = window.collect()
            changed = [field.name for field in fields(Config)
                       if getattr(current, field.name) != getattr(window.active_config, field.name)]
            if changed:
                raise RuntimeError("界面自检失败：配置回填不一致，字段：" + ", ".join(changed))
            def inspect_window():
                nonlocal smoke_passed, visibility
                if not getattr(root, "_window_exists", True):
                    root.after(50, inspect_window)
                    return
                now = time.monotonic()
                if visibility is None:
                    visibility = WindowVisibilityCheck(now)
                sample = {
                    "elapsed_ms": int((now - started) * 1000),
                    "state": root.state(),
                    "visible": bool(root.winfo_viewable()),
                    "start_button_visible": bool(window.start_button.winfo_viewable()),
                }
                visible = (sample["state"] in ("normal", "zoomed") and sample["visible"]
                           and sample["start_button_visible"])
                try:
                    ready = visibility.observe(visible, now)
                finally:
                    sample["visible_for_ms"] = visibility.visible_for_ms
                    samples.append(sample)
                    logger.info("窗口检查：%s", sample)
                if ready:
                    smoke_passed = True
                    window.close()
                else:
                    root.after(100, inspect_window)

            # Normal and check launches enter the same mainloop. Windows may
            # still be mapping/repainting at 250ms; wait for stable visibility
            # instead of failing a single snapshot or bypassing the mainloop.
            root.after(100, inspect_window)
        else:
            root.after(1000, lambda: logger.info("窗口状态=%s；可见=%s",
                                                root.state(), bool(root.winfo_viewable())))
        root.mainloop()
        if args.ui_smoke_test and not smoke_passed:
            exit_code = 1
            logger.error("窗口自检未完成；窗口可能被提前关闭，或发生了上方记录的异常。")
        if exit_code and not args.ui_smoke_test:
            _show_error(f"客户端运行出错，已停止。\n请查看启动日志：\n{folder / 'startup.log'}", root)
    except ConfigError as exc:
        exit_code = 1
        if logger is not None:
            logger.error("启动失败：%s", exc)
        if not args.ui_smoke_test:
            _show_error(str(exc), root)
    except Exception:
        exit_code = 1
        if logger is not None:
            _log_exception(logger, *sys.exc_info(), window)
        if not args.ui_smoke_test:
            location = folder / "startup.log" if handler is not None else "数据目录不可写，未能生成日志。"
            _show_error(f"客户端启动失败。\n请查看启动日志：\n{location}", root)
    finally:
        if window is not None:
            window.stop_event.set()
        if root is not None:
            try:
                for timer in root.tk.call("after", "info"):
                    root.after_cancel(timer)
                if root.winfo_exists():
                    root.destroy()
            except Exception:
                pass
        if window is not None:
            window.shutdown()
        if args.ui_smoke_test and handler is not None:
            try:
                result = {"schema_version": SMOKE_SCHEMA, "version": __version__,
                          "passed": exit_code == 0 and smoke_passed,
                          "required_visible_ms": SMOKE_VISIBLE_MS,
                          "stable_visible_ms": visibility.visible_for_ms if visibility else 0,
                          "samples": samples}
                (folder / SMOKE_RESULT).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                exit_code = 1
                logger.exception("无法写入界面检查结果")
        if logger is not None:
            logger.info("客户端退出，状态码=%s", exit_code)
        if handler is not None:
            handler.close()
            logger.removeHandler(handler)
        if lock is not None:
            lock.release()
    if args.ui_smoke_test and sys.stdout is not None:
        print("UI smoke test passed; no network requests were made." if exit_code == 0
              else f"UI smoke test failed; see {folder / 'startup.log'}")
    return exit_code
