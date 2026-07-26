"""HTTP-клиент к mangabuff.ru: рейт-лимит, CSRF, контроль сессии, эндпоинты обмена и фарма.

Эндпоинты сняты живьём 2026-07-25 (см. PLAN.md). Действия шлются POST с CSRF:
токен из meta[name="csrf-token"], заголовок X-CSRF-TOKEN + X-Requested-With.

ВНИМАНИЕ: DDoS-Guard привязывает куки к User-Agent. UA обязан совпадать
с браузером, из которого экспортированы куки, иначе редирект на /login.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time

import httpx

log = logging.getLogger(__name__)

RATE_LIMIT_STATUSES = {426, 429}
CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


class SessionDead(Exception):
    """Кука протухла — чинит человек (обновить cookies)."""


class RateLimited(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class TokenBucket:
    """Глобальный ограничитель запросов. Один на приложение."""

    def __init__(self, rate_per_minute: int):
        self.capacity = max(1, rate_per_minute)
        self.tokens = float(self.capacity)
        self.refill_per_sec = self.capacity / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.updated) * self.refill_per_sec,
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.refill_per_sec)


def _client_hints(user_agent: str) -> dict[str, str]:
    """sec-ch-ua заголовки в тон Chrome-версии из UA (реальный Chrome их всегда шлёт)."""
    m = re.search(r"Chrome/(\d+)", user_agent)
    ver = m.group(1) if m else "150"
    platform = '"Windows"' if "Windows" in user_agent else (
        '"macOS"' if "Mac OS" in user_agent else '"Linux"')
    return {
        "sec-ch-ua": f'"Chromium";v="{ver}", "Google Chrome";v="{ver}", '
                     f'"Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }


class MangaBuffClient:
    def __init__(self, config):
        self.config = config
        self.bucket = TokenBucket(config.requests_per_minute)
        self._client: httpx.AsyncClient | None = None
        self._csrf: str | None = None
        self.session_alive = True
        self._rng = random.Random()
        self._day = ""
        self._day_count = 0

    async def __aenter__(self) -> "MangaBuffClient":
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": getattr(self.config, "accept_language",
                                       "ru-RU,ru;q=0.9,en;q=0.8"),
            "Referer": self.config.base_url.rstrip("/") + "/",
            "X-Requested-With": "XMLHttpRequest",
            **_client_hints(self.config.user_agent),
        }
        if self.config.cookie:
            headers["Cookie"] = self.config.cookie
        # Прокси на аккаунт (для фарм-аккаунтов). Обменник обычно без прокси.
        proxy = getattr(self.config, "proxy", None) or None
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.request_timeout,
            follow_redirects=True,
            http2=False,
            headers=headers,
            proxy=proxy,
            trust_env=False,  # принципиально: не уводить куки через прокси окружения
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    def _tick_daily(self) -> None:
        """Дневной потолок запросов на аккаунт (мягкая защита от аномальной активности)."""
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._day_count = today, 0
        self._day_count += 1
        cap = getattr(self.config, "daily_request_cap", 0) or 0
        if cap and self._day_count > cap:
            raise RateLimited(429)  # исчерпан дневной лимит — ведём себя как при 429

    async def _humanize(self) -> None:
        """Случайная человеческая пауза перед запросом (не машинно-мгновенно)."""
        lo = getattr(self.config, "humanize_min", 0.0) or 0.0
        hi = getattr(self.config, "humanize_max", 0.0) or 0.0
        if hi > 0:
            await asyncio.sleep(self._rng.uniform(lo, hi))

    # --- низкий уровень ---
    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        self._tick_daily()
        await self.bucket.acquire()
        await self._humanize()
        assert self._client is not None
        resp = await self._client.request(method, path, **kw)
        if resp.status_code in RATE_LIMIT_STATUSES:
            raise RateLimited(resp.status_code)
        if resp.status_code in (301, 302) and "/login" in resp.headers.get("location", ""):
            self.session_alive = False
            raise SessionDead(path)
        return resp

    async def get(self, path: str) -> str:
        resp = await self._request("GET", path)
        text = resp.text
        if 'name="csrf-token"' in text and not self._csrf:
            m = CSRF_RE.search(text)
            if m:
                self._csrf = m.group(1)
        if '/login' in str(resp.url) and "trade" not in text[:2000]:
            self.session_alive = False
            raise SessionDead(path)
        return text

    async def _csrf_token(self) -> str:
        if not self._csrf:
            await self.get("/trades")  # любая страница отдаёт meta csrf-token
        if not self._csrf:
            raise SessionDead("csrf-token недоступен (сессия?)")
        return self._csrf

    async def post(self, path: str, data: dict | None = None) -> httpx.Response:
        token = await self._csrf_token()
        headers = {"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest"}
        return await self._request("POST", path, json=data or {}, headers=headers)

    # --- обмены ---
    async def health_check(self) -> bool:
        """Лёгкая проверка живости сессии: GET / и детект редиректа на /login."""
        try:
            await self.get("/")
        except SessionDead:
            return False
        return self.session_alive

    async def fetch_trades_list(self) -> str:
        return await self.get("/trades")

    async def fetch_trade_detail(self, trade_id: int) -> str:
        return await self.get(f"/trades/{trade_id}")

    async def accept_trade(self, trade_id: int) -> dict:
        """POST /trades/{id}/accept. Уважает dry_run: в сухом прогоне НЕ шлёт."""
        if self.config.dry_run:
            log.info("[DRY_RUN] accept_trade(%s) — не отправлено", trade_id)
            return {"dry_run": True, "action": "accept", "trade_id": trade_id}
        resp = await self.post(f"/trades/{trade_id}/accept", {})
        return _json(resp)

    async def reject_trade(self, trade_id: int) -> dict:
        if self.config.dry_run:
            log.info("[DRY_RUN] reject_trade(%s) — не отправлено", trade_id)
            return {"dry_run": True, "action": "reject", "trade_id": trade_id}
        resp = await self.post(f"/trades/{trade_id}/reject", {})
        return _json(resp)

    # --- рынок / цены ---
    async def fetch_market_card(self, card_id: int) -> str:
        return await self.get(f"/market/card/{card_id}")

    async def fetch_offers_want(self, card_id: int) -> str:
        return await self.get(f"/cards/{card_id}/offers/want")

    async def fetch_requests(self, card_id: int) -> str:
        return await self.get(f"/market/requests?card_id={card_id}")

    # --- фарм (для второго этапа) ---
    async def mine_hit(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "mine_hit"}
        return _json(await self.post("/mine/hit", {}))

    async def mine_exchange(self, diamonds: int) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "mine_exchange", "diamonds": diamonds}
        return _json(await self.post("/mine/exchange", {"diamonds": diamonds}))

    async def mine_strong_hit(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "mine_strong_hit"}
        return _json(await self.post("/mine/buy-strong-hit", {}))

    async def mine_upgrade(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "mine_upgrade"}
        return _json(await self.post("/mine/upgrade", {}))

    async def game_play(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "game_play"}
        return _json(await self.post("/card-game/play", {}))

    async def game_claim(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "game_claim"}
        return _json(await self.post("/card-game/claim", {}))

    async def quiz_start(self) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "quiz_start"}
        return _json(await self.post("/quiz/start", {}))

    async def quiz_answer(self, answer: str) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "quiz_answer", "answer": answer}
        return _json(await self.post("/quiz/answer", {"answer": answer}))

    async def calendar_claim(self, day: int) -> dict:
        if self.config.dry_run:
            return {"dry_run": True, "action": "calendar_claim", "day": day}
        return _json(await self.post(f"/balance/claim/{day}", {}))


def _json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text[:200]}


def config_for_account(base, account):
    """Собрать Config под конкретный аккаунт: своя кука/UA/прокси, остальное из base.

    `account` — утиный объект с полями cookie, user_agent, proxy (напр.
    mb.storage.Account). Используется фарм-воркёром для мультиаккаунта.
    """
    from dataclasses import replace
    return replace(base, cookie=account.cookie, user_agent=account.user_agent,
                   proxy=getattr(account, "proxy", None))
