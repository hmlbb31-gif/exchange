"""Конфигурация из .env + правила оценки обменов.

Правила намеренно вынесены сюда и снабжены безопасными значениями по умолчанию,
чтобы их можно было крутить, не трогая код.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv не обязателен для тестов
    pass


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit())


# Порядок рангов от дешёвого к дорогому. Нижний индекс = дешевле.
# Основные ранги X..E подтверждены. В каталоге также найдены P и G — их место
# в шкале ценности ПРЕДВАРИТЕЛЬНОЕ (нужно подтверждение пользователя, см. PLAN.md).
# По рарности P и G стоят между A и B; здесь оценены консервативно.
DEFAULT_RANK_ORDER = ("E", "D", "C", "B", "P", "G", "A", "S", "X")

# Базовая «ценность» ранга (геометрическая шкала: +ранг ≈ ×2). P/G — временно.
DEFAULT_RANK_VALUE = {
    "E": 1.0, "D": 2.0, "C": 4.0, "B": 8.0,
    "P": 12.0, "G": 12.0,   # ПРЕДВАРИТЕЛЬНО — уточнить у пользователя
    "A": 16.0, "S": 32.0, "X": 64.0,
}


@dataclass
class Rules:
    """Правила решения. Все пороги — fail-closed по духу: сомневаешься — не отдавай."""

    # Минимальный выигрыш: суммарная ценность получаемого должна быть
    # не меньше отдаваемого, умноженного на этот коэффициент.
    # 2.0 = «1:2 в мою пользу». Обмен на равного ранга 1:1 НЕ проходит — и это правильно.
    min_gain_ratio: float = 2.0

    # Per-rank пороги выгоды: ранг ОТДАВАЕМОЙ карты -> свой min_gain_ratio.
    # Смысл: дешёвый мусор (E/D/C) можно отдавать чуть мягче, а ценные ранги (A/S)
    # — строже, чтобы случайный «выгодный по весу» размен не увёл дорогую карту.
    # Побеждает САМЫЙ СТРОГИЙ порог среди отдаваемых карт (берём максимум);
    # ранг без записи использует глобальный min_gain_ratio. Пустой словарь по
    # умолчанию = поведение ровно как раньше (единый порог для всех).
    # Пример боевой настройки: {"E": 2.0, "D": 2.0, "C": 2.0,
    #                           "B": 2.5, "A": 3.0, "S": 3.0}
    min_gain_ratio_by_rank: dict[str, float] = field(default_factory=dict)

    # Анти-разбавление корзины. Справедливость по СУММЕ весов не защищает от
    # размена «отдаю 1 ценную B + мусор за гору дешёвых E»: сумма может пройти
    # порог, но по составу ты теряешь ценную карту. Поэтому дополнительно требуем,
    # чтобы самая ценная ОТДАВАЕМАЯ карта была покрыта ПОЛУЧАЕМОЙ картой
    # сопоставимого веса:  max(received_weight) >= max(given_weight) * ratio.
    # 1.0 = твоя лучшая отдаваемая карта должна быть покрыта хотя бы равной по
    # рангу полученной. 0.0 = проверка выключена (старое поведение).
    basket_guard_ratio: float = 1.0

    # Косметика экземпляра — тоже ценность. Палиндромные («красивые») номера
    # экземпляров ценятся коллекционерами, поэтому такие карты не отдаём на
    # автомате, даже если размен выгоден по рангу. Метка palindrome берётся из
    # собственной разметки сайта (card-copy-ribbon--palindrome), не эвристика.
    protect_palindrome: bool = True
    # Первые копии (низкий номер экземпляра) тоже ценны. Если > 0 — не отдаём
    # карту с номером экземпляра <= этого значения (напр. 3 => бережём экз. 1..3).
    # 0 = выключено (по умолчанию, чтобы не менять поведение без калибровки).
    protect_copy_number_below: int = 0

    # Ранги, которые НИКОГДА не отдаём автоматически, независимо от выгоды.
    # По решению владельца (2026-07-26): защищаем X и все ИВЕНТОВЫЕ ранги
    # (H, N, V, Q, L, K) — их держим в базе только чтобы фильтр их узнавал и не
    # отдал случайно. Обмениваем A, P, G, B, C, D, E (и S — см. strict_ranks).
    protected_ranks: frozenset[str] = frozenset({"X", "H", "N", "V", "Q", "L", "K"})

    # Ранги с УЖЕСТОЧЁННОЙ проверкой рынка (чтобы не отдать дорогую карту за дешёвую).
    # Для них: (а) обязателен известный рыночный прайс — иначе SKIP (не отдаём
    # вслепую); (б) действует более низкий порог «дорогая» (strict_expensive_price).
    strict_ranks: frozenset[str] = frozenset({"S"})

    # Порог «дорогая карта» в валюте рынка. Дороже — не отдаём.
    expensive_price: float = 50.0
    # Более жёсткий порог для strict-рангов (S). Значение предварительное —
    # уточнить по реальным ценам S на рынке (валюта ещё не разведана).
    strict_expensive_price: float = 20.0

    # Порог «популярная карта» (ПП): если сигналов внимания много — не отдаём.
    # ПП определяется по совокупности: «Хочу», число лотов, заявок.
    # Пороги подняты (2026-07-26) по решению владельца: на живом обмене обычная
    # D-карта (Хочу=13, заявки=24) ложно попадала в ПП при старых порогах 15/10.
    pp_want_threshold: int = 30        # >= столько «Хочу» — считаем популярной
    pp_low_supply_lots: int = 3        # и при этом мало лотов в продаже — дефицит
    pp_request_threshold: int = 40     # либо очень много заявок

    # Ранги, для которых обязательна ИЗВЕСТНАЯ рыночная цена, чтобы отдать карту
    # (защита «не отдать дорогое вслепую»). Ценные обмениваемые ранги требуют
    # подтверждённой цены; дешёвые C/D/E можно отдавать без неё. S покрыт strict.
    require_price_ranks: frozenset[str] = frozenset({"A", "P", "G", "B"})

    # Ручной whitelist карт, которые нельзя отдавать (card_id).
    protected_cards: frozenset[int] = frozenset()

    rank_order: tuple[str, ...] = DEFAULT_RANK_ORDER
    rank_value: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RANK_VALUE))


@dataclass
class Config:
    bot_token: str = ""
    admin_ids: tuple[int, ...] = ()
    cookie: str = ""
    cookie_file: str = "cookies.txt"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )
    base_url: str = "https://mangabuff.ru"
    dry_run: bool = True
    requests_per_minute: int = 20
    request_timeout: float = 20.0
    fernet_key: str = ""
    proxy: str | None = None   # напр. http://user:pass@host:port (для фарм-аккаунтов)
    # Анти-детект: случайная человеческая пауза перед КАЖДЫМ запросом [min, max] сек.
    humanize_min: float = 0.4
    humanize_max: float = 1.8
    # Мягкий дневной потолок запросов на аккаунт (0 = без ограничения).
    daily_request_cap: int = 0
    # TTL кэша рынка: не перезапрашивать одну карту чаще, чем раз в столько секунд.
    market_ttl: float = 600.0
    accept_language: str = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    rules: Rules = field(default_factory=Rules)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
            cookie=os.getenv("MB_COOKIE", ""),
            cookie_file=os.getenv("MB_COOKIE_FILE", "cookies.txt"),
            user_agent=os.getenv("MB_USER_AGENT", cls.user_agent),
            base_url=os.getenv("BASE_URL", "https://mangabuff.ru"),
            dry_run=os.getenv("DRY_RUN", "1") not in ("0", "false", "False", ""),
            requests_per_minute=int(os.getenv("REQUESTS_PER_MINUTE", "20")),
            fernet_key=os.getenv("FERNET_KEY", ""),
        )
