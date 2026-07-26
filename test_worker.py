"""Тесты фоновых воркёров: тихие часы, проход обменника, дневной раунд фарма."""

import asyncio

from config import Config
from mb.client import SessionDead
from mb.farm import FarmConfig, FarmRunner
from mb.models import CardRef, Evaluation, Trade, Verdict
from mb.storage import ROLE_FARM, Store
from mb.trader import TradeOutcome
from mb.worker import TradeLoop, daily_farm_round, is_quiet_hour


def test_is_quiet_hour_basic_and_wraparound():
    assert is_quiet_hour(3, 2, 8) is True
    assert is_quiet_hour(9, 2, 8) is False
    assert is_quiet_hour(1, 23, 6) is True     # через полночь
    assert is_quiet_hour(23, 23, 6) is True
    assert is_quiet_hour(12, 23, 6) is False
    assert is_quiet_hour(5, 5, 5) is False     # start==end -> тихих часов нет


def _trade(tid, recv=1, give=1):
    return Trade(tid, 2, "Partner",
                 receive=tuple(CardRef(i) for i in range(recv)),
                 give=tuple(CardRef(100 + i) for i in range(give)),
                 stated_receive=recv, stated_give=give)


class FakeTrader:
    def __init__(self, outcomes):
        self._o = outcomes

    async def process_once(self):
        return self._o


def test_run_once_logs_all_and_notifies_only_accept():
    store = Store()
    ev_a = Evaluation(verdict=Verdict.ACCEPT); ev_a.add("выгодно")
    ev_s = Evaluation(verdict=Verdict.SKIP); ev_s.add("нет данных")
    outs = [TradeOutcome(_trade(1), ev_a, True, {"dry_run": True}),
            TradeOutcome(_trade(2), ev_s, False, None)]
    msgs: list[str] = []

    async def notify(m):
        msgs.append(m)

    loop = TradeLoop(FakeTrader(outs), store, notifier=notify)
    asyncio.run(loop.run_once())
    assert store.decision_counts() == {"accept": 1, "skip": 1}  # value в нижнем регистре
    assert any("Принят обмен #1" in m for m in msgs)
    assert not any("#2" in m for m in msgs)


class _FakeActions:
    async def mine_hit(self): return {"dry_run": True}
    async def mine_exchange(self, o): return {"dry_run": True}
    async def game_play(self): return {"dry_run": True}
    async def game_claim(self): return {"dry_run": True}
    async def calendar_claim(self, d): return {"dry_run": True}
    async def quiz_start(self): return {"dry_run": True}
    async def quiz_answer(self, a): return {"dry_run": True}


class _FakeClientCM:
    def __init__(self, cfg):
        self.cfg = cfg

    async def __aenter__(self):
        if self.cfg.cookie == "DEAD":
            raise SessionDead("/login")
        return _FakeActions()

    async def __aexit__(self, *a):
        return False


def test_daily_farm_round_marks_ok_and_deactivates_dead():
    db = Store()
    good = db.add_account("g", "sess", "UA", proxy="http://p", role=ROLE_FARM)
    dead = db.add_account("d", "DEAD", "UA", role=ROLE_FARM)
    msgs: list[str] = []

    async def notify(m):
        msgs.append(m)

    res = asyncio.run(daily_farm_round(
        Config(dry_run=True), db, FarmRunner, _FakeClientCM,
        notifier=notify, day="2026-07-26",
        farm_cfg=FarmConfig(delay_min=0, delay_max=0)))

    assert good in res and dead not in res
    assert db.get_account(good).last_ok is not None
    assert db.get_account(dead).active is False
    assert any("протухла" in m for m in msgs)
    assert any("Фарм [g]" in m for m in msgs)
