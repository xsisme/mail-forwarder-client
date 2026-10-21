from contextlib import contextmanager
from dataclasses import replace
from email.message import EmailMessage
import itertools
import json
from pathlib import Path
import poplib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from mailclient.config import Config, ConfigError, ConfigStore, InstanceLock, WindowsVault
from mailclient.engine import Forwarder
from mailclient.parsing import Attachment, Mail, extract_code, matches_keywords, parse_mail, truncate_utf8
from mailclient.state import StateStore
from mailclient.transports import (TransportError, build_forward, diagnose, pop_session,
                                   safe_error, send_wecom, smtp_hello_name, smtp_session, uidl_list, wecom_payload)


def valid_config(**kwargs):
    return replace(Config(), pop_host="pop.example.test", pop_user="user@example.test",
                   pop_password="sample-password-not-real", wecom_webhook="https://example.test/send?key=FAKE-KEY",
                   remember_credentials=False, **kwargs)


def raw_mail(subject="验证码通知", body="您的验证码为 123456。"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "sender@example.test"
    msg.set_content(body)
    return msg.as_bytes()


class FakeVault:
    def seal(self, value):
        import base64
        return "TEST:" + base64.b64encode(value.encode()).decode()

    def unseal(self, value):
        import base64
        return base64.b64decode(value.removeprefix("TEST:")).decode()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.store = ConfigStore(self.folder, FakeVault())

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_have_no_embedded_credentials(self):
        cfg = Config()
        self.assertEqual((cfg.pop_password, cfg.wecom_webhook, cfg.smtp_password, cfg.pop_user), ("", "", "", ""))

    def test_mention_userids_save_and_older_config_loads_without_new_field(self):
        cfg = valid_config(wecom_userids="zhangsan,@lisi,zhangsan")
        cfg.validate()
        self.assertEqual(cfg.wecom_mentioned_userids, ["zhangsan", "lisi"])
        self.store.save(cfg)
        loaded, _ = self.store.load()
        self.assertEqual(loaded.wecom_userids, cfg.wecom_userids)
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        del data["settings"]["wecom_userids"]
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        older, warning = self.store.load()
        self.assertEqual(older.wecom_userids, "")
        self.assertEqual(older.pop_user, cfg.pop_user)
        self.assertFalse(warning)

    def test_invalid_member_ids_and_excess_mentions_are_rejected(self):
        for value in ("显示姓名", "<@zhangsan>", "name with spaces", "@all", "x" * 65,
                      ",".join(f"user{i}" for i in range(21))):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                valid_config(wecom_userids=value).validate()

    def test_different_data_directories_keep_instances_and_settings_independent(self):
        first, second = self.folder / "A", self.folder / "B"
        a, b = InstanceLock(first / "client.lock"), InstanceLock(second / "client.lock")
        try:
            a.acquire()
            b.acquire()
            ConfigStore(first).save(replace(valid_config(), pop_user="a@example.test"))
            ConfigStore(second).save(replace(valid_config(), pop_user="b@example.test"))
            self.assertEqual(ConfigStore(first).load()[0].pop_user, "a@example.test")
            self.assertEqual(ConfigStore(second).load()[0].pop_user, "b@example.test")
            duplicate = InstanceLock(first / "client.lock")
            with self.assertRaises(ConfigError):
                duplicate.acquire()
        finally:
            a.release()
            b.release()

    def test_remember_round_trip_and_no_plain_secrets(self):
        cfg = valid_config()
        cfg.remember_credentials = True
        cfg.smtp_password = "another-fake-secret"
        self.store.save(cfg)
        text = self.store.path.read_text(encoding="utf-8")
        for secret in (cfg.pop_password, cfg.wecom_webhook, cfg.smtp_password):
            self.assertNotIn(secret, text)
        actual, warning = self.store.load()
        self.assertEqual(actual, cfg)
        self.assertFalse(warning)

    def test_uncheck_remember_removes_old_secrets(self):
        cfg = valid_config()
        cfg.remember_credentials = True
        self.store.save(cfg)
        cfg.remember_credentials = False
        self.store.save(cfg)
        loaded, _ = self.store.load()
        self.assertEqual(loaded.pop_user, cfg.pop_user)
        self.assertEqual(loaded.pop_password, "")
        self.assertEqual(loaded.wecom_webhook, "")
        self.assertEqual(json.loads(self.store.path.read_text(encoding="utf-8"))["protected_secrets"], {})

    def test_encryption_failure_preserves_previous_file(self):
        cfg = valid_config()
        self.store.save(cfg)
        previous = self.store.path.read_bytes()
        cfg.remember_credentials = True
        with patch.object(self.store.vault, "seal", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                self.store.save(cfg)
        self.assertEqual(previous, self.store.path.read_bytes())

    def test_corrupt_settings_do_not_silently_reset_file(self):
        self.store.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.store.load()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{broken")

    def test_failed_decryption_is_reported(self):
        cfg = valid_config()
        cfg.remember_credentials = True
        self.store.save(cfg)
        with patch.object(self.store.vault, "unseal", side_effect=OSError("different user")):
            loaded, warning = self.store.load()
        self.assertEqual(loaded.pop_password, "")
        self.assertIn("无法解密", warning)

    def test_validation_and_account_isolation(self):
        cfg = valid_config()
        cfg.validate()
        self.assertNotEqual(cfg.account_id, replace(cfg, pop_user="other@example.test").account_id)
        for bad in (replace(cfg, pop_port=70000), replace(cfg, poll_seconds=0),
                    replace(cfg, wecom_webhook="http://example.test/"),
                    replace(cfg, wecom_enabled=False, smtp_enabled=False)):
            with self.assertRaises(ConfigError):
                bad.validate()

    def test_mailbox_only_does_not_require_channel_configuration(self):
        cfg = valid_config()
        cfg.wecom_webhook = ""
        cfg.validate(require_channels=False)

    def test_single_instance_lock(self):
        first = InstanceLock(self.folder / "lock")
        second = InstanceLock(self.folder / "lock")
        first.acquire()
        try:
            with self.assertRaises(ConfigError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI requires a real Windows user session")
    def test_real_windows_dpapi_round_trip(self):
        vault = WindowsVault()
        secret = "测试-password-123"
        sealed = vault.seal(secret)
        self.assertNotIn(secret, sealed)
        self.assertEqual(vault.unseal(sealed), secret)


class ParsingTests(unittest.TestCase):
    def test_plain_and_chinese_headers(self):
        mail = parse_mail(raw_mail())
        self.assertEqual(mail.subject, "验证码通知")
        self.assertEqual(mail.code, "123456")

    def test_html_only_mail(self):
        msg = EmailMessage()
        msg["Subject"] = "登录验证"
        msg.set_content("<style>.x{}</style><p>验证码：<b>654321</b></p>", subtype="html")
        mail = parse_mail(msg.as_bytes())
        self.assertEqual(mail.code, "654321")
        self.assertNotIn(".x", mail.plain)

    def test_codes_require_keyword_proximity(self):
        self.assertIsNone(extract_code("验证码服务维护公告", "工单 789012\n联系电话 13812345678"))
        self.assertEqual(extract_code("登录", "Your verification code is 654321"), "654321")
        self.assertEqual(extract_code("登录", "123456 is your verification code"), "123456")
        self.assertEqual(extract_code("登录", "验证码：2026"), "2026")
        self.assertIsNone(extract_code("OTP", "Please use token A123456B"))

    def test_keywords_and_utf8_byte_limit(self):
        mail = Mail("例行通知", plain="normal message")
        self.assertFalse(matches_keywords(mail, ["验证码"]))
        self.assertTrue(matches_keywords(mail, []))
        self.assertTrue(matches_keywords(Mail("Verification CODE"), ["code"]))
        for limit in (2, 20, 2000, 4000):
            result = truncate_utf8("测试\U0001f510" * 3000, limit)
            self.assertLessEqual(len(result.encode()), limit)
            self.assertNotIn("\ufffd", result)

    def test_attachments_do_not_become_mail_body(self):
        msg = EmailMessage()
        msg.set_content("正常正文")
        msg.add_attachment(b"verification code 999999", maintype="text", subtype="plain", filename="file.txt")
        mail = parse_mail(msg.as_bytes())
        self.assertNotIn("999999", mail.plain)
        self.assertEqual(mail.attachments[0].name, "file.txt")

    def test_inline_images_after_regular_attachments(self):
        cfg = valid_config()
        cfg.smtp_user = "sender@example.test"
        mail = Mail("图片邮件", plain="正文", html='<p>正文</p><img src="cid:photo">', attachments=[
            Attachment("a.txt", b"text", "text/plain"),
            Attachment("photo.png", b"fake-png", "image/png", "<photo>")])
        forwarded = build_forward(cfg, mail)
        rich = next(part for part in forwarded.walk() if part.get_content_type() == "multipart/related")
        self.assertEqual(rich.get_payload()[0].get_content_type(), "text/html")
        self.assertEqual(rich.get_payload()[1]["Content-ID"], "<photo>")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.state = StateStore(self.path, "account-A")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_over_ten_thousand_uidls_are_not_forgotten(self):
        self.state.initialize([f"uid-{x}" for x in range(10050)], False)
        self.assertEqual(len(self.state.records()), 10050)
        self.state.close()
        self.state = StateStore(self.path, "account-A")
        self.assertEqual(len(self.state.records()), 10050)
        self.assertTrue(self.state.initialized)

    def test_first_run_baseline_and_latest_pending(self):
        self.state.initialize(["one", "two", "three"], True)
        self.assertTrue(self.state.initialized)
        self.assertEqual(self.state.records(), {"one": (True, True, True), "two": (True, True, True)})

    def test_empty_baseline_is_still_initialized(self):
        self.state.initialize([], False)
        self.assertTrue(self.state.initialized)

    def test_accounts_and_delivery_channels_are_independent(self):
        self.state.ensure("one")
        self.state.done("one", "wecom")
        self.assertEqual(self.state.records()["one"], (True, False, False))
        other = StateStore(self.path, "account-B")
        try:
            self.assertEqual(other.records(), {})
            self.assertFalse(other.initialized)
        finally:
            other.close()

    def test_corrupt_state_raises_instead_of_resetting_baseline(self):
        broken = Path(self.tmp.name) / "broken.sqlite3"
        broken.write_bytes(b"definitely-not-a-sqlite-database")
        with self.assertRaises(sqlite3.DatabaseError):
            StateStore(broken, "account")


class TransportTests(unittest.TestCase):
    def test_message_id_is_ascii_with_chinese_computer_name(self):
        cfg = valid_config()
        cfg.smtp_user = "sender@example.test"
        for hostname in ("测试用户的电脑", "办公电脑.公司.example", "DESKTOP-ABCD"):
            with self.subTest(hostname=hostname), patch("socket.getfqdn", return_value=hostname):
                message = build_forward(cfg, Mail("测试邮件", plain="中文正文"))
                self.assertTrue(str(message["Message-ID"]).isascii())
                self.assertIn("@example.test>", str(message["Message-ID"]))
                self.assertTrue(message.as_bytes())

    def test_smtp_hello_name_is_ascii_or_address_literal(self):
        for hostname in ("办公电脑.公司.example", "测试用户的电脑", "DESKTOP-ABCD", "bad_name.example"):
            with self.subTest(hostname=hostname), patch("socket.getfqdn", return_value=hostname):
                name = smtp_hello_name()
                self.assertTrue(name.isascii())
                self.assertTrue(name == "[127.0.0.1]" or "." in name)

    def test_wecom_utf8_and_all_mention_field(self):
        cfg = valid_config()
        cfg.wecom_type = "text"
        cfg.wecom_mobiles = "@all,13800001111"
        payload = wecom_payload(cfg, Mail("测试", plain="正文" * 3000))
        self.assertEqual(payload["text"]["mentioned_list"], ["@all"])
        self.assertEqual(payload["text"]["mentioned_mobile_list"], ["13800001111"])
        self.assertLessEqual(len(payload["text"]["content"].encode()), 2048)
        cfg.wecom_type = "markdown"
        self.assertLessEqual(len(wecom_payload(cfg, Mail("测试", plain="正文" * 3000))["markdown"]["content"].encode()), 4096)

    def test_markdown_mentions_userids_and_keeps_complete_tags_for_long_mail(self):
        cfg = valid_config(wecom_userids="zhangsan,@lisi,zhangsan", wecom_mobiles="13800001111")
        cfg.validate()
        payload = wecom_payload(cfg, Mail("测试", plain="正文内容" * 3000))
        content = payload["markdown"]["content"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertTrue(content.startswith("<@zhangsan> <@lisi>\n\n"))
        self.assertEqual(content.count("<@zhangsan>"), 1)
        self.assertNotIn("<@13800001111>", content)
        self.assertNotIn("mentioned_list", payload["markdown"])
        self.assertLessEqual(len(content.encode("utf-8")), 4096)

    def test_text_mentions_userids_and_preserves_mobile_and_all_reminders(self):
        cfg = valid_config(wecom_type="text", wecom_userids="zhangsan,lisi", wecom_mobiles="@all,13800001111")
        cfg.validate()
        payload = wecom_payload(cfg, Mail("测试", plain="正文"))["text"]
        self.assertEqual(payload["mentioned_list"], ["zhangsan", "lisi", "@all"])
        self.assertEqual(payload["mentioned_mobile_list"], ["13800001111"])

    def test_wecom_business_error_is_not_reported_as_success(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"errcode":93000,"errmsg":"invalid key"}'
        with patch("mailclient.transports.urllib.request.urlopen", return_value=response):
            with self.assertRaises(TransportError):
                send_wecom(valid_config(), Mail("test"))

    def test_pop_password_rejection_closes_connection(self):
        connection = MagicMock()
        connection.pass_.side_effect = poplib.error_proto("-ERR auth")
        with patch("mailclient.transports.poplib.POP3_SSL", return_value=connection):
            with self.assertRaises(TransportError) as error:
                with pop_session(valid_config()):
                    pass
        self.assertEqual(error.exception.stage, "login")
        connection.quit.assert_called_once()

    def test_pop_tls_verifies_by_default(self):
        import ssl
        connection = MagicMock()
        with patch("mailclient.transports.poplib.POP3_SSL", return_value=connection) as connect:
            with pop_session(valid_config()):
                pass
            context = connect.call_args.kwargs["context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_smtp_login_error_closes_connection(self):
        import smtplib
        connection = MagicMock()
        connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad auth")
        with patch("mailclient.transports.smtplib.SMTP_SSL", return_value=connection):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                with smtp_session(valid_config()):
                    pass
        connection.quit.assert_called_once()

    def test_uidl_rejects_duplicates(self):
        connection = MagicMock()
        connection.uidl.return_value = (b"+OK", [b"1 duplicated", b"2 duplicated"], 0)
        with self.assertRaises(TransportError):
            uidl_list(connection)

    def test_diagnostics_continue_channels_when_mailbox_unreachable(self):
        events = []
        def emit(kind, **data):
            events.append({"kind": kind, **data})
        with patch("mailclient.transports.socket.getaddrinfo", side_effect=TimeoutError()), \
             patch("mailclient.transports.send_wecom") as webhook:
            result = diagnose(valid_config(), emit)
        self.assertFalse(result)
        self.assertFalse(result.can_forward)
        webhook.assert_called_once()
        self.assertTrue(any(e.get("key") == "network" and e.get("level") == "error" for e in events))
        self.assertTrue(any(e.get("key") == "wecom" and e.get("level") == "ok" for e in events))

    def test_cancelled_diagnostic_does_not_send_test_notifications(self):
        stop = threading.Event()
        stop.set()
        with patch("mailclient.transports.send_wecom") as webhook, \
             patch("mailclient.transports.socket.getaddrinfo") as dns:
            result = diagnose(valid_config(), stop_event=stop)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.can_forward)
        webhook.assert_not_called()
        dns.assert_not_called()

    def test_secrets_redacted(self):
        cfg = valid_config()
        result = safe_error(RuntimeError(f"failed: {cfg.pop_password} {cfg.wecom_webhook}"), cfg)
        self.assertNotIn(cfg.pop_password, result)
        self.assertNotIn("FAKE-KEY", result)


class FakePop:
    def __init__(self, messages):
        self.messages = messages
    def uidl(self):
        return b"+OK", [f"{i + 1} {uid}".encode() for i, (uid, _) in enumerate(self.messages)], 0
    def retr(self, number):
        return b"+OK", self.messages[number - 1][1].splitlines(), 0
    def dele(self, number):
        raise AssertionError("Original messages must never be deleted")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.cfg = valid_config(smtp_enabled=True, keywords="", test_latest_first_run=False)
        self.state = StateStore(self.folder / "delivery.sqlite3", self.cfg.account_id)
        self.events = []
        self.stop = threading.Event()
        self.forwarder = Forwarder(self.cfg, self.folder, self.stop,
                                    lambda kind, **data: self.events.append({"kind": kind, **data}))
        self.clock = patch("mailclient.engine.time.monotonic", side_effect=itertools.count(10, 10))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.state.close()
        self.tmp.cleanup()

    def session(self, pop):
        @contextmanager
        def fake(*args):
            yield pop
        return fake

    def test_partial_failure_retries_only_failed_channel_and_does_not_block_next_mail(self):
        self.state.initialize([], False)
        pop = FakePop([("one", raw_mail("第一封")), ("two", raw_mail("第二封"))])
        calls = {"wecom": [], "smtp": []}
        failed_once = set()
        def wecom(cfg, mail):
            calls["wecom"].append(mail.subject)
        def smtp(cfg, mail):
            calls["smtp"].append(mail.subject)
            if mail.subject == "第一封" and not failed_once:
                failed_once.add(True)
                raise OSError("simulated send failure")
        with patch("mailclient.engine.pop_session", self.session(pop)), \
             patch("mailclient.engine.send_wecom", side_effect=wecom), \
             patch("mailclient.engine.send_smtp", side_effect=smtp):
            self.forwarder.poll_once(self.state)
            self.assertEqual(self.state.records()["one"], (True, False, False))
            self.assertEqual(self.state.records()["two"], (True, True, False))
            self.forwarder.poll_once(self.state)
        self.assertEqual(calls["wecom"], ["第一封", "第二封"])
        self.assertEqual(calls["smtp"], ["第一封", "第二封", "第一封"])

    def test_first_test_failure_does_not_rebaseline_new_arrivals(self):
        self.cfg.test_latest_first_run = True
        self.cfg.wecom_enabled = False
        pop = FakePop([("old", raw_mail("历史")), ("latest", raw_mail("测试"))])
        with patch("mailclient.engine.pop_session", self.session(pop)), \
             patch("mailclient.engine.send_smtp", side_effect=OSError("failure")):
            self.forwarder.poll_once(self.state)
        self.assertTrue(self.state.initialized)
        pop.messages.append(("new", raw_mail("真正的新邮件")))
        subjects = []
        with patch("mailclient.engine.pop_session", self.session(pop)), \
             patch("mailclient.engine.send_smtp", side_effect=lambda cfg, mail: subjects.append(mail.subject)):
            self.forwarder.poll_once(self.state)
        self.assertEqual(subjects, ["测试", "真正的新邮件"])

    def test_filtered_mail_is_remembered(self):
        self.state.initialize([], False)
        self.cfg.keywords = "验证码"
        pop = FakePop([("one", raw_mail("例行维护", "明天维护"))])
        with patch("mailclient.engine.pop_session", self.session(pop)), \
             patch("mailclient.engine.send_smtp") as sender:
            self.forwarder.poll_once(self.state)
            self.forwarder.poll_once(self.state)
        sender.assert_not_called()
        self.assertEqual(self.state.records()["one"], (True, True, True))

    def test_stop_does_not_open_network(self):
        self.stop.set()
        with patch("mailclient.engine.pop_session") as connect:
            self.forwarder.poll_once(self.state)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
