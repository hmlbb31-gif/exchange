"""Тесты движка решений. Упор на fail-closed: сомнение = не отдавать."""

from config import Rules
from mb.decision import evaluate_trade, is_popular
from mb.models import CardInfo, CardRef, Trade, Verdict

RULES = Rules()


def provider(db: dict[int, CardInfo]):
    """Фабрика провайдера: неизвестная карта -> CardInfo с known=False."""
    def get(cid: int) -> CardInfo:
        return db.get(cid, CardInfo(card_id=cid))
    return get


def make_trade(receive_ids, give_ids, sr=None, sg=None) -> Trade:
    return Trade(
        trade_id=1, partner_id=2, partner_name="X",
        receive=tuple(CardRef(i) for i in receive_ids),
        give=tuple(CardRef(i) for i in give_ids),
        stated_receive=sr if sr is not None else len(receive_ids),
        stated_give=sg if sg is not None else len(give_ids),
    )


def test_unknown_given_card_is_skip():
    # отдаю карту, о которой ничего не знаю -> SKIP (не рискуем)
    db = {10: CardInfo(10, rank="X")}  # получаемая известна, отдаваемая нет
    ev = evaluate_trade(make_trade([10], [999]), provider(db), RULES)
    assert ev.verdict == Verdict.SKIP


def test_protected_rank_given_is_reject():
    # Защита рангов по умолчанию ВЫКЛючена (политика «всё по выгоде + вето»).
    # Проверяем настраиваемый переключатель: явно защитив S, отдавать его нельзя.
    rules = Rules(protected_ranks=frozenset({"S"}))
    db = {
        1: CardInfo(1, rank="X"),   # получаю дорогую
        2: CardInfo(2, rank="S"),   # но отдаю ранг S — защищён вручную
    }
    ev = evaluate_trade(make_trade([1], [2]), provider(db), rules)
    assert ev.verdict == Verdict.REJECT
    assert any("защищён" in r for r in ev.reasons)


def test_protected_card_whitelist_is_reject():
    rules = Rules(protected_cards=frozenset({777}))
    db = {1: CardInfo(1, rank="X"), 777: CardInfo(777, rank="E")}
    ev = evaluate_trade(make_trade([1], [777]), provider(db), rules)
    assert ev.verdict == Verdict.REJECT
    assert any("whitelist" in r.lower() for r in ev.reasons)


