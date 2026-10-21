"""Exact sender exclusions, persistence and delivery-state regressions."""
from contextlib import contextmanager
from email.message import EmailMessage
import itertools
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mailclient.config import Config, ConfigError, ConfigStore
from mailclient.engine import Forwarder
from mailclient.parsing import is_excluded_sender, parse_mail
from mailclient.state import StateStore


def message(sender, subject="验证码 123456", body="blocked@example.test"):
    msg = EmailMessage()
    msg["From"], msg["Subject"] = sender, subject
    msg.set_content(body)
    return msg.as_bytes()


class ExclusionConfigTests(unittest.TestCase):
    def test_separators_case_and_duplicates(self):
        cfg = Config(excluded_senders=" Blocked@Example.Test，news@example.test;\nBLOCKED@example.test； ")
        cfg.validate_excluded_senders()
        self.assertEqual(cfg.excluded_sender_addresses, {"blocked@example.test", "news@example.test"})

    def test_invalid_address_is_rejected_including_mailbox_only_mode(self):
        for value in ("example.test", "*@example.test", "User <a@example.test>", "a@", "a b@example.test"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "排除发件人"):
                Config(excluded_senders=value).validate(require_channels=False)

    def test_persistence_and_upgrade_of_v120_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder))
            cfg = Config(excluded_senders="blocked@example.test", remember_credentials=False)
            store.save(cfg)
            self.assertEqual(store.load()[0].excluded_senders, cfg.excluded_senders)
            data = json.loads(store.path.read_text(encoding="utf-8"))
            del data["settings"]["excluded_senders"]
            store.path.write_text(json.dumps(data), encoding="utf-8")
            loaded, warning = store.load()
            self.assertEqual(loaded.excluded_senders, "")
            self.assertEqual(loaded.account_id, cfg.account_id)
            self.assertFalse(warning)

    def test_only_from_mailboxes_match_not_display_names_body_or_substrings(self):
        for sender, expected in (
            ("Blocked@Example.Test", True),
            ('"中文通知" <BLOCKED@example.test>', True),
            ('"通知, 邮箱" <blocked@example.test>', True),
            ("allowed@example.test, blocked@example.test", True),
            ('"blocked@example.test" <allowed@example.test>', False),
            ("notblocked@example.test", False),
            ("blocked@example.test.evil.test", False),
            ("blocked+tag@example.test", False),
            ("allowed@example.test", False),
        ):
            with self.subTest(sender=sender):
                mail = parse_mail(message(sender))
                self.assertEqual(is_excluded_sender(mail, {"blocked@example.test"}), expected)

    def test_missing_sender_still_parses_and_empty_list_allows_mail(self):
        mail = parse_mail(b"Subject: code 123456\r\n\r\nbody")
        self.assertEqual(mail.sender_addresses, ())
        self.assertFalse(is_excluded_sender(mail, {"blocked@example.test"}))
        self.assertFalse(is_excluded_sender(parse_mail(message("blocked@example.test")), set()))


class ExclusionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.cfg = Config(smtp_enabled=True, keywords="验证码", excluded_senders="blocked@example.test")
        self.state = StateStore(self.folder / "state.sqlite3", self.cfg.account_id)
        self.events = []
        self.engine = Forwarder(self.cfg, self.folder, threading.Event(),
                                lambda kind, **data: self.events.append({"kind": kind, **data}))
        self.clock = patch("mailclient.engine.time.monotonic", side_effect=itertools.count(10, 10))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.state.close()
        self.tmp.cleanup()

    @contextmanager
    def mailbox(self, messages):
        class Pop:
            def uidl(self):
                return b"+OK", [f"{i} {uid}".encode() for i, (uid, _) in enumerate(messages, 1)], 0

            def retr(self, number):
                return b"+OK", messages[number - 1][1].splitlines(), 0

        @contextmanager
        def session(*args):
            yield Pop()

        with patch("mailclient.engine.pop_session", session), \
             patch("mailclient.engine.send_wecom") as wecom, \
             patch("mailclient.engine.send_smtp") as smtp:
            yield wecom, smtp

    def test_exclusion_wins_over_keywords_on_both_routes_and_does_not_block_next_mail(self):
        self.state.initialize([], False)
        with self.mailbox([("blocked", message("blocked@example.test")),
                           ("allowed", message("allowed@example.test"))]) as (wecom, smtp):
            self.engine.poll_once(self.state)
            self.engine.poll_once(self.state)
            for sender in (wecom, smtp):
                sender.assert_called_once()
                self.assertEqual(sender.call_args.args[1].sender_addresses, ("allowed@example.test",))
        self.assertEqual(self.state.records()["blocked"], (True, True, True))
        self.assertEqual(sum(e.get("amount", 0) for e in self.events if e.get("key") == "filtered"), 1)

    def test_first_latest_mail_is_also_excluded_with_empty_keywords(self):
        self.cfg.keywords = ""
        with self.mailbox([("old", message("allowed@example.test")),
                           ("latest", message("blocked@example.test"))]) as (wecom, smtp):
            self.engine.poll_once(self.state)
            wecom.assert_not_called()
            smtp.assert_not_called()
        self.assertEqual(self.state.records()["latest"], (True, True, True))

    def test_new_exclusion_cancels_retry_on_either_previously_failed_route(self):
        self.state.initialize([], False)
        self.state.ensure("wecom-done")
        self.state.done("wecom-done", "wecom")
        self.state.ensure("smtp-done")
        self.state.done("smtp-done", "smtp")
        messages = [(uid, message("blocked@example.test")) for uid in ("wecom-done", "smtp-done")]
        with self.mailbox(messages) as (wecom, smtp):
            self.engine.poll_once(self.state)
            wecom.assert_not_called()
            smtp.assert_not_called()
        self.assertTrue(all(record[2] for record in self.state.records().values()))

    def test_removed_exclusion_only_allows_future_mail_and_survives_restart(self):
        self.state.initialize([], False)
        messages = [("ignored", message("blocked@example.test"))]
        with self.mailbox(messages) as (wecom, smtp):
            self.engine.poll_once(self.state)
            self.state.close()
            self.state = StateStore(self.folder / "state.sqlite3", self.cfg.account_id)
            self.cfg.excluded_senders = ""
            self.engine.poll_once(self.state)
            wecom.assert_not_called()
            smtp.assert_not_called()
            messages.append(("future", message("blocked@example.test")))
            self.engine.poll_once(self.state)
            wecom.assert_called_once()
            smtp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
