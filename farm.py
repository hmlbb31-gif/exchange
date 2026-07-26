"""Фарм дэйликов (алмазы): шахта, игры, календарь, квиз. Мультиаккаунт (~10).

Каждый фарм-аккаунт ведётся отдельным клиентом (своя кука/UA/прокси). Воркёр
уважает DRY_RUN (в сухом прогоне действия не отправляются), делает человеческие
паузы между действиями, соблюдает дневные лимиты и пишет результат в daily_runs,
чтобы не делать одно и то же дважды.

ВНИМАНИЕ: точные тела/ответы фарм-эндпоинтов ещё не сняты живьём (см. PLAN.md,
п.8) — интерпретация ответов сделана терпимой и вынесена в маленькие хелперы;
после живой разведки правится только разбор ответа, не структура воркёра.

Действия берутся из объекта `actions` (утиная типизация — подходит
MangaBuffClient или фейк в тестах): async-методы mine_hit, mine_exchange,
game_play, game_claim, quiz_start, quiz_answer, calendar_claim.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

# Названия задач для daily_runs.
TASK_MINE = "mine"
TASK_GAMES = "games"
TASK_CALENDAR = "calendar"
TASK_QUIZ = "quiz"


@dataclass
class FarmConfig:
    max_mine_hits: int = 100        # верхний предел ударов за день
    max_games: int = 5              # сколько игр сыграть
    mine_exchange: bool = True      # менять руду на алмазы в конце
    # человеческие паузы между действиями (секунды), берётся random в [min, max]
    delay_min: float = 1.5
    delay_max: float = 4.0
    # тихие часы не тут — их держит планировщик (фоновый воркёр)


def _num(resp: dict[str, Any], *keys: str) -> int | None:
    """Достать первое числовое значение по ключам из ответа (терпимо к формату)."""
    for k in keys:
        v = resp.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _is_dry(resp: dict[str, Any]) -> bool:
    return bool(resp.get("dry_run"))


def _stop_signal(resp: dict[str, Any]) -> bool:
    """Похоже, ресурс кончился/действие не прошло (нет энергии, ошибка)."""
    if resp.get("error") or resp.get("errors"):
        return True
    msg = str(resp.get("message", "")).lower()
    return any(w in msg for w in (
        "нет", "недостаточно", "энерг", "лимит", "later",
        "уже", "забра", "получен"))   # «уже забрано», «уже получено» и т.п.


@dataclass
class FarmResult:
    task: str
    status: str          # done | dry_run | skipped | error
    reward: int = 0
    detail: str = ""


class FarmRunner:
    """Выполняет дэйлики для ОДНОГО аккаунта. Пишет учёт в store."""

    def __init__(self, actions: Any, store: Any, account_id: int,
                 cfg: FarmConfig | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rng: random.Random | None = None):
        self.a = actions
        self.store = store
        self.account_id = account_id
        self.cfg = cfg or FarmConfig()
        self._sleep = sleep
        self._rng = rng or random.Random()

    async def _pause(self) -> None:
        await self._sleep(self._rng.uniform(self.cfg.delay_min, self.cfg.delay_max))

    # --- шахта ---
    async def run_mine(self) -> FarmResult:
        total_ore = 0
        hits = 0
        for _ in range(self.cfg.max_mine_hits):
            r = await self.a.mine_hit()
            if _is_dry(r):
                return FarmResult(TASK_MINE, "dry_run", detail="удары не отправлены")
            if _stop_signal(r):
                break
            ore = _num(r, "ore", "amount", "count")
            if ore is None:          # неожиданный ответ — стоп, не долбим вслепую
                break
            total_ore += ore
            hits += 1
            await self._pause()
        diamonds = 0
        if self.cfg.mine_exchange and total_ore > 0:
            ex = await self.a.mine_exchange(total_ore)
            diamonds = _num(ex, "diamonds", "amount") or 0
        return FarmResult(TASK_MINE, "done", reward=diamonds,
                          detail=f"ударов {hits}, руды {total_ore}")

    # --- игры ---
    async def run_games(self) -> FarmResult:
        reward = 0
        played = 0
        for _ in range(self.cfg.max_games):
            r = await self.a.game_play()
            if _is_dry(r):
                return FarmResult(TASK_GAMES, "dry_run", detail="игры не отправлены")
            if _stop_signal(r):
                break
            await self._pause()
            c = await self.a.game_claim()
            reward += _num(c, "diamonds", "reward", "amount") or 0
            played += 1
            await self._pause()
        return FarmResult(TASK_GAMES, "done", reward=reward, detail=f"игр {played}")

    # --- календарь ---
    async def run_calendar(self, day: int | None = None) -> FarmResult:
        d = day if day is not None else date.today().day
        r = await self.a.calendar_claim(d)
        if _is_dry(r):
            return FarmResult(TASK_CALENDAR, "dry_run", detail="награда не забрана")
        if _stop_signal(r):
            return FarmResult(TASK_CALENDAR, "skipped", detail="уже забрано/недоступно")
        return FarmResult(TASK_CALENDAR, "done",
                          reward=_num(r, "diamonds", "reward", "amount") or 0)

    # --- квиз (с накоплением базы ответов) ---
    async def run_quiz(self) -> FarmResult:
        r = await self.a.quiz_start()
        if _is_dry(r):
            return FarmResult(TASK_QUIZ, "dry_run", detail="квиз не запущен")
        question = str(r.get("question", "")).strip()
        if not question:
            return FarmResult(TASK_QUIZ, "skipped", detail="нет вопроса в ответе")
        known = self.store.get_quiz_answer(question)
        if not known:
            # неизвестный вопрос: не угадываем вслепую — логируем на будущее.
            return FarmResult(TASK_QUIZ, "skipped",
                              detail=f"неизвестный вопрос (записан): {question[:60]}")
        res = await self.a.quiz_answer(known)
        # если сайт вернул правильный ответ — запомним/подтвердим.
        correct = res.get("correct_answer") or res.get("answer")
        if correct:
            self.store.set_quiz_answer(question, str(correct))
        ok = bool(res.get("correct", True))
        return FarmResult(TASK_QUIZ, "done" if ok else "error",
                          reward=_num(res, "diamonds", "reward", "amount") or 0)

    # --- оркестрация дня ---
    async def run_all(self, day: str | None = None) -> list[FarmResult]:
        """Выполнить все дэйлики, пропуская уже сделанное сегодня."""
        day = day or date.today().isoformat()
        plan: list[tuple[str, Callable[[], Awaitable[FarmResult]]]] = [
            (TASK_MINE, self.run_mine),
            (TASK_GAMES, self.run_games),
            (TASK_CALENDAR, self.run_calendar),
            (TASK_QUIZ, self.run_quiz),
        ]
        out: list[FarmResult] = []
        for task, fn in plan:
            if self.store.was_done(self.account_id, task, day):
                out.append(FarmResult(task, "skipped", detail="уже сделано сегодня"))
                continue
            try:
                res = await fn()
            except Exception as e:  # один сбойный дэйлик не должен ронять остальные
                res = FarmResult(task, "error", detail=str(e))
            # в daily_runs идёт только реально выполненное (done) — dry_run/skip не блокируют
            if res.status == "done":
                self.store.record_run(self.account_id, task, day, "done", res.reward)
            out.append(res)
            await self._pause()
        return out
