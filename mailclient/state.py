"""Durable per-account, per-channel delivery records. Never randomly trim UIDLs."""
from __future__ import annotations

from pathlib import Path
import sqlite3


class StateStore:
    def __init__(self, path: Path, account: str):
        self.account = account
        self.db = sqlite3.connect(str(path), timeout=10)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                account TEXT NOT NULL, uid TEXT NOT NULL,
                wecom INTEGER NOT NULL DEFAULT 0, smtp INTEGER NOT NULL DEFAULT 0,
                ignored INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account, uid)
            );
            """)
        except Exception:
            self.db.close()
            raise

    @property
    def initialized(self) -> bool:
        row = self.db.execute("SELECT initialized FROM accounts WHERE account=?", (self.account,)).fetchone()
        return bool(row and row[0])

    def initialize(self, uids: list[str], test_latest: bool) -> None:
        # Commit the baseline BEFORE retrieving or testing the newest message.
        # New mail arriving during a failed first test will then remain pending.
        skipped = uids[:-1] if test_latest and uids else uids
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO messages(account,uid,wecom,smtp,ignored) VALUES (?,?,1,1,1)",
                                [(self.account, uid) for uid in skipped])
            self.db.execute("INSERT OR REPLACE INTO accounts(account,initialized) VALUES (?,1)", (self.account,))

    def records(self) -> dict[str, tuple[bool, bool, bool]]:
        return {uid: (bool(w), bool(s), bool(i)) for uid, w, s, i in
                self.db.execute("SELECT uid,wecom,smtp,ignored FROM messages WHERE account=?", (self.account,))}

    def ensure(self, uid: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO messages(account,uid) VALUES (?,?)", (self.account, uid))

    def ignore(self, uid: str) -> None:
        with self.db:
            self.db.execute("UPDATE messages SET ignored=1,wecom=1,smtp=1 WHERE account=? AND uid=?", (self.account, uid))

    def done(self, uid: str, channel: str) -> None:
        if channel not in ("wecom", "smtp"):
            raise ValueError("Unknown channel")
        with self.db:
            self.db.execute(f"UPDATE messages SET {channel}=1 WHERE account=? AND uid=?", (self.account, uid))

    def close(self) -> None:
        self.db.close()
