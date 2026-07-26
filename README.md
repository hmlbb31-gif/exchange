# MangaBuff авто-обменник

Telegram-бот, который автоматически рассматривает входящие обмены на mangabuff.ru
и принимает только выгодные и безопасные по строгим (fail-closed) правилам.
Работает на твоих собственных аккаунтах («твинках»).

**Управление полностью кнопочное** — команды вводить не нужно. Открой бота
(`/start`), нажми «🔗 Привязать аккаунт», пришли строку cookie одним сообщением —
бот проверит её живым заходом на `/trades`, сохранит зашифрованно в БД и привяжет
аккаунт. Кука переживает перезапуск, так что `.env`/`cookies.txt` больше не
обязательны (нужны только токен бота и `ADMIN_IDS`).

## Структура

```
.
├── bot.py              — запуск Telegram-бота:  python bot.py
├── run_dry.py          — сухой прогон без Telegram (диагностика решений)
├── crawl_ranks.py      — сбор/обновление базы рангов (--refresh)
├── dump_trades.py      — дамп страницы обменов для отладки парсера
├── config.py           — настройки и пороги (класс Rules)
├── mb/                 — ядро пакета
│   ├── client.py       — HTTP-клиент (сессия, прокси, детект «протух логин»)
│   ├── parser.py       — разбор HTML обменов
│   ├── decision.py     — движок правил (fail-closed)
│   ├── market.py       — трекер рынка и опорные цены
│   ├── cards.py        — база рангов + провайдер CardInfo
│   ├── trader.py       — оркестрация одной итерации обмена
│   ├── worker.py       — цикл опроса и фоновые задачи
│   ├── storage.py      — SQLite (обмены, аккаунты, куки)
│   ├── cookies.py      — загрузка cookie из .env/cookies.txt
│   ├── models.py       — датаклассы предметной области
│   ├── history.py      — разбор истории обменов
│   └── farm.py         — задачи фарма
├── tests/              — pytest-тесты
├── data/               — card_ranks.json + БД trade.sqlite3 (создаётся)
├── requirements.txt    — ядро (httpx)
└── requirements-bot.txt— + aiogram, cryptography (нужен Python 3.12/3.13)
```

## Установка и запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-bot.txt
cp .env.example .env      # впиши EXCHANGE_BOT_TOKEN, ADMIN_IDS, FERNET_KEY
python bot.py
```

Дальше — в самом Telegram: `/start` → «🔗 Привязать аккаунт» → вставить cookie.
Куку можно не класть в `.env` вообще. Кнопки меню: включить/выключить авто-обмен,
один проход, статус, правила, статистика.

Сухой прогон логики без Telegram:

```bash
python run_dry.py
```

## Тесты

```bash
pip install pytest pytest-asyncio httpx
pytest
```

## Правила решений (кратко)

Движок `mb/decision.py` работает по принципу **fail-closed**: любая неопределённость
(неизвестный ранг, нет рыночных данных для дорогой карты) ведёт к отказу/пропуску,
а не к риску. Ключевые настройки в `config.py → Rules`:

- `min_gain_ratio` — базовый минимальный выигрыш (по умолчанию x2).
- `min_gain_ratio_by_rank` — индивидуальные пороги по рангу отдаваемой карты
  (ценные ранги — строже).
- `basket_guard_ratio` — анти-разбавление: лучшая отдаваемая карта должна быть
  покрыта равноценной полученной (защита от «слил ценную карту в куче мусора»).
- `protect_palindrome` / `protect_copy_number_below` — беречь «красивые» номера
  экземпляров и ранние копии.
- `protected_cards` / `protected_ranks` — белые списки, которые никогда не отдаём.
```
