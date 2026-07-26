"""Разбор истории обменов: /trades/history (моя) и /users/{id}/trades (чужая).

Разметка снята живьём 2026-07-25:
  .history__item
    .history__name           — «Обмен с <кто>»
    .history__date
    .history__body--gained   — что ПОЛУЧЕНО (.history__body-item -> /cards/{id})
    .history__body--lost     — что ОТДАНО

Полезно для статистики («какой у меня средний размен») и для сверки решений
бота с реальными обменами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ITEM_MARK = "history__item"
NAME_RE = re.compile(r'history__name"[^>]*>\s*([^<]+?)\s*<', re.I)
DATE_RE = re.compile(r'history__date"[^>]*>\s*([^<]+?)\s*<', re.I)
GAINED_MARK = "history__body--gained"
LOST_MARK = "history__body--lost"
BODY_CARD_RE = re.compile(r'href="/cards/(\d+)')


@dataclass(frozen=True)
class HistoryEntry:
    partner: str
    date: str
    gained: tuple[int, ...]   # card_id'ы полученных карт
    lost: tuple[int, ...]     # card_id'ы отданных карт

    @property
    def ratio(self) -> float | None:
        return len(self.gained) / len(self.lost) if self.lost else None


def _split_items(html: str) -> list[str]:
    idxs = [m.start() for m in re.finditer(r'class="[^"]*\bhistory__item\b[^"]*"', html)]
    if not idxs:
        return []
    idxs.append(len(html))
    return [html[idxs[i]:idxs[i + 1]] for i in range(len(idxs) - 1)]


def _cards_in(block: str, start_mark: str, end_marks: tuple[str, ...]) -> tuple[int, ...]:
    s = block.find(start_mark)
    if s == -1:
        return ()
    end = len(block)
    for em in end_marks:
        e = block.find(em, s + len(start_mark))
        if e != -1:
            end = min(end, e)
    return tuple(int(m.group(1)) for m in BODY_CARD_RE.finditer(block[s:end]))


def parse_history(html: str) -> list[HistoryEntry]:
    out: list[HistoryEntry] = []
    for block in _split_items(html):
        name_m = NAME_RE.search(block)
        date_m = DATE_RE.search(block)
        gained = _cards_in(block, GAINED_MARK, (LOST_MARK,))
        lost = _cards_in(block, LOST_MARK, (GAINED_MARK, ITEM_MARK + '"'))
        if not gained and not lost:
            continue
        out.append(HistoryEntry(
            partner=(name_m.group(1).strip() if name_m else ""),
            date=(date_m.group(1).strip() if date_m else ""),
            gained=gained, lost=lost,
        ))
    return out


def summarize(entries: list[HistoryEntry]) -> dict:
    """Средний размен и распределение — для сводки и сверки."""
    ratios = [e.ratio for e in entries if e.ratio is not None]
    dist: dict[str, int] = {}
    for e in entries:
        dist[f"{len(e.gained)}:{len(e.lost)}"] = dist.get(f"{len(e.gained)}:{len(e.lost)}", 0) + 1
    return {
        "count": len(entries),
        "avg_ratio": round(sum(ratios) / len(ratios), 2) if ratios else None,
        "distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
    }
