"""Разбор HTML mangabuff.ru для обменов. Чистые функции, тестируются на сохранённой разметке.

Разметка снята живьём 2026-07-25 со страницы /trades/{id}:

  <div class="trade" data-id="84967767"> ...
    <div class="trade__main-user">Вам предлагают - 1</div>
    <div class="trade__main-items trade__main-items--creator">
      <a href="/cards/288652/users" class="trade__main-item">
        <div class="card-copy-ribbon card-copy-ribbon--palindrome"></div>
        <span>экз. 171</span>
      </a>
    </div>
    <div class="trade__main-user">Вы отдадите - 9</div>
    <div class="trade__main-items trade__main-items--receiver"> ... </div>

  --creator  = «Вам предлагают» = карты, которые Я ПОЛУЧУ.
  --receiver = «Вы отдадите»    = карты, которые Я ОТДАМ.
"""

from __future__ import annotations

import re

from mb.models import CardRef, Trade

# --- Список входящих: /trades ---
LIST_ITEM_RE = re.compile(
    r'href="/trades/(\d+)"[^>]*class="[^"]*trade__list-item',
    re.I,
)
LIST_NAME_RE = re.compile(r'trade__list-name"[^>]*>\s*(?:от\s+)?([^<]+?)\s*<', re.I)

# --- Детали обмена: /trades/{id} ---
TRADE_ID_RE = re.compile(r'class="trade"[^>]*data-id="(\d+)"', re.I)
TRADE_ID_ALT_RE = re.compile(r'data-id="(\d+)"')
# Партнёр: ссылка <a ... class="trade__header-name" ...>Имя</a>. Атрибуты href и
# class могут идти в ЛЮБОМ порядке (живьём href стоит раньше class), поэтому
# сначала берём весь тег якоря, потом достаём из него href и имя.
PARTNER_A_RE = re.compile(
    r'<a\b([^>]*class="[^"]*trade__header-name[^"]*"[^>]*)>\s*([^<]+?)\s*</a>', re.I
)
HREF_USER_RE = re.compile(r'href="/users/(\d+)"')
STATED_RECEIVE_RE = re.compile(r"Вам\s+предлагают\s*[-–—]\s*(\d+)")
STATED_GIVE_RE = re.compile(r"Вы\s+отда(?:дите|ёте|дёте)\s*[-–—]\s*(\d+)")

CREATOR_MARK = "trade__main-items--creator"
RECEIVER_MARK = "trade__main-items--receiver"

CARD_HREF_RE = re.compile(r'href="/cards/(\d+)/users')
COPY_RE = re.compile(r"экз\.?\s*(\d+)")
PALINDROME_MARK = "card-copy-ribbon--palindrome"

LOGIN_MARKERS = ("login-button", 'action="/login"', "Войти через")


class ParseError(RuntimeError):
    """Разметка не та, что ожидаем. Падать надо громко, а не молча."""


def looks_like_login_page(html: str) -> bool:
    """Похоже, что сессия протухла и нас отправили на вход."""
    low = html[:4000]
    return sum(m in low for m in LOGIN_MARKERS) >= 1 and "trade" not in low[:1500]


def parse_trades_list(html: str) -> list[tuple[int, str]]:
    """Список входящих обменов -> [(trade_id, partner_name), ...]."""
    out: list[tuple[int, str]] = []
    for m in LIST_ITEM_RE.finditer(html):
        tid = int(m.group(1))
        # имя ищем в окне после ссылки
        window = html[m.end(): m.end() + 400]
        name_m = LIST_NAME_RE.search(window)
        out.append((tid, name_m.group(1).strip() if name_m else ""))
    return out


def _parse_items(block: str) -> tuple[CardRef, ...]:
    """Вытащить карты из одного блока (--creator или --receiver), сохраняя порядок."""
    positions = [m for m in CARD_HREF_RE.finditer(block)]
    items: list[CardRef] = []
    for i, m in enumerate(positions):
        start = m.start()
        end = positions[i + 1].start() if i + 1 < len(positions) else len(block)
        window = block[start:end]
        copy_m = COPY_RE.search(window)
        items.append(
            CardRef(
                card_id=int(m.group(1)),
                copy_number=int(copy_m.group(1)) if copy_m else None,
                palindrome=PALINDROME_MARK in window,
            )
        )
    return tuple(items)


def parse_trade_detail(html: str) -> Trade:
    """Разобрать страницу /trades/{id} в объект Trade.

    Fail-closed: если структура не распозналась (нет блоков сторон) — исключение,
    вызывающий код обязан НЕ трогать такой обмен.
    """
    tid_m = TRADE_ID_RE.search(html) or TRADE_ID_ALT_RE.search(html)
    if not tid_m:
        raise ParseError("не найден trade_id")
    trade_id = int(tid_m.group(1))

    partner_m = PARTNER_A_RE.search(html)
    partner_name = partner_m.group(2).strip() if partner_m else ""
    partner_id = None
    if partner_m:
        hid = HREF_USER_RE.search(partner_m.group(1))
        partner_id = int(hid.group(1)) if hid else None

    ci = html.find(CREATOR_MARK)
    ri = html.find(RECEIVER_MARK)
    if ci == -1 or ri == -1:
        raise ParseError("не найдены блоки сторон обмена (creator/receiver)")

    # creator идёт раньше receiver; блоки последовательны
    if ci < ri:
        creator_block = html[ci:ri]
        receiver_block = html[ri:]
    else:  # подстраховка, если порядок вдруг иной
        receiver_block = html[ri:ci]
        creator_block = html[ci:]

    receive = _parse_items(creator_block)   # «Вам предлагают» — получаю
    give = _parse_items(receiver_block)     # «Вы отдадите» — отдаю

    sr = STATED_RECEIVE_RE.search(html)
    sg = STATED_GIVE_RE.search(html)

    return Trade(
        trade_id=trade_id,
        partner_id=partner_id,
        partner_name=partner_name,
        receive=receive,
        give=give,
        stated_receive=int(sr.group(1)) if sr else None,
        stated_give=int(sg.group(1)) if sg else None,
    )
