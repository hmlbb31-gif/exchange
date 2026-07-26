"""Слой хранения бота: SQLite + шифрование кук.

Одна БД на всё приложение. Таблицы:
  - accounts       — аккаунты mangabuff (роль trade/farm, кука шифрованная, прокси).
  - daily_runs     — учёт выполненных дэйликов, чтобы не делать дважды.
  - decisions      — журнал решений обменника (для /stats и сверки).
  - protected_cards — ручной whitelist защищённых карт (правится из ТГ).
  - quiz_answers   — накопленная база ответов на квиз.

ВАЖНО (архитектура, уточнено 2026-07-26): обменник работает на ОДНОМ аккаунте
(role='trade'), мультиаккаунт (~10) нужен только фарму (role='farm'). Прокси
привязывается прежде всего к фарм-аккаунтам.

Куки шифруются Fernet (ключ FERNET_KEY). Если ключа нет — хранятся как есть
(plaintext) с пометкой enc=0, чтобы бот работал и без шифрования (менее безопасно).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

try:  # cryptography не обязателен, если не шифруем
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore


ROLE_TRADE = "trade"
ROLE_FARM = "farm"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    cookie      TEXT NOT NULL,      -- шифротекст (enc=1) или plaintext (enc=0)
    enc         INTEGER NOT NULL DEFAULT 0,
    user_agent  TEXT NOT NULL,
    proxy       TEXT,               -- напр. http://user:pass@host:port (для фарма)
    role        TEXT NOT NULL DEFAULT 'farm',
    active      INTEGER NOT NULL DEFAULT 1,
    last_ok     REAL
);
CREATE TABLE IF NOT EXISTS daily_runs (
    account_id  INTEGER NOT NULL,
    task        TEXT NOT NULL,
    day         TEXT NOT NULL,      -- YYYY-MM-DD
    status      TEXT NOT NULL,
    reward      INTEGER DEFAULT 0,
    ts          REAL NOT NULL,
    PRIMARY KEY (account_id, task, day)
);
CREATE TABLE IF NOT EXISTS decisions (
    trade_id    INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    reason      TEXT,
    ts          REAL NOT NULL,
    PRIMARY KEY (trade_id, ts)
);
CREATE TABLE IF NOT EXISTS protected_cards (
    card_id     INTEGER PRIMARY KEY,
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS quiz_answers (
    question    TEXT PRIMARY KEY,
    answer      TEXT NOT NULL
);
"""


class CookieCrypto:
    """Шифрование кук. Без ключа — прозрачный проброс (enc=0)."""

    def __init__(self, key: str = ""):
        self._fernet = None
        if key:
            if Fernet is None:
                raise RuntimeError("cryptography не установлен, а FERNET_KEY задан")
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> tuple[str, int]:
        if self._fernet is None:
            return plaintext, 0
        return self._fernet.encrypt(plaintext.encode()).decode(), 1

    def decrypt(self, stored: str, enc: int) -> str:
        if enc and self._fernet is not None:
            return self._fernet.decrypt(stored.encode()).decode()
        return stored

    @staticmethod
    def generate_key() -> str:
        if Fernet is None:
            raise RuntimeError("cryptography не установлен")
        return Fernet.generate_key().decode()


@dataclass
class Account:
    id: int
    label: str
    cookie: str          # уже расшифрованная
    user_agent: str
    proxy: str | None
    role: str
    active: bool
    last_ok: float | None


