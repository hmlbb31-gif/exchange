"""Тесты анти-скам сверки состава обмена перед приёмом (TOCTOU)."""

import asyncio

from config import Config, Rules
from mb.cards import CardStore
from mb.trader import Trader, _signature
from mb.parser import parse_trade_detail


def _trade_html(tid: int, recv_ids, give_ids) -> str:
    def items(ids):
        return "\n".join(
            f'<a href="/cards/{i}/users" class="trade__main-item"></a>' for i in ids)
    return f"""
    <div class="trade" data-id="{tid}">
      <a href="/users/5" class="trade__header-name">P</a>
      <div class="trade__main-user">Вам предлагают - {len(recv_ids)}</div>
      <div class="trade__main-items trade__main-items--creator">{items(recv_ids)}</div>
      <div class="trade__main-user">Вы отдадите - {len(give_ids)}</div>
      <div class="trade__main-items trade__main-items--receiver">{items(give_ids)}</div>
    </div>
    """


class FakeClient:
    def __init__(self, html):
        self.html = html
        self.config = Config()

    async def fetch_trade_detail(self, tid):
        return self.html


def _trader(html):
    return Trader(FakeClient(html), CardStore(), Rules())


def test_signature_ignores_order():
    a = parse_trade_detail(_trade_html(1, [20, 21], [10]))
    b = parse_trade_detail(_trade_html(1, [21, 20], [10]))
    assert _signature(a) == _signature(b)


def test_still_same_true_when_unchanged():
    html = _trade_html(7, [20, 21], [10])
    trade = parse_trade_detail(html)
    assert asyncio.run(_trader(html)._still_same(trade)) is True


def test_still_same_false_when_composition_changed():
    trade = parse_trade_detail(_trade_html(7, [20, 21], [10]))
    # сервер в момент приёма отдаёт ДРУГОЙ состав (одну карту убрали)
    changed = _trade_html(7, [20], [10])
    assert asyncio.run(_trader(changed)._still_same(trade)) is False


def test_still_same_false_on_parse_error():
    trade = parse_trade_detail(_trade_html(7, [20], [10]))
    # мусор вместо страницы -> не смогли подтвердить -> не принимаем
    assert asyncio.run(_trader("<html>garbage</html>")._still_same(trade)) is False