def test_popular_pp_card_given_is_reject():
    # карта дешёвого ранга, но популярная (много «Хочу», мало лотов) -> не отдаём
    db = {
        1: CardInfo(1, rank="X"),
        2: CardInfo(2, rank="C", want_count=40, lot_count=1),
    }
    ev = evaluate_trade(make_trade([1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT
    assert any("ПП" in r or "популяр" in r for r in ev.reasons)


def test_expensive_given_is_reject():
    db = {1: CardInfo(1, rank="X"), 2: CardInfo(2, rank="E", price=100.0)}
    ev = evaluate_trade(make_trade([1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT


def test_good_trade_accepts():
    # отдаю 2 дешёвые обычные карты за 1 явно ценную -> выгодно и безопасно
    db = {
        1: CardInfo(1, rank="B", price=30.0),          # получаю (не >= expensive 50)
        2: CardInfo(2, rank="E", price=2.0),
        3: CardInfo(3, rank="E", price=2.0),
    }
    ev = evaluate_trade(make_trade([1], [2, 3]), provider(db), RULES)
    assert ev.verdict == Verdict.ACCEPT
    assert ev.gain_ratio and ev.gain_ratio >= RULES.min_gain_ratio


def test_tradeable_rank_given_ok_by_default_when_profitable():
    # A — обмениваемый ранг (не защищён, не strict). Отдать можно, если выгодно
    # (2×A за 1×A) и известна цена (A требует цену) и не сработало вето.
    db = {
        1: CardInfo(1, rank="A"), 2: CardInfo(2, rank="A"),   # получаю 2×A
        3: CardInfo(3, rank="A", price=5.0),                  # отдаю 1×A (цена известна)
    }
    ev = evaluate_trade(make_trade([1, 2], [3]), provider(db), RULES)
    assert ev.verdict == Verdict.ACCEPT


def test_valuable_given_without_price_is_skip():
    # A — ценный ранг: без известной рыночной цены не отдаём вслепую -> SKIP.
    db = {1: CardInfo(1, rank="A"), 2: CardInfo(2, rank="A")}  # отдаю A без цены
    ev = evaluate_trade(make_trade([1, 1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.SKIP
    assert any("без рыночной цены" in r for r in ev.reasons)


def test_x_rank_protected_by_default():
    # X защищён по умолчанию — отдавать нельзя, даже за дорогое.
    db = {1: CardInfo(1, rank="X"), 2: CardInfo(2, rank="X")}
    ev = evaluate_trade(make_trade([1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT
    assert any("защищён" in r for r in ev.reasons)


def test_event_rank_protected_even_without_value():
    # Ивентовый ранг (H) защищён и НЕ имеет ценности в шкале — всё равно REJECT,
    # а не SKIP: мы знаем ранг и знаем, что его не отдаём.
    db = {1: CardInfo(1, rank="A"), 2: CardInfo(2, rank="H")}
    ev = evaluate_trade(make_trade([1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT
    assert any("защищён" in r for r in ev.reasons)


def test_strict_s_without_price_is_skip():
    # S — strict: без известной рыночной цены не отдаём вслепую -> SKIP.
    db = {1: CardInfo(1, rank="S"), 2: CardInfo(2, rank="S")}  # получаю 2×S, отдаю 1×S
    ev = evaluate_trade(make_trade([1, 1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.SKIP
    assert any("без рыночной цены" in r for r in ev.reasons)


def test_strict_s_with_low_price_accepts():
    # S с известной низкой ценой (ниже strict-порога 20) и выгодой 2×S за 1×S -> ACCEPT.
    db = {
        1: CardInfo(1, rank="S", price=5.0), 2: CardInfo(2, rank="S", price=5.0),
        3: CardInfo(3, rank="S", price=5.0),
    }
    ev = evaluate_trade(make_trade([1, 2], [3]), provider(db), RULES)
    assert ev.verdict == Verdict.ACCEPT


def test_strict_s_expensive_is_reject():
    # S дороже strict-порога (20) — не отдаём, хотя по рангу было бы выгодно.
    db = {
        1: CardInfo(1, rank="S", price=5.0), 2: CardInfo(2, rank="S", price=5.0),
        3: CardInfo(3, rank="S", price=25.0),  # отдаём дорогую S
    }
    ev = evaluate_trade(make_trade([1, 2], [3]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT
    assert any("дорог" in r.lower() for r in ev.reasons)


def test_fair_but_not_double_is_reject():
    # равноценный обмен 1:1 -> НЕ проходит правило x2
    db = {1: CardInfo(1, rank="C", price=4.0), 2: CardInfo(2, rank="C", price=4.0)}
    ev = evaluate_trade(make_trade([1], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.REJECT


def test_inconsistent_counts_is_skip():
    ev = evaluate_trade(make_trade([1], [2], sr=5), provider({}), RULES)
    assert ev.verdict == Verdict.SKIP


def test_unknown_received_card_is_skip():
    # отдаю известную дешёвую, но не знаю, что получаю -> нельзя оценить выгоду
    db = {2: CardInfo(2, rank="E", price=2.0)}
    ev = evaluate_trade(make_trade([999], [2]), provider(db), RULES)
    assert ev.verdict == Verdict.SKIP


def test_is_popular_by_requests():
    assert is_popular(CardInfo(1, request_count=50), RULES) is True
    assert is_popular(CardInfo(1, want_count=1, lot_count=100), RULES) is False
