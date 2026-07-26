"""Сухой прогон обменника: один проход по входящим трейдам с выводом решений.

НИЧЕГО не принимает и не отклоняет (DRY_RUN=1). Нужен для проверки, что бот
оценивает реальные обмены так, как ты ожидаешь, ПЕРЕД боевым режимом.

Запуск:
  1. cp .env.example .env  и заполнить BOT_TOKEN не нужен, нужны MB_COOKIE/MB_COOKIE_FILE и MB_USER_AGENT
  2. pip install -r requirements.txt
  3. python run_dry.py
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import Config
from mb.cards import CardStore, load_rank_db
from mb.client import MangaBuffClient
from mb.cookies import load_cookie
from mb.trader import Trader

RANK_DB = Path(__file__).parent / "data" / "card_ranks.json"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.from_env()
    cfg.dry_run = True  # жёстко: этот скрипт никогда не действует
    cfg.cookie = cfg.cookie or load_cookie(cookie_file=cfg.cookie_file)

    # Загрузить собранную базу рангов из каталога — БЕЗ неё все карты SKIP.
    ranks: dict[int, str] = {}
    if RANK_DB.exists():
        ranks = load_rank_db(RANK_DB)
        logging.info("Загружено рангов: %d карт из %s", len(ranks), RANK_DB.name)
    else:
        logging.warning("%s не найден — все карты будут SKIP. Запусти crawl_ranks.py",
                        RANK_DB.name)

    store = CardStore(ranks=ranks)  # цены наполнятся с рынка по ходу
    async with MangaBuffClient(cfg) as client:
        trader = Trader(client, store, cfg.rules)
        outcomes = await trader.process_once()

    print(f"\n=== Итог сухого прогона: {len(outcomes)} обмен(ов) ===")
    for o in outcomes:
        t = o.trade
        print(f"\n#{t.trade_id} от {t.partner_name}: "
              f"получаю {len(t.receive)}, отдаю {len(t.give)} -> {o.evaluation.verdict.value.upper()}")
        for r in o.evaluation.reasons:
            print("   -", r)


if __name__ == "__main__":
    asyncio.run(main())
