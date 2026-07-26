"""Тесты базы карт: ранги из каталога + интеграция с рыночным трекером."""

from mb.cards import CardStore, parse_catalog_ranks
from mb.market import Lot, MarketSnapshot, MarketTracker


def test_parse_catalog_ranks_both_orders():
    html = (
        '<div class="manga-cards__item-wrapper" data-rank="X" data-card-id="100"></div>'
        '<div data-card-id="200" data-rank="S"></div>'
    )
    assert parse_catalog_ranks(html) == {100: "X", 200: "S"}


def test_cardstore_unknown_is_fail_closed():
    store = CardStore()
    info = store.get(12345)
    assert info.known is False  # -> движок сделает SKIP


def test_cardstore_combines_rank_and_market():
    tracker = MarketTracker()
    tracker.ingest(MarketSnapshot(5, [Lot(1, 40.0), Lot(2, 50.0)],
                                  want_count=20, request_count=3))
    store = CardStore(ranks={5: "C"}, tracker=tracker)
    store.protect(5)
    info = store.get(5)
    assert info.rank == "C"
    assert info.price == 45.0          # медиана активных S-лотов
    assert info.want_count == 20
    assert info.lot_count == 2
    assert info.request_count == 3
    assert info.protected is True
    assert info.known is True


def test_cardstore_protect_unprotect():
    store = CardStore()
    store.protect(9)
    assert store.get(9).protected is True
    store.unprotect(9)
    assert store.get(9).protected is False
