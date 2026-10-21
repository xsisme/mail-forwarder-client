"""Regressions for hidden Windows roots and misleading packaged startup checks."""
import ctypes
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import check_startup
from mailclient import __version__, startup
from mailclient.config import InstanceLock


class StartupFailureTests(unittest.TestCase):
    def test_missing_ui_dependency_is_logged_and_releases_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.dict(sys.modules, {"customtkinter": None}), redirect_stdout(io.StringIO()):
                self.assertEqual(startup.main(["--ui-smoke-test", "--data-dir", directory]), 1)
            log = (folder / "startup.log").read_text(encoding="utf-8")
            self.assertIn("ModuleNotFoundError", log)
            self.assertFalse(json.loads((folder / startup.SMOKE_RESULT).read_text(encoding="utf-8"))["passed"])
            lock = InstanceLock(folder / "client.lock")
            lock.acquire()
            lock.release()

    def test_zero_exit_without_a_visibility_report_does_not_pass_packaged_check(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock(returncode=0)
            process.communicate.return_value = (b"", None)
            with patch("check_startup.subprocess.Popen", return_value=process), redirect_stderr(io.StringIO()):
                self.assertEqual(check_startup.main(["--data-dir", directory]), 1)

    def test_timeout_stops_only_the_startup_check_process(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock(pid=424242)
            process.communicate.side_effect = [subprocess.TimeoutExpired("test-child", 45), (b"", None)]
            with patch("check_startup.subprocess.Popen", return_value=process), \
                 patch("check_startup.subprocess.run") as terminate_tree, redirect_stderr(io.StringIO()):
                self.assertEqual(check_startup.main(["--data-dir", directory]), 1)
            process.kill.assert_called_once()
            if os.name == "nt":
                self.assertEqual(terminate_tree.call_args.args[0], ["taskkill", "/PID", "424242", "/T", "/F"])
            else:
                terminate_tree.assert_not_called()


class WindowVisibilityTests(unittest.TestCase):
    def test_initial_mapping_delay_waits_then_requires_two_visible_seconds(self):
        check = startup.WindowVisibilityCheck(0)
        self.assertFalse(check.observe(False, .25))
        self.assertFalse(check.observe(True, .8))
        self.assertFalse(check.observe(True, 2.7))
        self.assertTrue(check.observe(True, 2.9))

    def test_repainting_or_hidden_window_resets_continuous_visibility(self):
        check = startup.WindowVisibilityCheck(0)
        self.assertFalse(check.observe(True, .1))
        self.assertFalse(check.observe(False, 1.9))
        self.assertEqual(check.visible_for_ms, 0)
        self.assertFalse(check.observe(True, 2))
        self.assertFalse(check.observe(True, 3.9))
        self.assertTrue(check.observe(True, 4.1))

    def test_permanently_hidden_window_fails_instead_of_passing_or_hanging(self):
        check = startup.WindowVisibilityCheck(0)
        self.assertFalse(check.observe(False, 9.9))
        with self.assertRaisesRegex(RuntimeError, "10 秒"):
            check.observe(False, 10)


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "A native Windows desktop or X display is required")
class StartupWindowTests(unittest.TestCase):
    def setUp(self):
        import customtkinter as ctk
        from mailclient.gui import ClientWindow
        self.ctk, self.ClientWindow = ctk, ClientWindow
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def windows_titlebar(self):
        # Exercise CTk's actual Windows withdraw/mainloop/restore implementation
        # even on Linux. Stub just DWM painting, not window state or Tk events.
        module = importlib.import_module("customtkinter.windows.ctk_tk")
        from contextlib import ExitStack
        stack = ExitStack()
        if sys.platform == "win32":
            # Native Windows tests must use real native APIs. The stubs below
            # are only for exercising Windows restore logic on Linux.
            return stack
        native = SimpleNamespace(user32=SimpleNamespace(GetParent=lambda _: 0),
                                 dwmapi=SimpleNamespace(DwmSetWindowAttribute=lambda *args: 0))
        proxy = SimpleNamespace(windll=native, byref=ctypes.byref,
                                c_int=ctypes.c_int, sizeof=ctypes.sizeof)
        stack.enter_context(patch.object(module, "sys", SimpleNamespace(platform="win32")))
        stack.enter_context(patch.object(module, "ctypes", proxy))
        stack.enter_context(patch.object(self.ctk.CTk, "_windows_set_titlebar_icon", lambda _: None))
        return stack

    def assert_lock_released(self):
        lock = InstanceLock(self.folder / "client.lock")
        lock.acquire()
        lock.release()

    def assert_startup_exit(self, code, expected=0):
        if code == expected:
            return
        project = Path(__file__).resolve().parents[1]
        retained = project / ".build-test-logs" / self._testMethodName
        retained.mkdir(parents=True, exist_ok=True)
        for name in ("startup.log", startup.SMOKE_RESULT, "forwarder.log"):
            source = self.folder / name
            if source.is_file():
                shutil.copy2(source, retained / name)
        log = (self.folder / "startup.log").read_text(encoding="utf-8")
        self.fail(f"Startup exited with {code}, expected {expected}.\nLogs retained at: {retained}\n"
                  + "\n".join(log.splitlines()[-40:]))

    def test_normal_launch_remains_visible_in_windows_mainloop_without_early_update(self):
        observations = []
        roots = []
        def make_window(root, folder):
            roots.append(root)
            window = self.ClientWindow(root, folder)
            root.update = Mock(wraps=root.update)
            def inspect():
                if not root._window_exists:
                    root.after(50, inspect)
                    return
                observations.append((root.state(), bool(root.winfo_viewable()),
                                     bool(window.start_button.winfo_viewable())))
                window.close()
            root.after(500, inspect)
            return window
        with self.windows_titlebar(), \
             patch("mailclient.gui.ClientWindow", side_effect=make_window), \
             patch("mailclient.startup._show_error") as error:
            self.assert_startup_exit(startup.main(["--data-dir", self.tmp.name]))
        self.assertEqual(observations, [("normal", True, True)])
        roots[0].update.assert_not_called()
        error.assert_not_called()
        self.assert_lock_released()

    def test_close_during_windows_titlebar_setup_does_not_destroy_root_early(self):
        original = self.ctk.CTk._windows_set_titlebar_color
        observations = []
        def make_window(root, folder):
            window = self.ClientWindow(root, folder)
            root._close_test_window = window
            return window
        def titlebar(root, mode):
            window = getattr(root, "_close_test_window", None)
            if window is not None and not root._window_exists:
                window.close()
                observations.append(bool(root.winfo_exists()))
            return original(root, mode)
        with self.windows_titlebar(), \
             patch.object(self.ctk.CTk, "_windows_set_titlebar_color", titlebar), \
             patch("mailclient.gui.ClientWindow", side_effect=make_window), \
             patch("mailclient.startup._show_error") as error:
            self.assert_startup_exit(startup.main(["--data-dir", self.tmp.name]))
        self.assertEqual(observations, [True])
        error.assert_not_called()
        self.assert_lock_released()

    def test_smoke_uses_the_same_windows_startup_path_and_checks_after_two_seconds(self):
        # A fresh interpreter matches a real launch and isolates CTk's global
        # font, appearance and DPI state from earlier tests' destroyed Tk roots.
        project = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(project / "check_startup.py"), "--data-dir", self.tmp.name],
            cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=55)
        if completed.returncode:
            retained = project / ".build-test-logs" / self._testMethodName
            retained.mkdir(parents=True, exist_ok=True)
            for name in ("startup.log", startup.SMOKE_RESULT, "forwarder.log"):
                source = self.folder / name
                if source.is_file():
                    shutil.copy2(source, retained / name)
            details = completed.stdout + completed.stderr
            (retained / "check-output.txt").write_text(details, encoding="utf-8")
            self.fail(f"Standalone startup check exited with code {completed.returncode}.\n"
                      f"Logs retained at: {retained}\n{details}")
        result = json.loads((self.folder / startup.SMOKE_RESULT).read_text(encoding="utf-8"))
        self.assertEqual(result["version"], __version__)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["stable_visible_ms"], 2000)
        self.assertGreaterEqual(len(result["samples"]), 3)
        self.assertGreaterEqual(result["samples"][-1]["elapsed_ms"], 2000)
        self.assertTrue(result["samples"][-1]["visible"])
        self.assertTrue(result["samples"][-1]["start_button_visible"])
        self.assert_lock_released()

    def test_ui_construction_failure_destroys_root_and_releases_lock(self):
        with patch("mailclient.gui.ClientWindow", side_effect=RuntimeError("synthetic UI setup failure")), \
             patch("mailclient.startup._show_error") as error:
            self.assertEqual(startup.main(["--data-dir", self.tmp.name]), 1)
        error.assert_called_once()
        self.assertIn("synthetic UI setup failure", (self.folder / "startup.log").read_text(encoding="utf-8"))
        self.assert_lock_released()

    def test_callback_failure_is_logged_with_secrets_redacted_then_exits(self):
        def make_window(root, folder):
            window = self.ClientWindow(root, folder)
            window.active_config.pop_password = "fake-startup-secret"
            def fail():
                raise RuntimeError("synthetic callback failure fake-startup-secret")
            root.after(250, fail)
            return window
        with patch("mailclient.gui.ClientWindow", side_effect=make_window), \
             patch("mailclient.startup._show_error") as error:
            self.assertEqual(startup.main(["--data-dir", self.tmp.name]), 1)
        error.assert_called_once()
        log = (self.folder / "startup.log").read_text(encoding="utf-8")
        self.assertIn("synthetic callback failure", log)
        self.assertNotIn("fake-startup-secret", log)
        self.assert_lock_released()


if __name__ == "__main__":
    unittest.main()
