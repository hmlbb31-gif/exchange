"""Фоновые воркёры: опрос обменов (1 аккаунт) + дневной фарм (мультиаккаунт).

Обменник крутится как «человек»: интервал опроса с джиттером, тихие часы (ночью
не активничаем), устойчивость к сбоям (сбой одного прохода не роняет цикл).
Решения пишутся в storage.Store для /stats, важные события уходят в notifier —
async-колбэк, который бот подключает к телеграму (воркёр про aiogram не знает).

DRY_RUN уважается ниже по стеку (клиент не шлёт accept/reject в сухом прогоне).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable

from mb.client import SessionDead, config_for_account
from mb.models import Verdict

log = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]


async def _noop(_msg: str) -> None:
    return None


def is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """Тихие часы [start, end). Поддерживает переход через полночь (напр. 1..7)."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # через полночь


@dataclass
class TradeLoopConfig:
    interval: float = 45.0          # базовый интервал опроса, сек
    jitter: float = 20.0            # случайная добавка [0, jitter]
    dead_backoff: float = 600.0     # пауза после «кука протухла»
    quiet_start: int = 2            # тихие часы: с 02:00
    quiet_end: int = 8              # до 08:00


class TradeLoop:
    """Цикл обменника на ОДНОМ аккаунте."""

    def __init__(self, trader, store, notifier: Notifier = _noop,
                 cfg: TradeLoopConfig | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = datetime.now,
                 rng: random.Random | None = None):
        self.trader = trader            # mb.trader.Trader (или совместимый)
        self.store = store              # mb.storage.Store
        self.notify = notifier
        self.cfg = cfg or TradeLoopConfig()
        self._sleep = sleep
        self._now = now
        self._rng = rng or random.Random()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run_once(self) -> list:
        """Один проход: опросить, оценить, записать решения, уведомить о приёмах."""
        outcomes = await self.trader.process_once()
        for o in outcomes:
            self.store.log_decision(
                o.trade.trade_id, o.evaluation.verdict.value,
                "; ".join(o.evaluation.reasons))
            if o.evaluation.verdict == Verdict.ACCEPT:
                await self.notify(
                    f"✅ Принят обмен #{o.trade.trade_id} от {o.trade.partner_name}: "
                    f"получаю {len(o.trade.receive)}, отдаю {len(o.trade.give)}")
        return outcomes

    async def run_forever(self) -> None:
        while not self._stop:
            hour = self._now().hour
            if not is_quiet_hour(hour, self.cfg.quiet_start, self.cfg.quiet_end):
                try:
                    await self.run_once()
                except SessionDead:
                    await self.notify("⚠️ Сессия обменника протухла — обнови куку.")
                    await self._sleep(self.cfg.dead_backoff)
                    continue
                except Exception as e:  # noqa: BLE001
                    log.exception("сбой прохода обменника")
                    await self.notify(f"⚠️ Сбой обменника: {e}")
            await self._sleep(self.cfg.interval + self._rng.uniform(0, self.cfg.jitter))


async def daily_farm_round(base_config, db, farm_cls, client_cls,
                           notifier: Notifier = _noop, day: str | None = None,
                           farm_cfg=None) -> dict[int, list]:
    """Прогнать дэйлики по всем активным фарм-аккаунтам (каждый — свой клиент/прокси).

    farm_cls  — mb.farm.FarmRunner; client_cls — mb.client.MangaBuffClient
    (инъекция для тестируемости). Возвращает account_id -> список FarmResult.
    """
    from mb.storage import ROLE_FARM
    day = day or date.today().isoformat()
    results: dict[int, list] = {}
    for acc in db.list_accounts(role=ROLE_FARM, active_only=True):
        cfg = config_for_account(base_config, acc)
        try:
            async with client_cls(cfg) as client:
                runner = farm_cls(client, db, acc.id, cfg=farm_cfg)
                res = await runner.run_all(day)
            db.mark_ok(acc.id)
            results[acc.id] = res
            got = sum(r.reward for r in res)
            await notifier(f"🌾 Фарм [{acc.label}]: +{got} за день")
        except SessionDead:
            db.set_active(acc.id, False)
            await notifier(f"⚠️ Аккаунт [{acc.label}] — кука протухла, отключён от фарма.")
        except Exception as e:  # noqa: BLE001
            log.exception("сбой фарма аккаунта %s", acc.id)
            await notifier(f"⚠️ Сбой фарма [{acc.label}]: {e}")
    return results
