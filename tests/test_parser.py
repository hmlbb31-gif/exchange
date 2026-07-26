"""Тесты парсера на разметке, снятой живьём с /trades/84967767 (2026-07-25)."""

from mb.parser import parse_trade_detail, parse_trades_list, looks_like_login_page

GIVE_IDS = [354804, 241936, 241983, 241886, 57303, 242098, 242238, 242036, 241864]


def _build_trade_html() -> str:
    give_items = "\n".join(
        f'<a href="/cards/{cid}/users" class="trade__main-item">'
        f'<img src="/img/cards/{cid}.jpg"><span>экз. {i + 2}</span></a>'
        for i, cid in enumerate(GIVE_IDS)
    )
    return f"""
    <div class="trade" data-id="84967767">
      <a class="trade__header-name" href="/users/714166">Акбар Кцоев</a>
      <div class="trade__main">
        <div class="trade__main-user">Вам предлагают - 1</div>
        <div class="trade__main-items trade__main-items--creator">
          <a href="/cards/288652/users" class="trade__main-item">
            <div class="card-copy-ribbon card-copy-ribbon--palindrome"></div>
            <img src="/img/cards/288652.jpg"><span>экз. 171</span>
          </a>
        </div>
        <div class="trade__main-divider"></div>
        <div class="trade__main-user">Вы отдадите - 9</div>
        <div class="trade__main-items trade__main-items--receiver">
          {give_items}
        </div>
      </div>
    </div>
    """


def test_parse_trade_detail_basic():
    t = parse_trade_detail(_build_trade_html())
    assert t.trade_id == 84967767
    assert t.partner_id == 714166
    assert t.partner_name == "Акбар Кцоев"
    # получаю ровно одну карту 288652, экз 171, палиндром
    assert len(t.receive) == 1
    assert t.receive[0].card_id == 288652
    assert t.receive[0].copy_number == 171
    assert t.receive[0].palindrome is True
    # отдаю 9 карт в исходном порядке
    assert [c.card_id for c in t.give] == GIVE_IDS
    # палиндром только у получаемой
    assert all(not c.palindrome for c in t.give)


def test_stated_counts_consistent():
    t = parse_trade_detail(_build_trade_html())
    assert t.stated_receive == 1
    assert t.stated_give == 9
    assert t.counts_consistent is True


def test_counts_inconsistent_detected():
    # ломаем: заголовок говорит 3, а карта одна
    html = _build_trade_html().replace("Вам предлагают - 1", "Вам предлагают - 3")
    t = parse_trade_detail(html)
    assert t.counts_consistent is False


def test_parse_trades_list():
    html = """
    <a href="/trades/rejectAll?type_trade=receiver" class="button">Отменить все</a>
    <div class="trade__list">
      <a href="/trades/84967767" class="trade__list-item">
        <div class="trade__list-name">от Акбар Кцоев</div>
      </a>
      <a href="/trades/84967838" class="trade__list-item">
        <div class="trade__list-name">от Кто-то Другой</div>
      </a>
    </div>
    """
    rows = parse_trades_list(html)
    assert rows == [(84967767, "Акбар Кцоев"), (84967838, "Кто-то Другой")]
    # rejectAll (не числовой id) не попал в список
    assert all(isinstance(tid, int) for tid, _ in rows)


def test_partner_href_before_class():
    # Живая разметка /trades/85145430 (2026-07-26): в ссылке партнёра href идёт
    # РАНЬШЕ class. Парсер обязан вытащить и имя, и id независимо от порядка.
    html = """
    <div class="trade" data-id="85145430">
      <a href="/users/1388476" class="trade__header-name">Hedder</a>
      <div class="trade__main-user">Вам предлагают - 2</div>
      <div class="trade__main-items trade__main-items--creator">
        <a href="/cards/63752/users" class="trade__main-item"></a>
        <a href="/cards/1815/users" class="trade__main-item"></a>
      </div>
      <div class="trade__main-user">Вы отдадите - 1</div>
      <div class="trade__main-items trade__main-items--receiver">
        <a href="/cards/141651/users" class="trade__main-item"></a>
      </div>
    </div>
    """
    t = parse_trade_detail(html)
    assert t.trade_id == 85145430
    assert t.partner_id == 1388476
    assert t.partner_name == "Hedder"
    assert [c.card_id for c in t.receive] == [63752, 1815]
    assert [c.card_id for c in t.give] == [141651]
    assert t.stated_receive == 2 and t.stated_give == 1
    assert t.counts_consistent is True


def test_login_page_detection():
    assert looks_like_login_page(
        '<form action="/login"><button class="login-button">Войти через</button></form>'
    )
