"""Тесты парсера истории обменов на разметке, снятой живьём 2026-07-25."""

from mb.history import parse_history, summarize


def _entry(name, gained_ids, lost_ids):
    g = "".join(f'<a class="history__body-item" href="/cards/{i}/users"></a>' for i in gained_ids)
    l = "".join(f'<a class="history__body-item" href="/cards/{i}/users"></a>' for i in lost_ids)
    return (
        f'<div class="history__item">'
        f'  <div class="history__item-header">'
        f'    <div class="history__name">Обмен с {name}</div>'
        f'    <div class="history__date">25 июля 18:42</div>'
        f'  </div>'
        f'  <div class="history__body history__body--gained">{g}</div>'
        f'  <div class="history__body history__body--lost">{l}</div>'
        f'</div>'
    )


def test_parse_history_gained_lost():
    html = _entry("Приму", [153138, 144920, 303152, 38550], [500001])
    entries = parse_history(html)
    assert len(entries) == 1
    e = entries[0]
    assert e.partner == "Обмен с Приму"
    assert e.gained == (153138, 144920, 303152, 38550)
    assert e.lost == (500001,)
    assert e.ratio == 4.0


def test_parse_multiple_and_summary():
    html = (
        _entry("A", [1, 2], [3]) +          # 2:1
        _entry("B", [4, 5], [6]) +          # 2:1
        _entry("C", [7], [8])               # 1:1
    )
    entries = parse_history(html)
    assert len(entries) == 3
    s = summarize(entries)
    assert s["count"] == 3
    assert s["distribution"]["2:1"] == 2
    assert s["distribution"]["1:1"] == 1
    assert s["avg_ratio"] == round((2 + 2 + 1) / 3, 2)
