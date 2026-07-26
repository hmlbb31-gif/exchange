"""База данных карт: ранг (из каталога) + рыночные сигналы (из MarketTracker).

Ранг карты — из каталога /cards (data-rank). Цена/лоты/«Хочу»/заявки — из
MarketTracker (mb/market.py), который копит историю цен сэмплированием.

По умолчанию, пока карта не наполнена, get() возвращает known=False, и движок
из-за fail-closed делает SKIP — безопасное поведение по умолчанию.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mb.market import MarketTracker, parse_request_count, parse_want_count
from mb.models import CardInfo

# ранг из каталога: data-rank="X" ... data-card-id="123" (в любом порядке)
CATALOG_RANK_RE = re.compile(
    r'data-rank="([^"]+)"[^>]*data-card-id="(\d+)"'
    r'|data-card-id="(\d+)"[^>]*data-rank="([^"]+)"',
    re.I | re.S,
)


def parse_catalog_ranks(html: str) -> dict[int, str]:
    """cardId -> ранг со страницы каталога /cards."""
    out: dict[int, str] = {}
    for m in CATALOG_RANK_RE.finditer(html):
        if m.group(1):
            out[int(m.group(2))] = m.group(1)
        else:
            out[int(m.group(3))] = m.group(4)
    return out


def load_rank_db(path: str | Path) -> dict[int, str]:
    """Загрузить базу рангов из card_ranks.json (формат rank -> [card_id]).

    Возвращает cardId -> ранг. Служебные ключи (начинаются с '_') игнорируются.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    for rank, ids in data.items():
        if rank.startswith("_") or not isinstance(ids, list):
            continue
        for cid in ids:
            out[int(cid)] = rank.upper()
    return out


class CardStore:
    """Кэш данных о картах: ранги + protection + рыночные сигналы из трекера."""

    def __init__(self, ranks: dict[int, str] | None = None,
                 protected: set[int] | None = None,
                 tracker: MarketTracker | None = None):
        self._rank: dict[int, str] = dict(ranks or {})
        self._protected: set[int] = set(protected or set())
        self.tracker = tracker or MarketTracker()

    def set_rank(self, card_id: int, rank: str) -> None:
        self._rank[card_id] = rank

    def load_catalog(self, html: str) -> int:
        got = parse_catalog_ranks(html)
        self._rank.update(got)
        return len(got)

    def protect(self, card_id: int) -> None:
        self._protected.add(card_id)

    def unprotect(self, card_id: int) -> None:
        self._protected.discard(card_id)

    def get(self, card_id: int) -> CardInfo:
        rep = self.tracker.price_report(card_id)
        return CardInfo(
            card_id=card_id,
            rank=self._rank.get(card_id),
            price=self.tracker.reference_price(card_id),
            want_count=rep["want_count"],
            lot_count=rep["active_lots"] or None,
            request_count=rep["request_count"],
            protected=card_id in self._protected,
        )
