"""Движок решений по обмену. САМАЯ ВАЖНАЯ ЧАСТЬ — здесь fail-closed.

Философия: лучше пропустить сто выгодных обменов, чем один раз слить нужную карту.
Поэтому по умолчанию всё — REJECT/SKIP, и только полностью проверенный,
безопасный и выгодный обмен получает ACCEPT.

СПРАВЕДЛИВОСТЬ размена считаем ПО РАНГУ (по «весу» карт), а НЕ по рыночной цене.
Это подтверждено реальной историей обменов: сообщество торгует «1к2 одного ранга»
(получить 2 за 1) и «1к1 рангом выше». При геометрической шкале рангов (каждый
ранг вдвое дороже предыдущего) правило x2 воспроизводит оба случая точно:
  - 2 карты ранга C за 1 карту ранга C  = 8 за 4 = x2  -> ACCEPT
  - 1 карта ранга B за 1 карту ранга C  = 8 за 4 = x2  -> ACCEPT (апгрейд)
  - 1 за 1 одного ранга                 = x1           -> REJECT
Рыночная цена и «ПП» НЕ участвуют в справедливости — они только ЗАЩИТА
(что нельзя отдавать), чтобы бот не зарубал нормальные 2:1 из-за дешёвых карт.

Порядок проверок (любая сработавшая — прекращает оценку):
  1. Целостность разметки (счётчики сошлись).           нет -> SKIP
  2. Пустые стороны.                                     -> REJECT
  3. По каждой ОТДАВАЕМОЙ карте — жёсткие защиты:
       whitelist protected_cards     -> REJECT
       ранг в protected_ranks (X, ивентовые) -> REJECT  (даже без ценности)
       неизвестен ранг/ценность      -> SKIP  (не знаю, что отдаю — не отдаю)
       strict-ранг (S) без цены      -> SKIP  (не отдаём вслепую)
       цена >= порога (strict — ниже) -> REJECT
       популярная (ПП)               -> REJECT
  4. По каждой ПОЛУЧАЕМОЙ карте — нужен известный ранг.   нет -> SKIP
  5. Правило ранга: received_weight >= given_weight * min_gain_ratio.
                                          не проходит -> REJECT
  6. Всё чисто -> ACCEPT.
"""

from __future__ import annotations

from collections.abc import Callable

from config import Rules
from mb.models import CardInfo, Evaluation, Trade, Verdict

# Провайдер данных по карте: card_id -> CardInfo (или CardInfo с known=False).
CardInfoProvider = Callable[[int], CardInfo]


def is_popular(info: CardInfo, rules: Rules) -> bool:
    """ПП — популярная карта. Оцениваем по совокупности сигналов внимания.

    Считаем популярной, если:
      - много «Хочу» И мало лотов в продаже (высокий спрос при дефиците), ИЛИ
      - «Хочу» выше порога сам по себе, ИЛИ
      - много заявок.
    Отсутствующий сигнал в пользу популярности НЕ засчитывается (это отдельно
    ловит проверка known в основном движке — неизвестную карту не отдаём вовсе).
    """
    want = info.want_count
    lots = info.lot_count
    reqs = info.request_count

    if want is not None and lots is not None:
        if want >= rules.pp_want_threshold and lots <= rules.pp_low_supply_lots:
            return True
    if want is not None and want >= rules.pp_want_threshold:
        return True
    if reqs is not None and reqs >= rules.pp_request_threshold:
        return True
    return False


def rank_weight(info: CardInfo, rules: Rules) -> float | None:
    """«Вес» карты по рангу для оценки справедливости размена (НЕ рыночная цена).

    Геометрическая шкала (по умолчанию каждый ранг вдвое дороже) даёт правило
    «1к2 одного ранга / 1к1 рангом выше» при min_gain_ratio=2.0. None = ранг неизвестен.
    """
    if info.rank is not None:
        return rules.rank_value.get(info.rank)
    return None


