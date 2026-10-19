# Seller-bot + mini CRM — инструкции для ИИ-агентов

Открытый ИИ-продавец для Telegram, WhatsApp и Instagram со встроенной мини-CRM (диалоги, заявки
с карточками в Telegram-группе, заказы, оплата, рассылки, отчёты). Описание — в `README.md`,
архитектура — в `docs/ARCHITECTURE.md`, правила участия — в `CONTRIBUTING.md`.
Язык кода — Python, язык интерфейса и комментариев — русский.

## Стек и запуск
- Python 3.11+, FastAPI + Uvicorn, aiogram 3 (long-polling), SQLAlchemy async + SQLite (WAL),
  APScheduler, openpyxl, python-docx. Админка: Jinja2 + HTMX + Tailwind.
- Локально: `pip install -r requirements.txt`, `cp .env.example .env`, `python -m app.main`,
  админка `http://localhost:8000/admin` (по умолчанию `admin` / `admin`).
- Docker: `docker compose up -d --build`, ждать в логах «Run polling», проверка `GET /health`.

## Проверки перед любым изменением
- `PYTHONIOENCODING=utf-8 python -m pytest -q` — все тесты зелёные.
- `python -m ruff check app/ tests/` (line-length 100; в `app/services/jobs.py` E501 отключён
  намеренно — там промпты).
- Новый модуль = новые тесты в `tests/`.

## База данных
- Новая таблица создаётся сама (`create_all`). **Новая колонка в существующей таблице** — только
  через список `wanted` в `app/db/base.py::_ensure_sqlite_columns`.

## Ключевые места
- Входящие всех каналов: `app/channels/dispatch.py::dispatch_incoming`.
- ИИ-менеджер: `app/sales/ai_manager.py` (системный промпт), `app/sales/tools.py` (инструменты),
  `app/sales/prompts.py` (персона и методика продаж по умолчанию), портрет клиента —
  `app/sales/client_profile.py` + `app/sales/adaptation.py`.
- Заявки: `app/services/leads_service.py` (создание, карточка, кнопки), `app/bot/handlers/leads.py`
  (нажатия и ответы менеджеров), `app/services/leads_reminders.py` (утренняя сводка, возврат из
  ожидания), админка `app/admin/routes/leads.py`.
- Заказы и оплата: `app/services/orders.py`, `app/payments/`, выдача — `app/fulfillment/`.
- Фоновые задачи: `app/scheduler.py` → `app/services/jobs.py`.

## Правила кода
- Одна ответственность на модуль, файлы до ~400 строк, функции до ~50.
- Комментарии объясняют «почему», а не «что», по-русски.
- aiogram: сработавший хендлер останавливает цепочку — узкие фильтры в декораторе.
- Детерминированное (заявка, оплата, выдача доступа, отправка материалов) — кодом; промпт не гарантия.
- Ответ 200 от Meta не значит «доставлено»: вне 24-часового окна WhatsApp/Instagram отказ приходит
  позже вебхуком статуса — перед отправкой проверять `window_open()`.

## Безопасность
- Никаких секретов, телефонов и данных реальных клиентов в коде, тестах и коммитах.
  Секреты — только в `.env` и `runtime/` (оба в `.gitignore`).
