"""Тесты слоя хранения: аккаунты, шифрование кук, дэйлики, решения, защита, квиз."""

import pytest

from mb.storage import ROLE_FARM, ROLE_TRADE, CookieCrypto, Store

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except Exception:
    HAS_CRYPTO = False


def test_add_and_get_account_plaintext():
    s = Store()  # без ключа — куки plaintext
    aid = s.add_account("main", "sess=abc; __ddg=1", "UA/1", role=ROLE_TRADE)
    a = s.get_account(aid)
    assert a is not None
    assert a.label == "main" and a.role == ROLE_TRADE
    assert a.cookie == "sess=abc; __ddg=1"   # читается обратно как есть
    assert a.active is True and a.proxy is None


def test_list_accounts_by_role():
    s = Store()
    s.add_account("t", "c", "UA", role=ROLE_TRADE)
    s.add_account("f1", "c", "UA", proxy="http://p1", role=ROLE_FARM)
    s.add_account("f2", "c", "UA", proxy="http://p2", role=ROLE_FARM)
    assert len(s.list_accounts(role=ROLE_FARM)) == 2
    assert len(s.list_accounts(role=ROLE_TRADE)) == 1
    farm = s.list_accounts(role=ROLE_FARM)
    assert farm[0].proxy == "http://p1"


def test_deactivate_and_update_cookie():
    s = Store()
    aid = s.add_account("f", "old", "UA")
    s.set_active(aid, False)
    s.update_cookie(aid, "new-cookie")
    a = s.get_account(aid)
    assert a.active is False and a.cookie == "new-cookie"


@pytest.mark.skipif(not HAS_CRYPTO, reason="cryptography не установлен")
def test_cookie_encryption_roundtrip():
    key = CookieCrypto.generate_key()
    s = Store(fernet_key=key)
    aid = s.add_account("f", "secret-cookie", "UA")
    # в сырой строке БД куки НЕ должно быть в открытом виде
    raw = s.db.execute("SELECT cookie, enc FROM accounts WHERE id=?", (aid,)).fetchone()
    assert raw["enc"] == 1
    assert "secret-cookie" not in raw["cookie"]
    # но через API возвращается расшифрованной
    assert s.get_account(aid).cookie == "secret-cookie"


def test_daily_runs_dedup_and_summary():
    s = Store()
    aid = s.add_account("f", "c", "UA")
    assert s.was_done(aid, "mine", "2026-07-26") is False
    s.record_run(aid, "mine", "2026-07-26", "done", reward=120)
    s.record_run(aid, "game", "2026-07-26", "done", reward=60)
    assert s.was_done(aid, "mine", "2026-07-26") is True
    summ = s.farm_summary("2026-07-26")
    assert summ == {"mine": 120, "game": 60}


def test_decisions_log_and_counts():
    s = Store()
    s.log_decision(1, "ACCEPT", "выгодно")
    s.log_decision(2, "REJECT", "дорогая")
    s.log_decision(3, "REJECT", "ПП")
    counts = s.decision_counts()
    assert counts == {"ACCEPT": 1, "REJECT": 2}


def test_protected_cards():
    s = Store()
    s.protect(100, "нужная")
    s.protect(200)
    assert s.protected_ids() == {100, 200}
    s.unprotect(100)
    assert s.protected_ids() == {200}


def test_quiz_answers():
    s = Store()
    assert s.get_quiz_answer("2+2?") is None
    s.set_quiz_answer("2+2?", "4")
    assert s.get_quiz_answer("2+2?") == "4"
