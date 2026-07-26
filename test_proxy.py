"""Тесты прокси на аккаунт и health-check сессии."""

import asyncio

from config import Config
from mb.client import MangaBuffClient, SessionDead, config_for_account
from mb.storage import ROLE_FARM, Store


def test_config_for_account_sets_cookie_ua_proxy():
    base = Config(dry_run=True, requests_per_minute=5)
    store = Store()
    aid = store.add_account("f1", "sess=1", "UA/farm",
                            proxy="http://u:p@1.2.3.4:8080", role=ROLE_FARM)
    acc = store.get_account(aid)
    cfg = config_for_account(base, acc)
    assert cfg.cookie == "sess=1"
    assert cfg.user_agent == "UA/farm"
    assert cfg.proxy == "http://u:p@1.2.3.4:8080"
    # base не мутировали, прочие поля унаследованы
    assert base.proxy is None
    assert cfg.requests_per_minute == 5 and cfg.dry_run is True


def test_client_enters_with_proxy_without_error():
    # httpx не подключается к прокси до первого запроса — вход в контекст безопасен
    cfg = Config(proxy="http://127.0.0.1:9999")

    async def go():
        async with MangaBuffClient(cfg) as c:
            return c._client is not None

    assert asyncio.run(go()) is True


def test_health_check_true_and_false():
    c = MangaBuffClient(Config())

    async def dead(_path):
        raise SessionDead("/login")

    c.get = dead  # type: ignore
    assert asyncio.run(c.health_check()) is False

    async def ok(_path):
        return "<html>ok</html>"

    c.get = ok  # type: ignore
    c.session_alive = True
    assert asyncio.run(c.health_check()) is True
