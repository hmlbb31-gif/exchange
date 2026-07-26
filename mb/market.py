"""Понимание рынка mangabuff: парсинг лотов/заявок + история цен через сэмплирование.

Ключевой факт: у сайта НЕТ истории цен и нет JSON-API. Страница показывает только
ТЕКУЩИЕ активные лоты и заявки. Поэтому «понимание цены» строится так:

  1. Периодически снимаем снимок рынка карты: /market/card/{id} (лоты),
     /market/requests?card_id={id} (заявки), /cards/{id}/offers/want («Хочу»).
  2. Каждый лот имеет lot_id (из ссылки «Купить» /market/{lotId}). Мы храним,
     какие lot_id видели и по какой цене.
  3. Когда lot_id ИСЧЕЗАЕТ между снимками — считаем его ПРОДАННЫМ по последней
     виденной цене. Так, без всякого API, набираем историю продаж — ровно то,
     что показывает твой дашборд по «Химено» (проданные лоты, средняя/мин/последняя).
  4. Reference-цена карты = медиана активных S-лотов (устойчива к выбросам),
     подкреплённая историей продаж.

Цена в S и «карта за карту» (бартер) — это разные типы лотов. Для денежной
оценки берём только S-лоты; бартерные помечаем отдельно и в цену не мешаем.

ВАЖНО (fail-closed): цену в S берём ТОЛЬКО из явного ценового контейнера лота,
чтобы не перепутать «8 S» (цена) с «3 S» (три карты ранга S в бартере).
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import time
from dataclasses import dataclass, field

# --- разметка (снята живьём 2026-07-25) ---
LOT_BLOCK_RE = re.compile(r'market-show__item')
LOT_ID_RE = re.compile(r'href="/market/(\d+)"')
# цена только внутри явного ценового контейнера лота/футера
PRICE_CONTAINER_RE = re.compile(
    r'market-(?:list__cards-footer|show__price|item__price)[^>]*>\s*([\d\s]+?)\s*[SС]\b',
    re.I,
)
REQUEST_ITEM_RE = re.compile(r'manga-cards__item[^"]*market-item')
OFFER_PANEL_RE = re.compile(r'card-offer-panel')


@dataclass(frozen=True)
class Lot:
    lot_id: int | None
    price_s: float | None     # цена в S, если это S-лот
    barter: bool = False      # True = обмен карта-за-карту, не за S


@dataclass
class MarketSnapshot:
    card_id: int
    lots: list[Lot] = field(default_factory=list)
    want_count: int | None = None
    request_count: int | None = None
    ts: float = field(default_factory=time.time)

    @property
    def s_prices(self) -> list[float]:
        return sorted(l.price_s for l in self.lots if l.price_s is not None)

    @property
    def s_lot_count(self) -> int:
        return len(self.s_prices)

    @property
    def min_price(self) -> float | None:
        p = self.s_prices
        return p[0] if p else None

    @property
    def median_price(self) -> float | None:
        p = self.s_prices
        return float(statistics.median(p)) if p else None


def _blocks(html: str, marker_re: re.Pattern) -> list[str]:
    """Нарезать html на блоки, начинающиеся с маркера (по позициям вхождений)."""
    idxs = [m.start() for m in marker_re.finditer(html)]
    if not idxs:
        return []
    idxs.append(len(html))
    return [html[idxs[i]:idxs[i + 1]] for i in range(len(idxs) - 1)]


def parse_card_market(html: str, card_id: int) -> MarketSnapshot:
    """Разобрать /market/card/{id} в снимок с лотами (S-цена или бартер)."""
    lots: list[Lot] = []
    for block in _blocks(html, LOT_BLOCK_RE):
        lid_m = LOT_ID_RE.search(block)
        price_m = PRICE_CONTAINER_RE.search(block)
        if price_m:
            price = float(price_m.group(1).replace(" ", ""))
            lots.append(Lot(lot_id=int(lid_m.group(1)) if lid_m else None,
                            price_s=price, barter=False))
        else:
            lots.append(Lot(lot_id=int(lid_m.group(1)) if lid_m else None,
                            price_s=None, barter=True))
    return MarketSnapshot(card_id=card_id, lots=lots)


def parse_want_count(html: str) -> int:
    return len(OFFER_PANEL_RE.findall(html))


def parse_request_count(html: str) -> int:
    return len(REQUEST_ITEM_RE.findall(html))


# --- история цен через сэмплирование ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS active_lots (
    card_id INTEGER, lot_id INTEGER, price REAL,
    first_seen REAL, last_seen REAL,
    PRIMARY KEY (card_id, lot_id)
);
CREATE TABLE IF NOT EXISTS sold_events (
    card_id INTEGER, lot_id INTEGER, price REAL, sold_at REAL,
    PRIMARY KEY (card_id, lot_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    card_id INTEGER, ts REAL, min_price REAL, median_price REAL,
    s_lot_count INTEGER, want_count INTEGER, request_count INTEGER
);
"""


