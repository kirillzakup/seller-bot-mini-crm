# Деплой seller-bot на seller-bot.example.com

Один Docker-контейнер (бот long-polling + FastAPI: админка, лендинги, страница оплаты, вебхуки,
планировщик). Системный nginx на сервере проксирует поддомен на `127.0.0.1:8000`.

> У бота свой `BOT_TOKEN` (создайте нового у @BotFather).
> Telegram работает на polling, поэтому вебхук Telegram настраивать НЕ нужно.

## Что нужно на сервере
- VPS с Docker + docker compose, системный nginx, certbot.
- Доступ к DNS зоны `example.com`.

## Шаги

**1. DNS.** Добавьте A-запись: `seller-bot.example.com` → IP вашего VPS.

**2. Новый бот.** В @BotFather создайте бота для продаж → получите токен.
Токен можно ввести **в админке** (⚙️ Настройки → «Токен бота») уже после запуска — бот стартует сразу,
без перезапуска контейнера. `BOT_TOKEN` в `.env` — необязательный резервный вариант.

**3. Код на сервер.** Скопируйте корень проекта на сервер, например в `/opt/seller-bot`:
```bash
# с локальной машины (PowerShell/bash), из корня проекта, исключая мусор:
# ВАЖНО: --exclude .env — иначе локальный .env перезапишет рабочий на сервере
# (сбросит BASE_URL/SECRET_KEY/пароль админки). Секреты на сервере правим прямо там.
rsync -av --exclude .venv --exclude data --exclude .git --exclude graphify-out --exclude .env \
      ./  deploy@example.com:/opt/seller-bot/
# или: git clone https://github.com/kirillzakup/seller-bot-mini-crm.git /opt/seller-bot
```

**4. Конфиг.** На сервере:
```bash
cd /opt/seller-bot
cp .env.production.example .env
nano .env   # ADMIN_PASSWORD, SECRET_KEY (openssl rand -hex 32) — обязательно
#            BOT_TOKEN, LLM_API_KEY — можно оставить пустыми и ввести позже в админке
#            BASE_URL уже = https://seller-bot.example.com
```

**5. Запуск контейнера.**
```bash
docker compose up -d --build
docker compose logs -f sellerbot   # проверить, что бот стартовал: "Bot started: @..."
```

**6. nginx.**
```bash
sudo cp deploy/nginx-seller-bot.conf /etc/nginx/sites-available/seller-bot.example.com
sudo ln -s /etc/nginx/sites-available/seller-bot.example.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**7. SSL.**
```bash
sudo certbot --nginx -d seller-bot.example.com
```

**8. Проверка.**
```bash
curl -s https://seller-bot.example.com/health     # {"status":"ok"}
```
Откройте `https://seller-bot.example.com/admin` (логин/пароль из `.env`).

**9. Настройка в админке** (всё применяется сразу, без перезапуска):
- **⚙️ Настройки** — **Токен бота** (бот запускается сразу после сохранения), ИИ (base_url/api_key/model), `chat_id` операторов.
- **⚙️ Настройки → Голос (ElevenLabs)** — голосовые сообщения: включить, вставить API key + Voice ID, выбрать режим (зеркально/всегда/по запросу) и формат (кружок/mp3). ffmpeg уже в образе — «кружок» работает из коробки.
- **💳 Способы оплаты** — Kaspi/перевод (ручной) или «Ссылка на оплату»; шлюзы по желанию.
- **📦 Товары**, **📚 База знаний**, **🎨 Креативы** (ссылки-лендинги: `https://seller-bot.example.com/l/<код>`).
- **📡 Каналы** (WhatsApp/Instagram, Meta): Callback URL → `https://seller-bot.example.com/webhooks/meta`.
- Платёжные шлюзы (если включите): callback → `https://seller-bot.example.com/webhooks/pay/{epay|freedompay|yookassa|cryptobot}`.

## Обновление версии
```bash
# залить новый код (rsync как в шаге 3), затем:
cd /opt/seller-bot && docker compose up -d --build && curl -s https://seller-bot.example.com/health
```

## Бэкап данных
Все данные (SQLite + чеки) — в томе `sellerbot-data`:
```bash
docker run --rm -v seller-bot_sellerbot-data:/data -v $PWD:/backup alpine \
  tar czf /backup/sellerbot-backup-$(date +%F).tar.gz -C /data .
```

## Заметки
- `BASE_URL` обязан совпадать с доменом — от него строятся ссылки на оплату, лендинги и кнопка бота.
- Для масштаба SQLite можно заменить на PostgreSQL через `DATABASE_URL` (миграции через Alembic — в планах).
