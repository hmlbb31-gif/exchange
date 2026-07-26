"""Тесты загрузчика базы рангов + проверка реального сид-файла data/card_ranks.json."""

import json
from pathlib import Path

from mb.cards import CardStore, load_rank_db

# Сид-база рангов. В классической раскладке лежит в exchange/data/, в этом
# (флэттенизированном) репозитории — рядом с модулями в корне. Берём тот путь,
# который реально существует, чтобы тест не зависел от структуры каталогов.
_ROOT = Path(__file__).parent
DB = next(
    (p for p in (_ROOT / "data" / "card_ranks.json",
                 _ROOT.parent / "data" / "card_ranks.json",
                 _ROOT / "card_ranks.json")
     if p.exists()),
    _ROOT / "card_ranks.json",
)


def test_load_rank_db_inverts_and_skips_meta(tmp_path):
    p = tmp_path / "ranks.json"
    p.write_text(json.dumps({
        "_note": "служебное",
        "X": [100, 200],
        "S": [300],
    }), encoding="utf-8")
    db = load_rank_db(p)
    assert db == {100: "X", 200: "X", 300: "S"}


def test_real_rank_db_loads_and_resolves():
    # Реальная база из каталога (после crawl_ranks.py). Проверяем, что она грузится,
    # ранги — из известного набора, и карта резолвится через CardStore.
    db = load_rank_db(DB)
    assert len(db) >= 100
    # базовые + ивентовые ранги каталога
    known = {"X", "S", "A", "B", "C", "D", "E", "P", "G",
             "H", "N", "V", "Q", "L", "K"}
    assert set(db.values()) <= known
    store = CardStore(ranks=db)
    some_id = next(iter(db))
    assert store.get(some_id).rank in known