class Store:
    def __init__(self, db_path: str | Path = ":memory:", fernet_key: str = ""):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.crypto = CookieCrypto(fernet_key)

    def close(self) -> None:
        self.db.close()

    # --- аккаунты ---
    def add_account(self, label: str, cookie: str, user_agent: str,
                    proxy: str | None = None, role: str = ROLE_FARM) -> int:
        enc_cookie, enc = self.crypto.encrypt(cookie)
        cur = self.db.execute(
            "INSERT INTO accounts (label, cookie, enc, user_agent, proxy, role) "
            "VALUES (?,?,?,?,?,?)",
            (label, enc_cookie, enc, user_agent, proxy, role))
        self.db.commit()
        return int(cur.lastrowid)

    def _row_to_account(self, r: sqlite3.Row) -> Account:
        return Account(
            id=r["id"], label=r["label"],
            cookie=self.crypto.decrypt(r["cookie"], r["enc"]),
            user_agent=r["user_agent"], proxy=r["proxy"], role=r["role"],
            active=bool(r["active"]), last_ok=r["last_ok"])

    def get_account(self, account_id: int) -> Account | None:
        r = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    def list_accounts(self, role: str | None = None,
                      active_only: bool = False) -> list[Account]:
        q, args = "SELECT * FROM accounts", []
        conds = []
        if role:
            conds.append("role=?"); args.append(role)
        if active_only:
            conds.append("active=1")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id"
        return [self._row_to_account(r) for r in self.db.execute(q, args)]

    def update_cookie(self, account_id: int, cookie: str) -> None:
        enc_cookie, enc = self.crypto.encrypt(cookie)
        self.db.execute("UPDATE accounts SET cookie=?, enc=? WHERE id=?",
                        (enc_cookie, enc, account_id))
        self.db.commit()

    def set_active(self, account_id: int, active: bool) -> None:
        self.db.execute("UPDATE accounts SET active=? WHERE id=?",
                        (1 if active else 0, account_id))
        self.db.commit()

    def set_proxy(self, account_id: int, proxy: str | None) -> None:
        self.db.execute("UPDATE accounts SET proxy=? WHERE id=?", (proxy, account_id))
        self.db.commit()

    def mark_ok(self, account_id: int, ts: float | None = None) -> None:
        self.db.execute("UPDATE accounts SET last_ok=? WHERE id=?",
                        (ts if ts is not None else time.time(), account_id))
        self.db.commit()

    # --- дэйлики ---
    def record_run(self, account_id: int, task: str, day: str,
                   status: str, reward: int = 0) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO daily_runs (account_id, task, day, status, reward, ts) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, task, day, status, reward, time.time()))
        self.db.commit()

    def was_done(self, account_id: int, task: str, day: str) -> bool:
        r = self.db.execute(
            "SELECT status FROM daily_runs WHERE account_id=? AND task=? AND day=?",
            (account_id, task, day)).fetchone()
        return bool(r) and r["status"] == "done"

    def farm_summary(self, day: str) -> dict[str, int]:
        """Свод за день: task -> суммарная награда (для /stats)."""
        out: dict[str, int] = {}
        for r in self.db.execute(
                "SELECT task, SUM(reward) s FROM daily_runs WHERE day=? GROUP BY task",
                (day,)):
            out[r["task"]] = int(r["s"] or 0)
        return out

    # --- решения обменника ---
    def log_decision(self, trade_id: int, verdict: str, reason: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO decisions (trade_id, verdict, reason, ts) "
            "VALUES (?,?,?,?)", (trade_id, verdict, reason, time.time()))
        self.db.commit()

    def decision_counts(self, since_ts: float = 0.0) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.db.execute(
                "SELECT verdict, COUNT(*) c FROM decisions WHERE ts>=? GROUP BY verdict",
                (since_ts,)):
            out[r["verdict"]] = int(r["c"])
        return out

    # --- защищённые карты ---
    def protect(self, card_id: int, reason: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO protected_cards (card_id, reason) "
                        "VALUES (?,?)", (card_id, reason))
        self.db.commit()

    def unprotect(self, card_id: int) -> None:
        self.db.execute("DELETE FROM protected_cards WHERE card_id=?", (card_id,))
        self.db.commit()

    def protected_ids(self) -> set[int]:
        return {r["card_id"] for r in self.db.execute("SELECT card_id FROM protected_cards")}

    # --- база ответов квиза ---
    def get_quiz_answer(self, question: str) -> str | None:
        r = self.db.execute("SELECT answer FROM quiz_answers WHERE question=?",
                            (question,)).fetchone()
        return r["answer"] if r else None

    def set_quiz_answer(self, question: str, answer: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO quiz_answers (question, answer) "
                        "VALUES (?,?)", (question, answer))
        self.db.commit()
