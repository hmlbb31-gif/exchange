"""Тесты рынка: парсинг лотов, разделение S/бартер, история цен через сэмплирование."""

from mb.market import (Lot, MarketSnapshot, MarketTracker, parse_card_market,
                       parse_request_count, parse_want_count)


def _lot_html(lot_id: int, price_s: int | None = None, barter_rank: str = "E"):
    """Собрать блок лота как на /market/card/{id}."""
    if price_s is not None:
        price = (f'<div class="market-list__cards-footer">{price_s} S</div>')
    else:  # бартер: карта ранга (напр. "2 E") без ценового контейнера
        price = f'<span class="market-show__user-cards">2 {barter_rank}</span>'
    return (f'<a class="market-show__item" href="/market/{lot_id}">'
            f'<div class="market-show__user">Seller</div>{price}</a>')


def test_parse_s_and_barter_lots():
    html = _lot_html(1001, price_s=8) + _lot_html(1002, price_s=12) + _lot_html(1003, barter_rank="S")
    snap = parse_card_market(html, card_id=500)
    assert len(snap.lots) == 3
    s_lots = [l for l in snap.lots if not l.barter]
    assert {l.price_s for l in s_lots} == {8.0, 12.0}
    # бартерный лот с "2 S" НЕ распознан как цена 2 S
    barter = [l for l in snap.lots if l.barter]
    assert len(barter) == 1 and barter[0].price_s is None
    assert snap.min_price == 8.0
    assert snap.median_price == 10.0
    assert snap.s_lot_count == 2


def test_want_and_request_counts():
    assert parse_want_count('<div class="card-offer-panel"></div>' * 5) == 5
    assert parse_request_count('<div class="manga-cards__item market-item"></div>' * 3) == 3


def test_tracker_infers_sold_when_lot_disappears():
    tr = MarketTracker()
    card = 500
    # снимок 1: два лота
    snap1 = MarketSnapshot(card, [Lot(1001, 8.0), Lot(1002, 12.0)], want_count=20, request_count=2)
    assert tr.ingest(snap1) == []
    # снимок 2: лот 1001 исчез -> продан по 8 (каждый снимок несёт свежий «Хочу»)
    snap2 = MarketSnapshot(card, [Lot(1002, 12.0)], want_count=18, request_count=1)
    sold = tr.ingest(snap2)
    assert len(sold) == 1 and sold[0].lot_id == 1001 and sold[0].price_s == 8.0

    rep = tr.price_report(card)
    assert rep["sold_count"] == 1
    assert rep["sold_last"] == 8.0
    assert rep["active_min"] == 12.0
    assert rep["want_count"] == 18  # из последнего (свежего) снимка


def test_duplicate_lot_id_in_snapshot_does_not_crash():
    # На рынке один lot_id может встретиться в снимке дважды. Раньше это ломало
    # запись UNIQUE-исключением (баг найден на живом прогоне карты 28266).
    tr = MarketTracker()
    snap = MarketSnapshot(28266, [Lot(5001, 8.0), Lot(5001, 8.0), Lot(5002, 12.0)])
    assert tr.ingest(snap) == []            # не падает
    rep = tr.price_report(28266)
    assert rep["active_lots"] == 2          # дубль схлопнулся в один активный лот
    assert rep["active_min"] == 8.0


def test_reference_price_prefers_active_median():
    tr = MarketTracker()
    tr.ingest(MarketSnapshot(7, [Lot(1, 10.0), Lot(2, 20.0), Lot(3, 30.0)]))
    assert tr.reference_price(7) == 20.0


def test_reference_price_falls_back_to_sold_avg():
    tr = MarketTracker()
    tr.ingest(MarketSnapshot(7, [Lot(1, 10.0), Lot(2, 30.0)]))
    tr.ingest(MarketSnapshot(7, []))  # оба проданы (10 и 30) -> avg 20
    assert tr.reference_price(7) == 20.0


def test_reference_price_none_when_no_data():
    tr = MarketTracker()
    assert tr.reference_price(999) is None  # -> движок сделает fail-closed SKIP
