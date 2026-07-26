"""Тесты фарм-воркёра на фейковых действиях (без сети и без реальных пауз)."""

import asyncio
import random

from mb.farm import (TASK_CALENDAR, TASK_GAMES, TASK_MINE, TASK_QUIZ,
                     FarmConfig, FarmRunner)
from mb.storage import Store


async def _nosleep(_t):  # мгновенные паузы в тестах
    return None


class FakeActions:
    """Утиная замена клиента: очередь ответов на каждый метод."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple] = []

    async def _resp(self, name, *a):
        self.calls.append((name, *a))
        r = self.responses.get(name, {})
        if isinstance(r, list):
            return r.pop(0) if r else {"message": "нет"}
        return r

    async def mine_hit(self): return await self._resp("mine_hit")
    async def mine_exchange(self, ore): return await self._resp("mine_exchange", ore)
    async def game_play(self): return await self._resp("game_play")
    async def game_claim(self): return await self._resp("game_claim")
    async def calendar_claim(self, day): return await self._resp("calendar_claim", day)
    async def quiz_start(self): return await self._resp("quiz_start")
    async def quiz_answer(self, ans): return await self._resp("quiz_answer", ans)


def _runner(actions, store, cfg=None, aid=1):
    return FarmRunner(actions, store, aid, cfg=cfg or FarmConfig(),
                      sleep=_nosleep, rng=random.Random(0))


def test_mine_accumulates_ore_and_exchanges():
    acts = FakeActions(
        mine_hit=[{"ore": 5}, {"ore": 5}, {"ore": 5}, {"message": "нет энергии"}],
        mine_exchange={"diamonds": 12})
    r = _runner(acts, Store())
    res = asyncio.run(r.run_mine())
    assert res.status == "done"
    assert res.reward == 12          # алмазы с обмена руды
    assert "руды 15" in res.detail   # 3 удара по 5
    assert "ударов 3" in res.detail


def test_mine_dry_run_does_not_loop():
    acts = FakeActions(mine_hit={"dry_run": True, "action": "mine_hit"})
    res = asyncio.run(_runner(acts, Store()).run_mine())
    assert res.status == "dry_run"
    assert acts.calls.count(("mine_hit",)) == 1   # ровно один символический удар


def test_games_play_and_claim():
    acts = FakeActions(game_play={"ok": 1}, game_claim={"diamonds": 60})
    res = asyncio.run(_runner(acts, Store(), cfg=FarmConfig(max_games=3)).run_games())
    assert res.status == "done"
    assert res.reward == 180          # 3 игры × 60
    assert "игр 3" in res.detail


def test_calendar_claim_done_and_skip():
    done = asyncio.run(_runner(FakeActions(calendar_claim={"diamonds": 25}), Store())
                       .run_calendar(day=26))
    assert done.status == "done" and done.reward == 25
    skip = asyncio.run(_runner(FakeActions(calendar_claim={"message": "уже забрано"}),
                               Store()).run_calendar(day=26))
    assert skip.status == "skipped"


def test_quiz_known_answer():
    store = Store()
    store.set_quiz_answer("Столица Японии?", "Токио")
    acts = FakeActions(quiz_start={"question": "Столица Японии?"},
                       quiz_answer={"correct": True, "diamonds": 40})
    res = asyncio.run(_runner(acts, store).run_quiz())
    assert res.status == "done" and res.reward == 40
    assert ("quiz_answer", "Токио") in acts.calls


def test_quiz_unknown_is_skipped_not_guessed():
    acts = FakeActions(quiz_start={"question": "Неизвестный вопрос?"})
    res = asyncio.run(_runner(acts, Store()).run_quiz())
    assert res.status == "skipped"
    # не пытались отвечать вслепую
    assert not any(c[0] == "quiz_answer" for c in acts.calls)


def test_run_all_skips_already_done_and_records():
    store = Store()
    aid = store.add_account("f", "c", "UA")
    store.record_run(aid, TASK_MINE, "2026-07-26", "done", 10)  # шахта уже сделана
    acts = FakeActions(
        mine_hit=[{"ore": 5}], mine_exchange={"diamonds": 5},
        game_play={"ok": 1}, game_claim={"diamonds": 60},
        calendar_claim={"diamonds": 25},
        quiz_start={"question": "?"})   # квиз неизвестен -> skipped
    r = FarmRunner(acts, store, aid, cfg=FarmConfig(max_games=1),
                   sleep=_nosleep, rng=random.Random(0))
    results = asyncio.run(r.run_all(day="2026-07-26"))
    by = {res.task: res for res in results}
    assert by[TASK_MINE].status == "skipped"      # не делаем повторно
    assert by[TASK_GAMES].status == "done"
    assert by[TASK_CALENDAR].status == "done"
    assert by[TASK_QUIZ].status == "skipped"
    # games записались в daily_runs
    assert store.was_done(aid, TASK_GAMES, "2026-07-26") is True
    assert store.farm_summary("2026-07-26")[TASK_GAMES] == 60
