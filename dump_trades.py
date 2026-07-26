"""Диагностика: сохраняет сырой HTML /trades в файл, ничего не жмёт.

Запуск:  python dump_trades.py
Потом загрузи trades_dump.html в чат — сверю с парсером.
"""

from __future__ import annotations

import asyncio

from config import Config
from mb.client import MangaBuffClient
from mb.cookies import load_cookie


async def main() -> None:
    cfg = Config.from_env()
    cfg.cookie = cfg.cookie or load_cookie(cookie_file=cfg.cookie_file)
    async with MangaBuffClient(cfg) as client:
        html = await client.get("/trades")  # get() возвращает уже текст
    with open("trades_dump.html", "w", encoding="utf-8") as f:
        f.write(html)
    # быстрые подсказки: залогинены ли мы и есть ли строки обменов
    print(f"saved trades_dump.html: {len(html)} bytes")
    print("login-форма на странице:" , "/login" in html or "Войти" in html or "authorization" in html.lower())
    print("вхождений 'trade__list-item':", html.count("trade__list-item"))
    print("вхождений 'trade__' (любых):", html.count("trade__"))


if __name__ == "__main__":
    asyncio.run(main())
