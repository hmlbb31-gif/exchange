"""Модели данных обмена и карт. Чистые dataclass'ы, без сети."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    """Итог оценки обмена."""
    ACCEPT = "accept"      # выгодно и безопасно — принять
    REJECT = "reject"      # невыгодно/опасно — отклонить
    SKIP = "skip"          # недостаточно данных — не трогать (fail-closed)


@dataclass(frozen=True)
class CardRef:
    """Карта в составе обмена: только то, что даёт страница /trades/{id}."""
    card_id: int
    copy_number: int | None = None   # «экз. N», если показан
    palindrome: bool = False          # красивый номер (косметика)


@dataclass(frozen=True)
class Trade:
    """Входящее предложение обмена.

    receive — карты, которые Я ПОЛУЧУ (блок «Вам предлагают», --creator).
    give    — карты, которые Я ОТДАМ  (блок «Вы отдадите», --receiver).
    """
    trade_id: int
    partner_id: int | None
    partner_name: str
    receive: tuple[CardRef, ...]
    give: tuple[CardRef, ...]
    stated_receive: int | None = None   # число из заголовка «Вам предлагают - N»
    stated_give: int | None = None      # число из заголовка «Вы отдадите - M»

    @property
    def counts_consistent(self) -> bool:
        """Совпало ли распарсенное число карт с числом в заголовке.

        Если нет — разметка изменилась или что-то не распарсилось.
        Fail-closed: такой обмен трогать нельзя.
        """
        ok = True
        if self.stated_receive is not None:
            ok = ok and self.stated_receive == len(self.receive)
        if self.stated_give is not None:
            ok = ok and self.stated_give == len(self.give)
        return ok


@dataclass(frozen=True)
class CardInfo:
    """Что бот знает о карте для оценки. Собирается из каталога и рынка."""
    card_id: int
    name: str = ""
    rank: str | None = None          # X, S, A, B, C, D, E ... (None = неизвестно)
    price: float | None = None       # оценка цены в валюте рынка (S); None = неизвестно
    want_count: int | None = None    # сколько людей «Хочу» — сигнал популярности
    lot_count: int | None = None     # сколько лотов продаётся
    request_count: int | None = None # сколько заявок
    protected: bool = False          # ручной whitelist «никогда не отдавать»

    @property
    def known(self) -> bool:
        """Достаточно ли данных, чтобы вообще оценивать. Минимум — ранг или цена."""
        return self.rank is not None or self.price is not None


@dataclass
class Evaluation:
    """Результат оценки одного обмена — с полным объяснением (для сухого прогона)."""
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    received_value: float = 0.0
    given_value: float = 0.0
    gain_ratio: float | None = None

    def add(self, reason: str) -> None:
        self.reasons.append(reason)