class MarketTracker:
    """Хранит снимки, выводит проданные лоты (исчезнувшие) и считает статистику."""

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)

    def ingest(self, snap: MarketSnapshot) -> list[Lot]:
        """Записать снимок. Вернуть лоты, которые ИСЧЕЗЛИ = проданы."""
        now = snap.ts
        cur_ids = {l.lot_id for l in snap.lots if l.lot_id is not None}

        prev = {row[0]: row[1] for row in self.db.execute(
            "SELECT lot_id, price FROM active_lots WHERE card_id=?", (snap.card_id,))}

        # обновить/добавить текущие активные лоты. Upsert идемпотентен: он
        # безопасен и когда лот уже в БД, и когда один lot_id встречается в снимке
        # дважды (на рынке бывают дубли) — first_seen сохраняется, обновляются
        # только last_seen и цена. Раньше на дубль падало UNIQUE-исключение.
        for l in snap.lots:
            if l.lot_id is None:
                continue
            self.db.execute(
                "INSERT INTO active_lots VALUES (?,?,?,?,?) "
                "ON CONFLICT(card_id, lot_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, price=excluded.price",
                (snap.card_id, l.lot_id, l.price_s, now, now))

        # исчезнувшие лоты = проданы по последней цене (только S-лоты идут в статистику)
        sold: list[Lot] = []
        for lot_id, price in prev.items():
            if lot_id not in cur_ids:
                self.db.execute(
                    "INSERT OR IGNORE INTO sold_events VALUES (?,?,?,?)",
                    (snap.card_id, lot_id, price, now))
                self.db.execute(
                    "DELETE FROM active_lots WHERE card_id=? AND lot_id=?",
                    (snap.card_id, lot_id))
                sold.append(Lot(lot_id=lot_id, price_s=price,
                                barter=price is None))

        self.db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
            (snap.card_id, now, snap.min_price, snap.median_price,
             snap.s_lot_count, snap.want_count, snap.request_count))
        self.db.commit()
        return sold

    def last_snapshot_ts(self, card_id: int) -> float | None:
        """Время последнего снимка рынка по карте (для TTL-кэша, анти-детект)."""
        r = self.db.execute("SELECT MAX(ts) FROM snapshots WHERE card_id=?",
                            (card_id,)).fetchone()
        return r[0] if r and r[0] is not None else None

    def price_report(self, card_id: int) -> dict:
        """Сводка по карте: активные цены + история продаж (avg/min/last)."""
        active = [row[0] for row in self.db.execute(
            "SELECT price FROM active_lots WHERE card_id=? AND price IS NOT NULL",
            (card_id,))]
        sold = [row for row in self.db.execute(
            "SELECT price, sold_at FROM sold_events "
            "WHERE card_id=? AND price IS NOT NULL ORDER BY sold_at", (card_id,))]
        sold_prices = [p for p, _ in sold]
        last_snap = self.db.execute(
            "SELECT want_count, request_count FROM snapshots WHERE card_id=? "
            "ORDER BY ts DESC LIMIT 1", (card_id,)).fetchone()
        return {
            "active_min": min(active) if active else None,
            "active_median": float(statistics.median(active)) if active else None,
            "active_lots": len(active),
            "sold_count": len(sold_prices),
            "sold_avg": round(statistics.mean(sold_prices), 1) if sold_prices else None,
            "sold_min": min(sold_prices) if sold_prices else None,
            "sold_last": sold_prices[-1] if sold_prices else None,
            "want_count": last_snap[0] if last_snap else None,
            "request_count": last_snap[1] if last_snap else None,
        }

    def reference_price(self, card_id: int, min_samples: int = 1) -> float | None:
        """Опорная цена для оценки обмена: медиана активных S-лотов, иначе средняя
        цена продаж. None = данных недостаточно (движок сделает fail-closed SKIP).

        min_samples — минимум точек данных, чтобы вообще ДОВЕРЯТЬ цене. Один
        случайный лот легко перекошен (демпинг/накрутка), поэтому для ценных
        рангов имеет смысл поднять порог: цена вернётся только когда активных
        лотов (или продаж) не меньше min_samples. min_samples=1 = как раньше.
        """
        r = self.price_report(card_id)
        if r["active_median"] is not None and r["active_lots"] >= min_samples:
            return r["active_median"]
        if r["sold_avg"] is not None and r["sold_count"] >= min_samples:
            return r["sold_avg"]
        return None
