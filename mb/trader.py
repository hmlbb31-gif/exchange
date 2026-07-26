"""Оркестрация обменника: опросить входящие -> оценить -> (в live) принять/отклонить.

По умолчанию config.dry_run=True: бот только логирует решения, ничего не жмёт.
Это обязательный этап перед боевым режимом — сверить решения на реальных трейдах.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from mb.cards import CardStore
from mb.client import MangaBuffClient
from mb.decision import evaluate_trade
from mb.market import (MarketSnapshot, parse_card_market, parse_request_count,
                       parse_want_count)
from mb.models import Evaluation, Trade, Verdict
from mb.parser import parse_trade_detail, parse_trades_list

log = logging.getLogger(__name__)


def _signature(trade: Trade) -> tuple:
    """Отпечаток состава обмена: отсортированные (card_id, экземпляр) обеих сторон.

    Используется для анти-скам сверки перед accept: если состав страницы обмена
    изменился между оценкой и приёмом (подмена в последний момент) — не принимаем.
    """
    give = sorted((c.card_id, c.copy_number) for c in trade.give)
    recv = sorted((c.card_id, c.copy_number) for c in trade.receive)
    return (tuple(give), tuple(recv))


@dataclass
class TradeOutcome:
    trade: Trade
    evaluation: Evaluation
    acted: bool          # выполнено ли действие (в live-режиме)
    result: dict | None  # ответ сайта, если действовали


class Trader:
    def __init__(self, client: MangaBuffClient, store: CardStore, rules):
        self.client = client
        self.store = store
        self.rules = rules

    async def _enrich(self, trade: Trade) -> None:
        """Снять свежий снимок рынка по каждой карте обмена и записать в трекер.

        Каждый снимок пополняет историю цен (и выводит проданные лоты).
        """
        ttl = getattr(self.client.config, "market_ttl", 0.0) or 0.0
        card_ids = {c.card_id for c in trade.give} | {c.card_id for c in trade.receive}
        for cid in card_ids:
            # TTL-кэш: не перезапрашивать одну карту чаще, чем раз в ttl секунд
            # (меньше трафика на сервер = менее заметно).
            if ttl:
                last = self.store.tracker.last_snapshot_ts(cid)
                if last is not None and (time.time() - last) < ttl:
                    continue
            try:
                market_html = await self.client.fetch_market_card(cid)
                snap = parse_card_market(market_html, cid)
                snap.want_count = parse_want_count(await self.client.fetch_offers_want(cid))
                snap.request_count = parse_request_count(await self.client.fetch_requests(cid))
                self.store.tracker.ingest(snap)
            except Exception as e:  # noqa: BLE001
                log.warning("не удалось обогатить карту %s: %s", cid, e)

    async def _still_same(self, trade: Trade) -> bool:
        """Перечитать /trades/{id} и убедиться, что состав обмена не изменился."""
        try:
            fresh = parse_trade_detail(await self.client.fetch_trade_detail(trade.trade_id))
        except Exception as e:  # noqa: BLE001 — не смогли подтвердить = не принимаем
            log.warning("не удалось перепроверить обмен %s: %s", trade.trade_id, e)
            return False
        return _signature(fresh) == _signature(trade)

    async def process_once(self, *, enrich: bool = True) -> list[TradeOutcome]:
        """Один проход по входящим обменам."""
        html = await self.client.fetch_trades_list()
        rows = parse_trades_list(html)
        outcomes: list[TradeOutcome] = []

        for trade_id, _name in rows:
            detail = await self.client.fetch_trade_detail(trade_id)
            trade = parse_trade_detail(detail)
            if enrich:
                await self._enrich(trade)

            ev = evaluate_trade(trade, self.store.get, self.rules)
            acted, result = False, None

            if ev.verdict == Verdict.ACCEPT:
                # АНТИ-СКАМ (TOCTOU): перечитать страницу обмена ПРЯМО перед приёмом
                # и сверить состав. Если что-то подменили между оценкой и accept —
                # не принимаем, помечаем SKIP.
                if not await self._still_same(trade):
                    ev.verdict = Verdict.SKIP
                    ev.add("Состав обмена изменился перед приёмом — возможная подмена. SKIP.")
                else:
                    result = await self.client.accept_trade(trade.trade_id)
                    acted = True
            elif ev.verdict == Verdict.REJECT:
                # отклонять на автомате — по желанию; по умолчанию в dry_run просто лог
                result = await self.client.reject_trade(trade.trade_id)
                acted = True
            # SKIP — ничего не трогаем

            log.info(
                "trade %s: %s | %s", trade.trade_id, ev.verdict.value,
                " ; ".join(ev.reasons),
            )
            outcomes.append(TradeOutcome(trade, ev, acted, result))

        return outcomes
