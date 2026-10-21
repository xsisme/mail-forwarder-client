"""Stalled real sockets and process-level close regression checks."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mailclient.cancellation import CancellationEvent, Cancelled, CancellableSocket, watch_socket
from mailclient.config import Config, InstanceLock
from mailclient.parsing import Mail
from mailclient.transports import diagnose, pop_session, send_smtp, send_wecom, smtp_session
import test_protocols as protocol


class StalledPop(protocol.LocalProtocolHandler):
    def exchange(self):
        self.wfile.write(b"+OK test server\r\n")
        while line := self.rfile.readline():
            command = line.split()[0].decode().upper()
            if command == self.server.block_command:
                self.server.entered.set()
                self.server.release.wait(5)
                return
            self.wfile.write(b"+OK\r\n")
            if command == "QUIT":
                return


class StalledSmtp(protocol.LocalProtocolHandler):
    def exchange(self):
        self.wfile.write(b"220 localhost test server\r\n")
        while line := self.rfile.readline():
            command = line.split()[0].decode().upper()
            if command == self.server.block_command:
                self.server.entered.set()
                self.server.release.wait(5)
                return
            if command == "EHLO":
                self.wfile.write(b"250-localhost\r\n250 AUTH PLAIN\r\n")
            elif command == "AUTH":
                self.wfile.write(b"235 authenticated\r\n")
            elif command == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return


class StalledWebhook(protocol.WebhookHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.flush()
        self.server.entered.set()
        self.server.release.wait(5)


class FragmentedWebhook(protocol.WebhookHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Enc")
        self.wfile.flush()
        threading.Event().wait(.35)
        self.wfile.write(b"oding: chunked\r\nConnection: close\r\n\r\n")
        for chunk in (b'{"errcode":', b'0,"errmsg":"ok"}'):
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            threading.Event().wait(.35)
        self.wfile.write(b"0\r\n\r\n")


class CancellationTests(unittest.TestCase):
    def assert_interrupted(self, server, action, stop, expect_cancel=True):
        server.entered, server.release = threading.Event(), threading.Event()
        errors = []

        def work():
            try:
                action()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertTrue(server.entered.wait(2), "client did not reach stalled operation")
            started = time.monotonic()
            stop.set()
            worker.join(1.5)
            self.assertFalse(worker.is_alive(), "cancellation still waits for the network timeout")
            self.assertLess(time.monotonic() - started, 1.5)
            if expect_cancel:
                self.assertEqual(len(errors), 1, errors)
                self.assertIsInstance(errors[0], Cancelled)
            else:
                self.assertEqual(errors, [])
        finally:
            server.release.set()
            worker.join(2)

    def test_pop_login_uidl_and_quit_are_interruptible(self):
        for command in ("USER", "UIDL", "QUIT"):
            with self.subTest(command=command), protocol.server_instance(protocol.ThreadedTCP, StalledPop) as server:
                server.block_command = command
                cfg = Config(pop_host="127.0.0.1", pop_port=server.server_address[1], pop_security="不加密",
                             pop_user="user@example.test", pop_password="fake", timeout_seconds=60)
                stop = CancellationEvent()

                def action():
                    with pop_session(cfg, stop_event=stop) as connection:
                        if command == "UIDL":
                            connection.uidl()

                self.assert_interrupted(server, action, stop, expect_cancel=command != "QUIT")

    def test_smtp_login_and_quit_are_interruptible(self):
        for command in ("AUTH", "QUIT"):
            with self.subTest(command=command), protocol.server_instance(protocol.ThreadedTCP, StalledSmtp) as server:
                server.block_command = command
                cfg = Config(smtp_host="127.0.0.1", smtp_port=server.server_address[1], smtp_security="不加密",
                             smtp_user="user@example.test", smtp_password="fake", timeout_seconds=60)
                stop = CancellationEvent()

                def action():
                    with smtp_session(cfg, stop):
                        pass

                self.assert_interrupted(server, action, stop, expect_cancel=command != "QUIT")

    def test_webhook_stalled_response_body_is_interruptible(self):
        with protocol.server_instance(protocol.ThreadingHTTPServer, StalledWebhook) as server:
            cfg = Config(wecom_webhook=f"http://127.0.0.1:{server.server_address[1]}/", timeout_seconds=60)
            stop = CancellationEvent()
            self.assert_interrupted(server, lambda: send_wecom(cfg, Mail("test"), stop), stop)

    def test_all_six_stalled_operations_cancel_even_without_os_wakeup(self):
        # Reproduce Windows providers that do not wake a pending socket wait
        # on shutdown. Keep the original 1.5-second assertions unchanged.
        with patch.object(CancellationEvent, "_interrupt", lambda *args: None):
            self.test_pop_login_uidl_and_quit_are_interruptible()
            self.test_smtp_login_and_quit_are_interruptible()
            self.test_webhook_stalled_response_body_is_interruptible()

    def test_slow_fragmented_http_headers_and_chunked_body_survive_poll_timeouts(self):
        with protocol.server_instance(protocol.ThreadingHTTPServer, FragmentedWebhook) as server:
            cfg = Config(wecom_webhook=f"http://127.0.0.1:{server.server_address[1]}/", timeout_seconds=3)
            send_wecom(cfg, Mail("test"), CancellationEvent())

    def test_fragmented_lines_preserve_buffer_and_original_timeout(self):
        client, peer = socket.socketpair()
        with client, peer:
            client.settimeout(2)
            connection = CancellableSocket(client, CancellationEvent())

            def write():
                peer.sendall(b"first ")
                threading.Event().wait(.35)
                peer.sendall(b"line\r\nsecond ")
                threading.Event().wait(.35)
                peer.sendall(b"line\r\n")

            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            try:
                with connection.makefile("rb") as stream:
                    self.assertEqual(stream.readline(), b"first line\r\n")
                    self.assertEqual(stream.readline(), b"second line\r\n")
                self.assertEqual(client.gettimeout(), 2)
            finally:
                worker.join(2)

    def test_no_data_still_times_out_at_configured_deadline(self):
        client, peer = socket.socketpair()
        with client, peer:
            client.settimeout(.45)
            connection = CancellableSocket(client, CancellationEvent())
            started = time.monotonic()
            with connection.makefile("rb") as stream, self.assertRaises(TimeoutError):
                stream.readline()
            self.assertGreater(time.monotonic() - started, .35)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(client.gettimeout(), .45)

    def test_blocked_write_cancels_without_os_wakeup(self):
        client, peer = socket.socketpair()
        with client, peer:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client.settimeout(60)
            stop = CancellationEvent()
            blocked = threading.Event()

            class ObservedSocket:
                def __getattr__(self, name):
                    return getattr(client, name)

                def send(self, data, flags=0):
                    try:
                        return client.send(data, flags)
                    except TimeoutError:
                        # A real send has exhausted its polling wait. Signal
                        # that backpressure exists before requesting cancel.
                        blocked.set()
                        raise

            connection = CancellableSocket(ObservedSocket(), stop)
            errors = []

            def write():
                try:
                    # A fixed 8 MiB payload can finish on Windows before the
                    # test requests cancellation. Keep sending bounded chunks
                    # without draining the peer until an actual send stalls.
                    payload = b"x" * (64 * 1024)
                    while True:
                        connection.sendall(payload)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            try:
                self.assertTrue(blocked.wait(3), "sender did not reach a real socket write timeout")
                started = time.monotonic()
                stop.set()  # No socket registered: cancellation must be cooperative.
                worker.join(1.5)
                self.assertFalse(worker.is_alive(), "blocked write still waits for the network timeout")
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(len(errors), 1, errors)
                self.assertIsInstance(errors[0], Cancelled)
                self.assertEqual(client.gettimeout(), 60)
            finally:
                stop.set()
                worker.join(2)

    def test_cancel_before_registration_and_reuse_after_worker_cleanup(self):
        stop = CancellationEvent()
        with socket.socket() as sock:
            stop.set()
            with self.assertRaises(Cancelled), watch_socket(stop, sock):
                self.fail("cancelled socket must not be used")
            self.assertEqual(stop._sockets, set())
        stop.clear()
        with socket.socket() as sock, watch_socket(stop, sock):
            self.assertFalse(stop.is_set())
        self.assertEqual(stop._sockets, set())

    def test_cancelled_senders_do_not_open_connections(self):
        stop = CancellationEvent()
        stop.set()
        with patch("mailclient.transports.urllib.request.urlopen") as http, \
             patch("mailclient.transports.smtplib.SMTP_SSL") as smtp:
            for sender in (send_wecom, send_smtp):
                with self.assertRaises(Cancelled):
                    sender(Config(), Mail("test"), stop)
            http.assert_not_called()
            smtp.assert_not_called()

    def test_cancellation_during_dns_does_not_start_tcp_or_test_notifications(self):
        stop = CancellationEvent()

        def resolve(*args, **kwargs):
            stop.set()
            return []

        with patch("mailclient.transports.socket.getaddrinfo", side_effect=resolve), \
             patch("mailclient.transports.socket.create_connection") as tcp, \
             patch("mailclient.transports.send_wecom") as webhook:
            result = diagnose(Config(), stop_event=stop)
        self.assertTrue(result.cancelled)
        tcp.assert_not_called()
        webhook.assert_not_called()


# Run in a separate interpreter: returning from mainloop alone is insufficient
# if a blocked worker or interpreter finalization still keeps the process alive.
BLOCKED_DNS_PROCESS = r'''
import json, sys, threading, time
from unittest.mock import patch
from mailclient import gui, startup
entered = threading.Event()
observed = {"heartbeats": 0}
original = gui.ClientWindow

def stalled_dns(*args, **kwargs):
    entered.set()
    threading.Event().wait(120)
    return []

def make_window(root, folder):
    window = original(root, folder)
    for key, value in {"pop_host": "pop.example.test", "pop_user": "user@example.test",
                       "pop_password": "fake", "remember_credentials": False}.items():
        window.vars[key].set(value)
    def heartbeat():
        observed["heartbeats"] += 1
        root.after(50, heartbeat)
    def close_when_blocked():
        if not entered.is_set():
            root.after(20, close_when_blocked)
            return
        observed["close_started"] = time.monotonic()
        window.close()
        window.close()
        heartbeat()
    def begin():
        if not root._window_exists:
            root.after(50, begin)
            return
        window.launch("mailbox")
        close_when_blocked()
    root.after(50, begin)
    return window

with patch.object(gui, "ClientWindow", side_effect=make_window), \
     patch("mailclient.transports.socket.getaddrinfo", side_effect=stalled_dns):
    result = startup.main(["--data-dir", sys.argv[1]])
observed["close_elapsed"] = time.monotonic() - observed["close_started"]
print(json.dumps(observed), flush=True)
raise SystemExit(result)
'''


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "A desktop display is required")
class ShutdownProcessTests(unittest.TestCase):
    def test_stuck_dns_process_exits_within_grace_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", BLOCKED_DNS_PROCESS, directory],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                    encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertGreater(observed["heartbeats"], 10)
            self.assertLess(observed["close_elapsed"], 4)
            self.assertNotIn("TclError", result.stderr)
            lock = InstanceLock(Path(directory) / "client.lock")
            lock.acquire()
            lock.release()


if __name__ == "__main__":
    unittest.main()
