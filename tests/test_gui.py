"""Native desktop checks; Windows builds run these before making the EXE."""
from dataclasses import fields
import os
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
import customtkinter as ctk
import unittest
from unittest.mock import patch

from mailclient.config import Config, ConfigStore
from mailclient.gui import ClientWindow
from mailclient.transports import DiagnosticResult


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "A native Windows desktop or X display is required")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = ctk.CTk()
        self.root.withdraw()
        self.window = ClientWindow(self.root, Path(self.tmp.name))
        self.window.vars["remember_credentials"].set(False)
        self.root.update()

    def tearDown(self):
        self.window.stop_event.set()
        if self.window.worker:
            self.window.worker.join(timeout=3)
        for ident in self.root.tk.call("after", "info"):
            self.root.after_cancel(ident)
        self.window.shutdown()
        self.root.destroy()
        self.tmp.cleanup()

    def set_valid_config(self):
        for key, value in {"pop_host": "pop.example.test", "pop_user": "user@example.test", "pop_password": "fake-password",
                           "wecom_webhook": "https://example.test/send?key=NOT-REAL"}.items():
            self.window.vars[key].set(value)

    def finish_worker(self):
        self.window.worker.join(timeout=3)
        self.assertFalse(self.window.worker.is_alive())
        self.window._drain()
        self.window._finish_when_stopped()
        self.root.update()

    def test_every_configuration_field_has_ui_binding(self):
        self.assertEqual(set(self.window.vars), {field.name for field in fields(Config)})
        self.assertEqual(set(self.window.inputs), set(self.window.vars))
        self.assertEqual(len(self.window.notebook.tabs()), 4)
        self.assertEqual(len(self.window.status_vars), 5)

    def test_full_diagnostic_message_wraps_without_clipping(self):
        self.root.deiconify()
        self.root.geometry("1000x730")
        text = "连接超时；请检查 VPN、服务器端口及网络策略。"
        self.window._status("network", "error", text)
        self.root.update()
        self.assertEqual(self.window.status_vars["network"].get(), text)
        label = self.window.status_labels["network"]
        content = label._label
        self.assertLessEqual(content.winfo_reqheight(), label.winfo_height())
        self.assertLessEqual(content.winfo_rooty() + content.winfo_reqheight(),
                             self.window.status_panel.winfo_rooty() + self.window.status_panel.winfo_height())
        self.assertLessEqual(content.winfo_reqwidth(), label.winfo_width())

    def test_small_window_configuration_forms_can_scroll_to_bottom(self):
        self.root.deiconify()
        self.root.geometry("1000x730")
        self.window.notebook.select(1)
        self.root.update()
        tab = self.window.tabs[1]
        tab.canvas.yview_moveto(1)
        self.root.update()
        self.assertAlmostEqual(tab.canvas.yview()[1], 1.0, places=2)
        entry = self.window.inputs["smtp_security"]
        self.assertGreaterEqual(entry.winfo_rooty(), tab.canvas.winfo_rooty())
        self.assertLessEqual(entry.winfo_rooty() + entry.winfo_height(), tab.canvas.winfo_rooty() + tab.canvas.winfo_height())

    def test_saving_and_loading_accounts_without_remembering_password(self):
        self.set_valid_config()
        self.window.vars["excluded_senders"].set("blocked@example.test;news@example.test")
        self.window.save()
        loaded, warning = ConfigStore(Path(self.tmp.name)).load()
        self.assertEqual(loaded.pop_user, "user@example.test")
        self.assertEqual(loaded.pop_password, "")
        self.assertEqual(loaded.excluded_sender_addresses, {"blocked@example.test", "news@example.test"})
        self.assertFalse(warning)

    def test_invalid_exclusion_does_not_replace_saved_settings(self):
        self.set_valid_config()
        self.window.save()
        previous = self.window.store.path.read_bytes()
        self.window.vars["excluded_senders"].set("*@example.test")
        with patch("mailclient.gui.messagebox.showerror") as error:
            self.window.save()
        error.assert_called_once()
        self.assertEqual(self.window.store.path.read_bytes(), previous)

    def test_policy_exclusion_field_can_scroll_into_view(self):
        self.root.deiconify()
        self.root.geometry("1000x730")
        self.window.notebook.select(2)
        self.root.update()
        tab = self.window.tabs[2]
        tab.canvas.yview_moveto(1)
        self.root.update()
        entry = self.window.inputs["excluded_senders"]
        self.assertGreaterEqual(entry.winfo_rooty(), tab.canvas.winfo_rooty())
        self.assertLessEqual(entry.winfo_rooty() + entry.winfo_height(),
                             tab.canvas.winfo_rooty() + tab.canvas.winfo_height())

    def test_worker_events_are_queued_and_password_is_redacted(self):
        self.window.active_config.pop_password = "fake-secret-value"
        def work():
            self.window.post("status", key="login", level="ok", text="账号登录成功")
            self.window.post("log", level="ERROR", text="failure fake-secret-value")
        worker = threading.Thread(target=work)
        worker.start()
        worker.join(timeout=2)
        self.window._drain()
        self.assertEqual(self.window.status_vars["login"].get(), "账号登录成功")
        self.assertNotIn("fake-secret-value", self.window.log_text.get("1.0", "end"))
        self.assertNotIn("fake-secret-value", (Path(self.tmp.name) / "forwarder.log").read_text(encoding="utf-8"))

    def test_login_is_in_background_and_tk_remains_responsive(self):
        self.set_valid_config()
        release = threading.Event()
        entered = threading.Event()
        worker_threads = []
        def diagnostic(cfg, emit, **kwargs):
            worker_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return DiagnosticResult(True, {"wecom": True})
        with patch("mailclient.gui.diagnose", side_effect=diagnostic):
            self.window.launch("check")
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            heartbeat = []
            self.root.after(0, lambda: heartbeat.append(True))
            self.root.update()
            self.assertEqual(heartbeat, [True])
            self.assertEqual(str(self.window.start_button.cget("state")), "disabled")
            release.set()
            self.finish_worker()
        self.assertEqual(str(self.window.start_button.cget("state")), "normal")
        self.assertEqual(self.window.checked_config.pop_user, "user@example.test")

    def test_start_after_successful_check_uses_saved_configuration(self):
        self.set_valid_config()
        with patch("mailclient.gui.diagnose", return_value=DiagnosticResult(True, {"wecom": True})) as diagnostic, \
             patch("mailclient.gui.Forwarder") as engine:
            self.window.launch("check")
            self.finish_worker()
            self.window.launch("start")
            self.finish_worker()
        diagnostic.assert_called_once()
        engine.return_value.run.assert_called_once()
        self.assertTrue((Path(self.tmp.name) / "config.json").is_file())

    def test_stop_interrupts_detection_before_forwarding(self):
        self.set_valid_config()
        entered = threading.Event()
        def diagnostic(cfg, emit, stop_event=None, **kwargs):
            entered.set()
            stop_event.wait(2)
            return DiagnosticResult(cancelled=stop_event.is_set())
        with patch("mailclient.gui.diagnose", side_effect=diagnostic), \
             patch("mailclient.gui.Forwarder") as engine:
            self.window.launch("start")
            self.assertTrue(entered.wait(1))
            self.window.stop()
            self.finish_worker()
        engine.assert_not_called()
        self.assertEqual(self.window.phase_var.get(), "检测已停止")


if __name__ == "__main__":
    unittest.main()
