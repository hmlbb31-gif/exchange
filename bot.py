"""Бот-АВТООБМЕНЩИК — самодостаточный проект (1 аккаунт, кука из .env).

Только обмены. Fail-closed + анти-скам + человеческий трафик. Пока DRY_RUN=1 —
только логирует и уведомляет, ничего не жмёт. Фарма здесь нет (отдельный проект).

Запуск (Python 3.12/3.13 — aiogram не собирается на 3.14):
    pip install -r requirements-bot.txt
    # .env: EXCHANGE_BOT_TOKEN (или BOT_TOKEN), ADMIN_IDS, MB_COOKIE/MB_COOKIE_FILE,
    #       MB_USER_AGENT, FERNET_KEY, DRY_RUN
    python -m bot            # (запускать из папки exchange/)  либо  python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import Config
from mb.cards import CardStore, load_rank_db
from mb.client import MangaBuffClient
from mb.cookies import load_cookie
from mb.storage import Store
from mb.trader import Trader
from mb.worker import TradeLoop

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parent / "data"
RANK_DB = DATA_DIR / "card_ranks.json"


class ExchangeApp:
    def __init__(self):
        self.cfg = Config.from_env()
        self.token = os.getenv("EXCHANGE_BOT_TOKEN", "") or self.cfg.bot_token
        self.admin_ids = self.cfg.admin_ids
        DATA_DIR.mkdir(exist_ok=True)
        self.store = Store(DATA_DIR / "trade.sqlite3", fernet_key=self.cfg.fernet_key)
        self.cfg.cookie = self.cfg.cookie or load_cookie(cookie_file=self.cfg.cookie_file)
        ranks = load_rank_db(RANK_DB) if RANK_DB.exists() else {}
        self.cards = CardStore(ranks=ranks, protected=self.store.protected_ids())
        self.rules = self.cfg.rules
        self.trade_on = False
        self.loop: TradeLoop | None = None
        self.bot: Bot | None = None
        self._ranks = len(ranks)

    def is_admin(self, uid: int | None) -> bool:
        return bool(uid) and (not self.admin_ids or uid in self.admin_ids)

    async def notify(self, text: str) -> None:
        if not self.bot:
            return
        for admin in self.admin_ids or ():
            try:
                await self.bot.send_message(admin, text)
            except Exception:  # noqa: BLE001
                log.warning("не доставлено уведомление %s", admin)


def build_dispatcher(app: ExchangeApp) -> Dispatcher:
    dp = Dispatcher()

    def ok(m: Message) -> bool:
        return app.is_admin(m.from_user.id if m.from_user else None)

    @dp.message(Command("start", "help"))
    async def _help(msg: Message):
        if not ok(msg):
            return
        await msg.answer(
            "🤝 Бот-обменщик MangaBuff.\n\n"
            "/status — режим и состояние\n"
            "/trade on|off — вкл/выкл авто-обмен\n"
            "/run — один проход прямо сейчас\n"
            "/rules — пороги решений\n"
            "/protect <cardId> [причина] · /unprotect <cardId>\n"
            "/stats — сводка решений")

    @dp.message(Command("status"))
    async def _status(msg: Message):
        if not ok(msg):
            return
        await msg.answer(
            f"Обмен: {'ВКЛ' if app.trade_on else 'выкл'}\n"
            f"DRY_RUN: {'да (ничего не жмётся)' if app.cfg.dry_run else 'НЕТ — LIVE!'}\n"
            f"Рангов в базе: {app._ranks}\n"
            f"Защищённых карт: {len(app.cards._protected)}")

    @dp.message(Command("trade"))
    async def _trade(msg: Message, command: CommandObject):
        if not ok(msg):
            return
        arg = (command.args or "").strip().lower()
        if arg not in ("on", "off"):
            await msg.answer("Использование: /trade on|off")
            return
        app.trade_on = arg == "on"
        warn = "" if (app.cfg.dry_run or not app.trade_on) else " ⚠️ LIVE — обмены будут приниматься!"
        await msg.answer(f"Авто-обмен {'включён' if app.trade_on else 'выключен'}.{warn}")

    @dp.message(Command("run"))
    async def _run(msg: Message):
        if not ok(msg):
            return
        if not app.loop:
            await msg.answer("Воркёр ещё не готов.")
            return
        try:
            outs = await app.loop.run_once()
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"Сбой прохода: {e}")
            return
        if not outs:
            await msg.answer("Входящих обменов нет.")
            return
        await msg.answer("Проход:\n" + "\n".join(
            f"#{o.trade.trade_id} {o.trade.partner_name}: {o.evaluation.verdict.value.upper()}"
            for o in outs))

    @dp.message(Command("rules"))
    async def _rules(msg: Message):
        if not ok(msg):
            return
        r = app.rules
        await msg.answer(
            f"Правило выгоды: ×{r.min_gain_ratio} по рангу.\n"
            f"Защищённые ранги: {', '.join(sorted(r.protected_ranks))}\n"
            f"Strict-ранги: {', '.join(sorted(r.strict_ranks))}\n"
            f"Порог дорогой: {r.expensive_price} (strict {r.strict_expensive_price})\n"
            f"ПП: Хочу≥{r.pp_want_threshold}&лоты≤{r.pp_low_supply_lots}, "
            f"либо заявки≥{r.pp_request_threshold}\n"
            f"Защищённых карт: {len(app.cards._protected)}")

    @dp.message(Command("protect"))
    async def _protect(msg: Message, command: CommandObject):
        if not ok(msg):
            return
        args = (command.args or "").split(maxsplit=1)
        if not args or not args[0].isdigit():
            await msg.answer("Использование: /protect <cardId> [причина]")
            return
        cid = int(args[0])
        app.store.protect(cid, args[1] if len(args) > 1 else "")
        app.cards.protect(cid)
        await msg.answer(f"Карта {cid} защищена.")

    @dp.message(Command("unprotect"))
    async def _unprotect(msg: Message, command: CommandObject):
        if not ok(msg):
            return
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await msg.answer("Использование: /unprotect <cardId>")
            return
        cid = int(arg)
        app.store.unprotect(cid)
        app.cards.unprotect(cid)
        await msg.answer(f"Карта {cid} снята с защиты.")

    @dp.message(Command("stats"))
    async def _stats(msg: Message):
        if not ok(msg):
            return
        counts = app.store.decision_counts()
        await msg.answer("Решения: " + (", ".join(f"{k}: {v}" for k, v in counts.items()) or "нет"))

    return dp


async def trade_worker(app: ExchangeApp) -> None:
    async with MangaBuffClient(app.cfg) as client:
        trader = Trader(client, app.cards, app.rules)
        app.loop = TradeLoop(trader, app.store, notifier=app.notify)
        while True:
            if app.trade_on:
                try:
                    await app.loop.run_once()
                except Exception as e:  # noqa: BLE001
                    await app.notify(f"⚠️ Сбой обменника: {e}")
            await asyncio.sleep(app.loop.cfg.interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    app = ExchangeApp()
    if not app.token:
        raise SystemExit("Нет EXCHANGE_BOT_TOKEN (или BOT_TOKEN) в .env")
    if not app.cfg.cookie:
        raise SystemExit("Нет куки обменника: MB_COOKIE или cookies.txt")
    app.bot = Bot(app.token)
    dp = build_dispatcher(app)
    asyncio.create_task(trade_worker(app))
    await app.notify("🤝 Бот-обменщик запущен. Авто-обмен выключен — /trade on.")
    await dp.start_polling(app.bot)


if __name__ == "__main__":
    asyncio.run(main())
