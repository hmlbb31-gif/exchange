"""Резюмируемый краулер базы рангов из каталога mangabuff.

Зачем: ранг карты есть ТОЛЬКО в каталоге (/cards?rank=R&page=N) — ни на странице
карты, ни на рынке его нет. Каталог огромен (~370k карт, ~6900 страниц), поэтому:
  - обход вежливый (rate limit + backoff на 426/429),
  - с чекпоинтом: прервётся/упрётся в лимит — просто запусти снова, продолжит,
  - пишет прямо на диск в data/card_ranks.json (формат rank -> [card_id]).

Запуск (на твоей машине/сервере, где есть куки mangabuff):
    pip install -r requirements.txt
    # куки: cookies.txt рядом ИЛИ переменная MB_COOKIE; UA — MB_USER_AGENT
    python crawl_ranks.py                 # все ранги, редкие первыми
    python crawl_ranks.py --ranks x,s,a   # только выбранные
    python crawl_ranks.py --rps 4         # запросов в секунду (по умолчанию 4)

Прогресс печатается по ходу. Файл можно грузить в бота: mb.cards.load_rank_db().
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

from config import Config
from mb.cookies import load_cookie

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "card_ranks.json"
STATE_PATH = DATA_DIR / "rank_crawl_state.json"
CARD_ID_RE = re.compile(r'data-card-id="(\d+)"')

# Редкие ранги первыми — быстро закрываем самое ценное (защита fail-closed).
# Базовые + ивентовые (h,n,v,q,l,k) — чтобы --refresh ловил новинки всех рангов.
DEFAULT_RANKS = ["x", "s", "a", "p", "g", "b", "c", "d", "e",
                 "h", "n", "v", "q", "l", "k"]


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save_db(db: dict[str, set[int]]) -> None:
    out = {r.upper(): sorted(ids) for r, ids in db.items() if ids}
    DB_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def crawl(ranks: list[str], rps: float) -> None:
    cfg = Config.from_env()
    cookie = cfg.cookie or load_cookie(cookie_file=cfg.cookie_file)
    if not cookie:
        sys.exit("Нет куки: заполни MB_COOKIE или положи cookies.txt рядом.")

    DATA_DIR.mkdir(exist_ok=True)
    raw = _load_json(DB_PATH, {})
    db: dict[str, set[int]] = {
        r.upper(): set(v) for r, v in raw.items() if isinstance(v, list)
    }
    state = _load_json(STATE_PATH, {})  # rank -> последняя ЗАВЕРШЁННАЯ страница
    delay = 1.0 / max(0.5, rps)

    headers = {
        "User-Agent": cfg.user_agent,
        "Cookie": cookie,
        "X-Requested-With": "XMLHttpRequest",
    }
    with httpx.Client(base_url=cfg.base_url, headers=headers,
                      timeout=cfg.request_timeout, trust_env=False,
                      follow_redirects=True) as client:
        for rank in ranks:
            R = rank.upper()
            db.setdefault(R, set())
            page = int(state.get(R, 0)) + 1
            print(f"[{R}] старт со страницы {page} (уже {len(db[R])} карт)")
            while True:
                try:
                    resp = client.get(f"/cards?rank={rank}&page={page}")
                except httpx.HTTPError as e:
                    print(f"[{R}] сетевая ошибка на стр.{page}: {e}; пауза 10с")
                    time.sleep(10)
                    continue
                if resp.status_code in (426, 429):
                    print(f"[{R}] rate limit ({resp.status_code}) на стр.{page}; пауза 60с")
                    time.sleep(60)
                    continue
                if "/login" in str(resp.url):
                    sys.exit(f"[{R}] редирект на /login — кука протухла или UA не тот.")

                ids = [int(x) for x in CARD_ID_RE.findall(resp.text)]
                if not ids:
                    print(f"[{R}] готово: {len(db[R])} карт")
                    break
                db[R].update(ids)
                state[R] = page
                if page % 20 == 0:
                    _save_db(db)
                    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
                    print(f"[{R}] стр.{page}, всего {len(db[R])} (чекпоинт)")
                page += 1
                time.sleep(delay)

            _save_db(db)
            STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    total = sum(len(v) for v in db.values())
    print(f"\nИтого в базе: {total} карт по {len([r for r in db if db[r]])} рангам -> {DB_PATH}")


def refresh(ranks: list[str], rps: float, stop_after: int) -> None:
    """Пассивная дозагрузка: сканирует каталог с 1-й страницы и дописывает ТОЛЬКО
    новые карты (которых ещё нет в базе). Ранг завершается после `stop_after`
    подряд страниц без единой новой карты — новые карты в каталоге идут первыми,
    поэтому обычно хватает нескольких страниц. Ничего не удаляет, состояние
    основного краула (rank_crawl_state.json) не трогает.
    """
    cfg = Config.from_env()
    cookie = cfg.cookie or load_cookie(cookie_file=cfg.cookie_file)
    if not cookie:
        sys.exit("Нет куки: заполни MB_COOKIE или положи cookies.txt рядом.")

    DATA_DIR.mkdir(exist_ok=True)
    raw = _load_json(DB_PATH, {})
    db: dict[str, set[int]] = {
        r.upper(): set(v) for r, v in raw.items() if isinstance(v, list)
    }
    delay = 1.0 / max(0.5, rps)
    headers = {
        "User-Agent": cfg.user_agent, "Cookie": cookie,
        "X-Requested-With": "XMLHttpRequest",
    }
    total_new = 0
    with httpx.Client(base_url=cfg.base_url, headers=headers,
                      timeout=cfg.request_timeout, trust_env=False,
                      follow_redirects=True) as client:
        for rank in ranks:
            R = rank.upper()
            db.setdefault(R, set())
            page, empty_streak, rank_new = 1, 0, 0
            while True:
                try:
                    resp = client.get(f"/cards?rank={rank}&page={page}")
                except httpx.HTTPError as e:
                    print(f"[{R}] сетевая ошибка стр.{page}: {e}; пауза 10с")
                    time.sleep(10)
                    continue
                if resp.status_code in (426, 429):
                    print(f"[{R}] rate limit; пауза 60с")
                    time.sleep(60)
                    continue
                if "/login" in str(resp.url):
                    sys.exit(f"[{R}] редирект на /login — кука протухла или UA не тот.")

                ids = [int(x) for x in CARD_ID_RE.findall(resp.text)]
                if not ids:
                    break  # конец каталога по этому рангу
                new = [c for c in ids if c not in db[R]]
                if new:
                    db[R].update(new)
                    rank_new += len(new)
                    empty_streak = 0
                else:
                    empty_streak += 1
                    if empty_streak >= stop_after:
                        break
                page += 1
                time.sleep(delay)
            if rank_new:
                _save_db(db)
                total_new += rank_new
                print(f"[{R}] новых карт: {rank_new} (всего в ранге {len(db[R])})")
            else:
                print(f"[{R}] новых нет")
    print(f"\nОбновление завершено: добавлено {total_new} новых карт -> {DB_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ranks", default=",".join(DEFAULT_RANKS),
                    help="список рангов через запятую (по умолчанию все, редкие первыми)")
    ap.add_argument("--rps", type=float, default=4.0, help="запросов в секунду")
    ap.add_argument("--refresh", action="store_true",
                    help="пассивная дозагрузка: искать только НОВЫЕ карты с 1-й страницы")
    ap.add_argument("--stop-after", type=int, default=5,
                    help="в --refresh: остановить ранг после N страниц без новых карт")
    args = ap.parse_args()
    ranks = [r.strip() for r in args.ranks.split(",") if r.strip()]
    if args.refresh:
        refresh(ranks, args.rps, args.stop_after)
    else:
        crawl(ranks, args.rps)


if __name__ == "__main__":
    main()