def evaluate_trade(
    trade: Trade,
    get_info: CardInfoProvider,
    rules: Rules,
) -> Evaluation:
    """Оценить входящий обмен. Возвращает вердикт с полным объяснением."""
    ev = Evaluation(verdict=Verdict.SKIP)

    # 1. Целостность разметки
    if not trade.counts_consistent:
        ev.verdict = Verdict.SKIP
        ev.add("Счётчики карт не сошлись с заголовком — разметка подозрительна. SKIP.")
        return ev

    # 2. Пустые стороны
    if not trade.receive or not trade.give:
        ev.verdict = Verdict.REJECT
        ev.add("Одна из сторон пуста (получаю или отдаю ничего). REJECT.")
        return ev

    # 3. Жёсткие защиты по отдаваемым картам (рыночная цена/ПП — только тут, как вето)
    given_value = 0.0
    for c in trade.give:
        info = get_info(c.card_id)
        label = f"карта {c.card_id} ({info.name or '?'}, ранг {info.rank or '?'})"

        # 3a. whitelist — самая явная защита
        if c.card_id in rules.protected_cards or info.protected:
            ev.verdict = Verdict.REJECT
            ev.add(f"{label} в защищённом списке (whitelist). REJECT.")
            return ev
        # 3b. защищённый ранг (X, ивентовые) — известный ранг, который НИКОГДА
        #     не отдаём, даже если ему не задана ценность в шкале.
        if info.rank is not None and info.rank in rules.protected_ranks:
            ev.verdict = Verdict.REJECT
            ev.add(f"{label}: ранг {info.rank} защищён от автоотдачи. REJECT.")
            return ev
        # 3c. для оценки размена нужна ценность ранга (иначе не знаю, что отдаю)
        w = rank_weight(info, rules)
        if w is None:
            ev.verdict = Verdict.SKIP
            ev.add(f"Неизвестен ранг/ценность {label} — не знаю, что отдаю. SKIP.")
            return ev
        # 3d. Ценные ранги — нужна ИЗВЕСТНАЯ рыночная цена, чтобы не отдать дорогое
        #     вслепую. strict-ранги (S) вдобавок имеют более низкий порог «дорогая».
        strict = info.rank is not None and info.rank in rules.strict_ranks
        needs_price = strict or (
            info.rank is not None
            and info.rank in getattr(rules, "require_price_ranks", frozenset()))
        exp_threshold = rules.strict_expensive_price if strict else rules.expensive_price
        if needs_price and info.price is None:
            ev.verdict = Verdict.SKIP
            ev.add(f"{label}: ценный ранг {info.rank} без рыночной цены — "
                   f"не отдаём вслепую. SKIP.")
            return ev
        # 3e. дорогая карта — вето
        if info.price is not None and info.price >= exp_threshold:
            ev.verdict = Verdict.REJECT
            ev.add(f"{label}: цена {info.price} >= порога {exp_threshold} — дорогая. REJECT.")
            return ev
        # 3f. популярная (ПП) — вето
        if is_popular(info, rules):
            ev.verdict = Verdict.REJECT
            ev.add(f"{label}: популярная (ПП) — хочу={info.want_count}, "
                   f"лоты={info.lot_count}, заявки={info.request_count}. REJECT.")
            return ev
        given_value += w

    # 4 + 5. Получаемое и правило ранга (справедливость — по весу ранга)
    received_value = 0.0
    for c in trade.receive:
        info = get_info(c.card_id)
        w = rank_weight(info, rules)
        if w is None:
            ev.verdict = Verdict.SKIP
            ev.add(f"Неизвестен ранг получаемой карты {c.card_id} — не могу оценить размен. SKIP.")
            return ev
        received_value += w

    ev.received_value = received_value
    ev.given_value = given_value
    ev.gain_ratio = (received_value / given_value) if given_value > 0 else None

    need = given_value * rules.min_gain_ratio
    if received_value >= need:
        ev.verdict = Verdict.ACCEPT
        ev.add(f"Выгодно по рангу: беру вес {received_value:.0f} за {given_value:.0f} "
               f"(x{ev.gain_ratio:.2f} >= x{rules.min_gain_ratio}). ACCEPT.")
    else:
        ev.verdict = Verdict.REJECT
        ev.add(f"Невыгодно: вес {received_value:.0f}, нужно >= {need:.0f} "
               f"(x{rules.min_gain_ratio}). REJECT.")
    return ev
