"""Бот-АВТООБМЕНЩИК — самодостаточный проект (1 аккаунт).

Управление ПОЛНОСТЬЮ кнопочное: команд вводить не нужно. Нажал «Привязать
аккаунт» → прислал строку cookie одним сообщением → бот проверил и сохранил
(зашифровано) → аккаунт привязан. Кука хранится в БД, поэтому переживает
перезапуск: .env с кукой больше не обязателен (нужен только токен и ADMIN_IDS).

Только обмены. Fail-closed + анти-скам + человеческий трафик. Пока DRY_RUN=1 —
только логирует и уведомляет, ничего не жмёт. Фарма здесь нет (отдельный проект).

Запуск (Python 3.12/3.13 — aiogram не собирается на 3.14):
    pip install -r requirements-bot.txt
    # .env: EXCHANGE_BOT_TOKEN (или BOT_TOKEN), ADMIN_IDS, FERNET_KEY, DRY_RUN
    #       (MB_COOKIE больше не обязателен — куку привязываешь кнопкой)
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from mb.cards import CardStore, load_rank_db
from mb.client import MangaBuffClient, RateLimited, SessionDead
from mb.cookies import load_cookie, parse_netscape_cookies
from mb.storage import ROLE_TRADE, Store
from mb.trader import Trader
from mb.worker import TradeLoop

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parent / "data"
RANK_DB = DATA_DIR / "card_ranks.json"

# Числовые правила, которые можно править из ТГ: имя поля Rules -> преобразователь.
RULE_FIELDS: dict[str, type] = {
    "min_gain_ratio": float,
    "basket_guard_ratio": float,
    "protect_copy_number_below": int,
    "expensive_price": float,
    "strict_expensive_price": float,
    "pp_want_threshold": int,
    "pp_low_supply_lots": int,
    "pp_request_threshold": int,
}
# Человекочитаемые подписи для меню правил.
RULE_LABELS: dict[str, str] = {
    "min_gain_ratio": "Выгода ×",
    "basket_guard_ratio": "Анти-разбавление ×",
    "protect_copy_number_below": "Беречь копии ≤",
    "expensive_price": "Порог «дорогая»",
    "strict_expensive_price": "Порог strict",
    "pp_want_threshold": "ПП: Хочу ≥",
    "pp_low_supply_lots": "ПП: лоты ≤",
    "pp_request_threshold": "ПП: заявки ≥",
}


class ExchangeApp:
    def __init__(self):
        self.cfg = Config.from_env()
        self.token = os.getenv("EXCHANGE_BOT_TOKEN", "") or self.cfg.bot_token
        self.admin_ids = self.cfg.admin_ids
        DATA_DIR.mkdir(exist_ok=True)
        self.store = Store(DATA_DIR / "trade.sqlite3", fernet_key=self.cfg.fernet_key)

        # Кука: приоритет у сохранённой в БД (привязана кнопкой ранее), затем
        # .env / cookies.txt — чтобы старый способ тоже работал при первом старте.
        stored = self.store.list_accounts(role=ROLE_TRADE, active_only=True)
        env_cookie = self.cfg.cookie or load_cookie(cookie_file=self.cfg.cookie_file)
        if stored:
            self.cfg.cookie = stored[0].cookie
            if stored[0].user_agent:
                self.cfg.user_agent = stored[0].user_agent
        else:
            self.cfg.cookie = env_cookie

        ranks = load_rank_db(RANK_DB) if RANK_DB.exists() else {}
        self.cards = CardStore(ranks=ranks, protected=self.store.protected_ids())
        self.rules = self.cfg.rules
        self.trade_on = False
        self.loop: TradeLoop | None = None
        self.bot: Bot | None = None
        self.worker_task: asyncio.Task | None = None
        # user_id -> что ждём от него следующим сообщением:
        #   "cookie"        — строку cookie
        #   "ua"            — новый User-Agent
        #   "wl_add"        — id карт(ы) для защиты
        #   "wl_del"        — id карт(ы) снять с защиты
        #   f"rule:<name>"  — новое значение правила
        self.awaiting: dict[int, str] = {}
        self._ranks = len(ranks)

        self._load_settings()

    # --- персистентные настройки (переживают рестарт) ---
    def _load_settings(self) -> None:
        s = self.store.all_settings()
        if "dry_run" in s:
            self.cfg.dry_run = s["dry_run"] == "1"
        if s.get("user_agent"):
            self.cfg.user_agent = s["user_agent"]
        if "protect_palindrome" in s:
            self.rules.protect_palindrome = s["protect_palindrome"] == "1"
        # числовые правила
        for key, cast in RULE_FIELDS.items():
            if key in s:
                try:
                    setattr(self.rules, key, cast(s[key]))
                except (ValueError, TypeError):
                    pass

    def set_dry_run(self, value: bool) -> None:
        self.cfg.dry_run = value
        self.store.set_setting("dry_run", "1" if value else "0")

    def set_user_agent(self, ua: str) -> None:
        self.cfg.user_agent = ua
        self.store.set_setting("user_agent", ua)
        # обновим UA и у сохранённого trade-аккаунта
        for a in self.store.list_accounts(role=ROLE_TRADE):
            self.store.db.execute("UPDATE accounts SET user_agent=? WHERE id=?",
                                  (ua, a.id))
        self.store.db.commit()

    def set_rule(self, name: str, raw: str) -> None:
        cast = RULE_FIELDS[name]
        setattr(self.rules, name, cast(raw))
        self.store.set_setting(name, str(getattr(self.rules, name)))

    def reload_protected(self) -> None:
        """Пересобрать whitelist в CardStore из БД (после add/remove)."""
        self.cards._protected = self.store.protected_ids()

    @property
    def linked(self) -> bool:
        return bool(self.cfg.cookie)

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

    def save_trade_cookie(self, cookie: str) -> None:
        """Сохранить/обновить куку trade-аккаунта в БД (шифрованно, если есть ключ)."""
        accts = self.store.list_accounts(role=ROLE_TRADE)
        if accts:
            self.store.update_cookie(accts[0].id, cookie)
            self.store.set_active(accts[0].id, True)
        else:
            self.store.add_account("trade", cookie, self.cfg.user_agent,
                                   role=ROLE_TRADE)

    async def start_worker(self) -> None:
        """(Пере)запустить фоновый воркёр обменника под текущую куку."""
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.worker_task = None
        if self.linked:
            self.worker_task = asyncio.create_task(trade_worker(self))


# ------------------------- Кнопки и экраны -------------------------

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_kb(app: ExchangeApp) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if app.trade_on:
        rows.append([_btn("⏸ Выключить обмен", "trade_off")])
    else:
        rows.append([_btn("▶️ Включить обмен", "trade_on")])
    # Переключатель режима сразу в меню, чтобы видеть текущий и менять одним тапом.
    if app.cfg.dry_run:
        rows.append([_btn("🧪 Режим: СУХОЙ → включить LIVE", "mode_live")])
    else:
        rows.append([_btn("🟢 Режим: LIVE → вернуть сухой", "mode_dry")])
    rows.append([_btn("🔁 Обновить куку" if app.linked else "🔗 Привязать аккаунт",
                      "link")])
    rows.append([_btn("🔄 Один проход", "run"), _btn("📊 Статус", "status")])
    rows.append([_btn("⚙️ Правила", "rules"), _btn("🛡 Whitelist", "wl")])
    rows.append([_btn("🕵️ User-Agent", "ua"), _btn("📈 Статистика", "stats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ В меню", "menu")]])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "menu")]])


def menu_text(app: ExchangeApp) -> str:
    linked = "✅ привязан" if app.linked else "❌ не привязан"
    mode = "🟢 БОЕВОЙ (LIVE — обмены жмутся!)" if not app.cfg.dry_run \
        else "🧪 сухой (dry-run, ничего не жмётся)"
    trade = "ВКЛ" if app.trade_on else "выкл"
    return (
        "🤝 <b>Бот-обменщик MangaBuff</b>\n\n"
        f"Аккаунт: <b>{linked}</b>\n"
        f"Авто-обмен: <b>{trade}</b>\n"
        f"Режим: {mode}\n"
        f"Рангов в базе: {app._ranks}\n\n"
        "Управляй кнопками ниже 👇"
    )


def status_text(app: ExchangeApp) -> str:
    return (
        "📊 <b>Статус</b>\n\n"
        f"Аккаунт: {'✅ привязан' if app.linked else '❌ не привязан'}\n"
        f"Авто-обмен: {'ВКЛ' if app.trade_on else 'выкл'}\n"
        f"DRY_RUN: {'да (ничего не жмётся)' if app.cfg.dry_run else 'НЕТ — LIVE!'}\n"
        f"Рангов в базе: {app._ranks}\n"
        f"Защищённых карт: {len(app.cards._protected)}"
    )


def rules_text(app: ExchangeApp) -> str:
    r = app.rules
    per_rank = ", ".join(f"{k}×{v}" for k, v in r.min_gain_ratio_by_rank.items()) or "—"
    return (
        "⚙️ <b>Правила решений</b>\n\n"
        f"Выгода: ×{r.min_gain_ratio} по рангу\n"
        f"Пороги по рангам: {per_rank}\n"
        f"Анти-разбавление: ×{r.basket_guard_ratio}\n"
        f"Защита палиндромов: {'да' if r.protect_palindrome else 'нет'}\n"
        f"Беречь копии ≤: {r.protect_copy_number_below or '—'}\n"
        f"Защищённые ранги: {', '.join(sorted(r.protected_ranks))}\n"
        f"Strict-ранги: {', '.join(sorted(r.strict_ranks))}\n"
        f"Порог дорогой: {r.expensive_price} (strict {r.strict_expensive_price})\n"
        f"ПП: Хочу≥{r.pp_want_threshold}&лоты≤{r.pp_low_supply_lots}, "
        f"либо заявки≥{r.pp_request_threshold}\n"
        f"Защищённых карт: {len(app.cards._protected)}"
    )


def stats_text(app: ExchangeApp) -> str:
    counts = app.store.decision_counts()
    body = ", ".join(f"{k}: {v}" for k, v in counts.items()) or "пока нет"
    return f"📈 <b>Статистика решений</b>\n\n{body}"


# --- Экран редактирования правил ---

def rules_edit_kb(app: ExchangeApp) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for name, label in RULE_LABELS.items():
        val = getattr(app.rules, name)
        rows.append([_btn(f"{label}: {val}", f"editrule:{name}")])
    # булев тумблер палиндромов
    pal = "вкл" if app.rules.protect_palindrome else "выкл"
    rows.append([_btn(f"Защита палиндромов: {pal}", "toggle_palindrome")])
    rows.append([_btn("⬅️ В меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rule_prompt(name: str, app: ExchangeApp) -> str:
    return (
        f"✏️ <b>{RULE_LABELS[name]}</b>\n\n"
        f"Текущее значение: <b>{getattr(app.rules, name)}</b>\n\n"
        "Пришли новое число одним сообщением."
    )


# --- Экран whitelist ---

def wl_text(app: ExchangeApp) -> str:
    ids = sorted(app.cards._protected)
    shown = ", ".join(str(i) for i in ids[:50]) or "пусто"
    more = f"\n…и ещё {len(ids) - 50}" if len(ids) > 50 else ""
    return (
        "🛡 <b>Защищённые карты (whitelist)</b>\n\n"
        "Эти карты НИКОГДА не отдаются автоматически.\n\n"
        f"Всего: <b>{len(ids)}</b>\n{shown}{more}"
    )


def wl_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➕ Добавить карты", "wl_add"), _btn("➖ Убрать карты", "wl_del")],
        [_btn("⬅️ В меню", "menu")],
    ])


def ua_text(app: ExchangeApp) -> str:
    return (
        "🕵️ <b>User-Agent</b>\n\n"
        "Cookie MangaBuff привязан к UA браузера, из которого экспортирован — "
        "он <b>должен совпадать</b>, иначе DDoS-Guard сбросит сессию.\n\n"
        f"Сейчас:\n<code>{app.cfg.user_agent}</code>"
    )


def ua_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Изменить UA", "ua_set")],
        [_btn("⬅️ В меню", "menu")],
    ])


LINK_PROMPT = (
    "🔗 <b>Привязка аккаунта</b>\n\n"
    "Пришли <b>одним сообщением</b> строку cookie от mangabuff.ru "
    "(из DevTools → Network → заголовок <code>Cookie</code>, либо экспорт "
    "cookies.txt).\n\n"
    "⚠️ Cookie привязан к User-Agent браузера, из которого экспортирован — "
    "он должен совпадать с настройкой бота.\n"
    "🔒 Сообщение с кукой я удалю сразу после проверки."
)


# ------------------------- Логика куки -------------------------

def _parse_ids(text: str) -> list[int]:
    """Вытащить целочисленные id карт из свободного текста (пробелы/запятые)."""
    out: list[int] = []
    for tok in (text or "").replace(",", " ").split():
        if tok.isdigit():
            out.append(int(tok))
    return out


def normalize_pasted_cookie(text: str) -> str:
    text = (text or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    if "\t" in text or "Netscape" in text:
        return parse_netscape_cookies(text)
    return text


async def validate_cookie(base: Config, cookie: str) -> tuple[bool, str]:
    """Проверить куку живым заходом на /trades. dry_run — ничего не жмём."""
    if not cookie or "=" not in cookie:
        return False, "это не похоже на строку cookie (нет пар name=value)"
    test_cfg = replace(base, cookie=cookie, dry_run=True)
    try:
        async with MangaBuffClient(test_cfg) as client:
            html = await client.fetch_trades_list()
    except SessionDead:
        return False, "сессия не залогинена (редирект на /login)"
    except RateLimited:
        return False, "сайт временно ограничил запросы (429) — попробуй позже"
    except Exception as e:  # noqa: BLE001
        return False, f"ошибка сети: {e}"
    if "csrf-token" not in html:
        return False, "страница обменов без csrf — вероятно, не залогинен"
    return True, ""


# ------------------------- Диспетчер -------------------------

def build_dispatcher(app: ExchangeApp) -> Dispatcher:
    dp = Dispatcher()

    def ok_msg(m: Message) -> bool:
        return app.is_admin(m.from_user.id if m.from_user else None)

    def ok_cb(c: CallbackQuery) -> bool:
        return app.is_admin(c.from_user.id if c.from_user else None)

    async def show_menu(target: Message | CallbackQuery) -> None:
        text, kb = menu_text(app), main_kb(app)
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(text, reply_markup=kb)
            except Exception:  # noqa: BLE001 — «message is not modified» и т.п.
                pass
        else:
            await target.answer(text, reply_markup=kb)

    @dp.message(Command("start", "menu", "help"))
    async def _start(msg: Message):
        if not ok_msg(msg):
            return
        app.awaiting.pop(msg.from_user.id, None)
        await show_menu(msg)

    @dp.callback_query(F.data == "menu")
    async def _menu(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.awaiting.pop(cb.from_user.id, None)
        await show_menu(cb)
        await cb.answer()

    @dp.callback_query(F.data == "status")
    async def _status(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        await cb.message.edit_text(status_text(app), reply_markup=back_kb())
        await cb.answer()

    @dp.callback_query(F.data == "rules")
    async def _rules(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        await cb.message.edit_text(rules_text(app) + "\n\n👇 Нажми поле, чтобы изменить",
                                   reply_markup=rules_edit_kb(app))
        await cb.answer()

    @dp.callback_query(F.data.startswith("editrule:"))
    async def _edit_rule(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        name = cb.data.split(":", 1)[1]
        if name not in RULE_FIELDS:
            return await cb.answer("Неизвестное правило", show_alert=True)
        app.awaiting[cb.from_user.id] = f"rule:{name}"
        await cb.message.edit_text(rule_prompt(name, app), reply_markup=cancel_kb())
        await cb.answer()

    @dp.callback_query(F.data == "toggle_palindrome")
    async def _toggle_pal(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.rules.protect_palindrome = not app.rules.protect_palindrome
        app.store.set_setting("protect_palindrome",
                              "1" if app.rules.protect_palindrome else "0")
        await cb.message.edit_text(rules_text(app) + "\n\n👇 Нажми поле, чтобы изменить",
                                   reply_markup=rules_edit_kb(app))
        await cb.answer("Готово.")

    @dp.callback_query(F.data.in_({"mode_live", "mode_dry"}))
    async def _mode(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.set_dry_run(cb.data == "mode_dry")
        if not app.cfg.dry_run:
            await cb.answer("⚠️ LIVE включён — обмены будут реально приниматься!",
                            show_alert=True)
        else:
            await cb.answer("Сухой режим: ничего не жмётся.")
        await show_menu(cb)

    @dp.callback_query(F.data == "wl")
    async def _wl(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        await cb.message.edit_text(wl_text(app), reply_markup=wl_kb())
        await cb.answer()

    @dp.callback_query(F.data.in_({"wl_add", "wl_del"}))
    async def _wl_edit(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.awaiting[cb.from_user.id] = cb.data
        verb = "добавить в защиту" if cb.data == "wl_add" else "снять с защиты"
        await cb.message.edit_text(
            f"Пришли id карт(ы), чтобы <b>{verb}</b>.\n"
            "Можно несколько через пробел или запятую (напр. <code>1234 5678</code>).",
            reply_markup=cancel_kb())
        await cb.answer()

    @dp.callback_query(F.data == "ua")
    async def _ua(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        await cb.message.edit_text(ua_text(app), reply_markup=ua_kb())
        await cb.answer()

    @dp.callback_query(F.data == "ua_set")
    async def _ua_set(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.awaiting[cb.from_user.id] = "ua"
        await cb.message.edit_text(
            "Пришли новую строку User-Agent одним сообщением.\n"
            "Скопируй её из того же браузера, где брал cookie "
            "(DevTools → Network → любой запрос → Request Headers → User-Agent).",
            reply_markup=cancel_kb())
        await cb.answer()

    @dp.callback_query(F.data == "stats")
    async def _stats(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        await cb.message.edit_text(stats_text(app), reply_markup=back_kb())
        await cb.answer()

    @dp.callback_query(F.data.in_({"trade_on", "trade_off"}))
    async def _trade(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        want_on = cb.data == "trade_on"
        if want_on and not app.linked:
            await cb.answer("Сначала привяжи аккаунт (кнопка «Привязать аккаунт»).",
                            show_alert=True)
            return
        app.trade_on = want_on
        if want_on and not app.cfg.dry_run:
            await cb.answer("⚠️ LIVE: обмены будут приниматься!", show_alert=True)
        else:
            await cb.answer("Готово.")
        await show_menu(cb)

    @dp.callback_query(F.data == "run")
    async def _run(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        if not app.linked:
            await cb.answer("Сначала привяжи аккаунт.", show_alert=True)
            return
        if not app.loop:
            await cb.answer("Воркёр ещё не готов, секунду…", show_alert=True)
            return
        await cb.answer("Опрашиваю обмены…")
        try:
            outs = await app.loop.run_once()
        except Exception as e:  # noqa: BLE001
            await cb.message.edit_text(f"❌ Сбой прохода: {e}", reply_markup=back_kb())
            return
        if not outs:
            body = "Входящих обменов нет."
        else:
            body = "\n".join(
                f"#{o.trade.trade_id} {o.trade.partner_name}: "
                f"{o.evaluation.verdict.value.upper()}" for o in outs)
        await cb.message.edit_text(f"🔄 <b>Проход</b>\n\n{body}", reply_markup=back_kb())

    @dp.callback_query(F.data == "link")
    async def _link(cb: CallbackQuery):
        if not ok_cb(cb):
            return await cb.answer()
        app.awaiting[cb.from_user.id] = "cookie"
        await cb.message.edit_text(LINK_PROMPT, reply_markup=cancel_kb())
        await cb.answer()

    # Единый приём текста от админа в режиме ожидания. Никаких команд — просто
    # вставил значение и всё. Что именно ждём — хранится в app.awaiting[uid].
    @dp.message(F.text & ~F.text.startswith("/"))
    async def _on_text(msg: Message):
        if not ok_msg(msg):
            return
        uid = msg.from_user.id
        kind = app.awaiting.get(uid)
        if not kind:
            await show_menu(msg)
            return
        app.awaiting.pop(uid, None)

        if kind == "cookie":
            await _handle_cookie(msg)
        elif kind == "ua":
            app.set_user_agent(msg.text.strip())
            await msg.answer("✅ User-Agent обновлён. При смене UA обычно нужно "
                             "заново привязать cookie из того же браузера.",
                             reply_markup=main_kb(app))
        elif kind in ("wl_add", "wl_del"):
            ids = _parse_ids(msg.text)
            if not ids:
                await msg.answer("Не нашёл ни одного id. Попробуй снова.",
                                 reply_markup=main_kb(app))
                return
            for cid in ids:
                if kind == "wl_add":
                    app.store.protect(cid, reason="из ТГ")
                else:
                    app.store.unprotect(cid)
            app.reload_protected()
            verb = "добавлены в защиту" if kind == "wl_add" else "сняты с защиты"
            await msg.answer(f"✅ {len(ids)} карт(ы) {verb}. "
                             f"Всего защищено: {len(app.cards._protected)}.",
                             reply_markup=main_kb(app))
        elif kind.startswith("rule:"):
            name = kind.split(":", 1)[1]
            try:
                app.set_rule(name, msg.text.strip().replace(",", "."))
            except (ValueError, TypeError):
                await msg.answer("Нужно число. Попробуй снова.",
                                 reply_markup=main_kb(app))
                return
            await msg.answer(
                f"✅ {RULE_LABELS.get(name, name)} = {getattr(app.rules, name)}",
                reply_markup=rules_edit_kb(app))
        else:
            await show_menu(msg)

    async def _handle_cookie(msg: Message):
        cookie = normalize_pasted_cookie(msg.text)
        # Убираем сообщение с кукой из чата (бот может удалять входящие в ЛС).
        try:
            await msg.delete()
        except Exception:  # noqa: BLE001
            pass
        status = await msg.answer("🔎 Проверяю куку…")
        good, err = await validate_cookie(app.cfg, cookie)
        if not good:
            await status.edit_text(
                f"❌ Кука не подошла: {err}\n\nНажми «Привязать аккаунт» и попробуй снова.",
                reply_markup=main_kb(app))
            return
        app.cfg.cookie = cookie
        app.save_trade_cookie(cookie)
        await app.start_worker()
        enc = "🔒 зашифрована" if app.store.crypto.enabled else "⚠️ без шифрования (задай FERNET_KEY)"
        await status.edit_text(
            f"✅ Аккаунт привязан! Кука сохранена ({enc}).\n"
            "Теперь можно включить авто-обмен.",
            reply_markup=main_kb(app))

    return dp


async def trade_worker(app: ExchangeApp) -> None:
    async with MangaBuffClient(app.cfg) as client:
        trader = Trader(client, app.cards, app.rules)
        app.loop = TradeLoop(trader, app.store, notifier=app.notify)
        while True:
            if app.trade_on:
                try:
                    await app.loop.run_once()
                except SessionDead:
                    await app.notify("⚠️ Сессия протухла — привяжи куку заново "
                                     "(кнопка «Обновить куку»).")
                    app.trade_on = False
                except Exception as e:  # noqa: BLE001
                    await app.notify(f"⚠️ Сбой обменника: {e}")
            await asyncio.sleep(app.loop.cfg.interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    app = ExchangeApp()
    if not app.token:
        raise SystemExit("Нет EXCHANGE_BOT_TOKEN (или BOT_TOKEN) в .env")
    app.bot = Bot(app.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(app)
    await app.start_worker()  # запустится только если кука уже привязана
    hint = "Аккаунт привязан." if app.linked else \
        "Аккаунт НЕ привязан — открой меню и нажми «Привязать аккаунт»."
    await app.notify(f"🤝 Бот-обменщик запущен. {hint}\nОткрой меню: /start")
    await dp.start_polling(app.bot)


if __name__ == "__main__":
    asyncio.run(main())
