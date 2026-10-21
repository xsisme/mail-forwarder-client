"""Real loopback protocol checks. Never uses the uploaded account or any public server."""
from contextlib import contextmanager
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from mailclient.config import Config
from mailclient.engine import Forwarder
from mailclient.state import StateStore
from mailclient.transports import diagnose, send_smtp, send_wecom
from mailclient.parsing import Mail


class LocalProtocolHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.exchange()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # A reachability probe closes without reading the banner. On Windows
            # this normally raises 10053/10054; it is not a protocol test failure.
            return


class PopHandler(LocalProtocolHandler):
    def exchange(self):
        self.wfile.write(b"+OK test POP3 ready\r\n")
        authenticated = False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            parts = line.decode().strip().split(" ", 1)
            command = parts[0].upper()
            argument = parts[1] if len(parts) == 2 else ""
            self.server.commands.append(command)
            if command == "USER":
                self.wfile.write(b"+OK user accepted\r\n")
            elif command == "PASS":
                authenticated = argument == "fake-password"
                self.wfile.write(b"+OK authenticated\r\n" if authenticated else b"-ERR auth rejected\r\n")
            elif command == "QUIT":
                self.wfile.write(b"+OK goodbye\r\n")
                return
            elif not authenticated:
                self.wfile.write(b"-ERR authenticate first\r\n")
            elif command == "STAT":
                self.wfile.write(f"+OK {len(self.server.messages)} 100\r\n".encode())
            elif command == "UIDL":
                self.wfile.write(b"+OK UIDL listing\r\n")
                for index, (uid, _) in enumerate(self.server.messages, 1):
                    self.wfile.write(f"{index} {uid}\r\n".encode())
                self.wfile.write(b".\r\n")
            elif command == "RETR":
                raw = self.server.messages[int(argument) - 1][1]
                self.wfile.write(b"+OK message follows\r\n")
                for raw_line in raw.splitlines():
                    if raw_line.startswith(b"."):
                        raw_line = b"." + raw_line
                    self.wfile.write(raw_line + b"\r\n")
                self.wfile.write(b".\r\n")
            else:
                self.wfile.write(b"-ERR unsupported\r\n")


class SmtpHandler(LocalProtocolHandler):
    def exchange(self):
        self.wfile.write(b"220 localhost SMTP test\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode().strip().split(" ", 1)[0].upper()
            if command in ("EHLO", "HELO"):
                self.wfile.write(b"250-localhost\r\n250-AUTH PLAIN\r\n250 SIZE 30000000\r\n")
            elif command == "AUTH":
                self.wfile.write(b"235 authentication successful\r\n")
            elif command in ("MAIL", "RCPT", "RSET", "NOOP"):
                self.wfile.write(b"250 OK\r\n")
            elif command == "DATA":
                self.wfile.write(b"354 end with dot\r\n")
                body = []
                while True:
                    item = self.rfile.readline()
                    if not item or item == b".\r\n":
                        break
                    body.append(item)
                self.server.delivered.append(b"".join(body))
                self.wfile.write(b"250 queued\r\n")
            elif command == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"500 unknown\r\n")


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.payloads.append(json.loads(content.decode("utf-8")))
        result = b'{"errcode":0,"errmsg":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)
    def log_message(self, *args):
        pass


class ThreadedTCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def server_instance(kind, handler):
    server = kind(("127.0.0.1", 0), handler)
    server.commands, server.messages, server.delivered, server.payloads = [], [], [], []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, pop, smtp):
        return Config(pop_host="127.0.0.1", pop_port=pop.server_address[1], pop_security="不加密",
                      pop_user="user@example.test", pop_password="fake-password", wecom_enabled=False,
                      smtp_enabled=True, smtp_host="127.0.0.1", smtp_port=smtp.server_address[1],
                      smtp_security="不加密", smtp_user="me@example.test", smtp_password="fake-auth",
                      smtp_to="receiver@example.test", timeout_seconds=3, remember_credentials=False,
                      test_latest_first_run=False, keywords="")

    def test_real_pop_and_smtp_login_diagnostic_then_forward_once(self):
        with server_instance(ThreadedTCP, PopHandler) as pop, server_instance(ThreadedTCP, SmtpHandler) as smtp:
            pop.commands, pop.messages, smtp.delivered = [], [], []
            cfg = self.config(pop, smtp)
            cfg.validate()
            events = []
            result = diagnose(cfg, lambda kind, **data: events.append({"kind": kind, **data}))
            self.assertTrue(result, f"{result!r}\n" + "\n".join(
                event.get("text", "") for event in events if event.get("level") == "ERROR"))
            self.assertEqual(len(smtp.delivered), 1)  # explicit diagnostic test email
            state = StateStore(Path(self.tmp.name) / "state.sqlite3", cfg.account_id)
            try:
                engine = Forwarder(cfg, Path(self.tmp.name), threading.Event())
                engine.poll_once(state)  # empty mailbox baseline
                message = EmailMessage()
                message["Subject"] = "验证码通知"
                message.set_content("验证码：135790\n.这一行以点开头")
                pop.messages.append(("new-id", message.as_bytes()))
                engine.poll_once(state)
                engine.poll_once(state)
                self.assertEqual(len(smtp.delivered), 2)
                self.assertNotIn("DELE", pop.commands)
                self.assertEqual(state.records()["new-id"], (True, True, False))
            finally:
                state.close()

    def test_real_smtp_accepts_chinese_computer_name(self):
        with server_instance(ThreadedTCP, SmtpHandler) as smtp:
            cfg = Config(smtp_host="127.0.0.1", smtp_port=smtp.server_address[1], smtp_security="不加密",
                         smtp_user="me@example.test", smtp_password="fake-auth", smtp_to="receiver@example.test",
                         timeout_seconds=5)
            with patch("socket.getfqdn", return_value="办公电脑.公司.example"):
                send_smtp(cfg, Mail("中文电脑名发信测试", plain="测试内容"))
            self.assertEqual(len(smtp.delivered), 1)
            self.assertIn(b"@example.test>", smtp.delivered[0])

    def test_real_pop_rejected_password_is_separate_login_failure(self):
        with server_instance(ThreadedTCP, PopHandler) as pop, server_instance(ThreadedTCP, SmtpHandler) as smtp:
            pop.commands, pop.messages, smtp.delivered = [], [], []
            cfg = self.config(pop, smtp)
            cfg.pop_password = "incorrect"
            events = []
            result = diagnose(cfg, lambda kind, **data: events.append({"kind": kind, **data}), mailbox_only=True)
            self.assertFalse(result)
            self.assertTrue(any(e.get("key") == "network" and e.get("level") == "ok" for e in events))
            self.assertTrue(any(e.get("key") == "login" and e.get("level") == "error" for e in events))
            self.assertEqual(smtp.delivered, [])

    def test_real_http_json_webhook_transport_on_loopback_only(self):
        with server_instance(ThreadingHTTPServer, WebhookHandler) as webhook:
            webhook.payloads = []
            cfg = Config(wecom_webhook=f"http://127.0.0.1:{webhook.server_address[1]}/test",
                         wecom_userids="zhangsan,lisi")
            # HTTP is ONLY used for this local fixture; production config validation requires HTTPS.
            send_wecom(cfg, Mail("测试邮件", plain="验证码：123456", code="123456"))
            self.assertEqual(len(webhook.payloads), 1)
            self.assertEqual(webhook.payloads[0]["msgtype"], "markdown")
            self.assertIn("123456", webhook.payloads[0]["markdown"]["content"])
            self.assertTrue(webhook.payloads[0]["markdown"]["content"].startswith("<@zhangsan> <@lisi>\n\n"))


if __name__ == "__main__":
    unittest.main()
