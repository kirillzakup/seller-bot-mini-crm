"""Аналитика чатов: метрики, фильтр настроек, применение/откат, выгрузки.

Тестовая БД общая на весь прогон, поэтому тут два уровня изоляции:

1. Каждый тест сеет данные в СВОЁ окно дат (2019 год) и считает метрики по нему —
   проверки остаются точными независимо от порядка тестов.
2. :class:`_Sandbox` удаляет за собой пользователей, сообщения и заказы и
   возвращает изменённые настройки. Без этого посеянные клиенты попадали, например,
   в тест дожимов: он ищет «молчащих» по всей базе и находил наших.
"""

from __future__ import annotations

import asyncio
import csv
import io
import random
from datetime import date, datetime, timedelta

from sqlalchemy import delete

from app.db.base import AsyncSessionLocal
from app.db.models import ChatReport, Message, Order, Product, User
from app.db.models.enums import Channel, ConversationRole, OrderStatus
from app.services import chat_report as svc
from app.services import contacts_export as cx
from app.services.settings_service import get_setting, set_setting

_TRACKED_SETTINGS = ("sales_script", "welcome_text", "llm", "tz_offset_hours")


class _Sandbox:
    """Сеет данные и гарантированно убирает их за собой."""

    def __init__(self, session):
        self.s = session
        self.user_ids: list[int] = []
        self.product_ids: list[int] = []
        self.report_ids: list[int] = []
        self._settings: dict[str, object] = {}

    async def __aenter__(self):
        for key in _TRACKED_SETTINGS:
            self._settings[key] = await get_setting(self.s, key)
        return self

    async def __aexit__(self, *exc):
        if self.user_ids:
            await self.s.execute(delete(Message).where(Message.user_id.in_(self.user_ids)))
            await self.s.execute(delete(Order).where(Order.user_id.in_(self.user_ids)))
            await self.s.execute(delete(User).where(User.id.in_(self.user_ids)))
        if self.product_ids:
            await self.s.execute(delete(Product).where(Product.id.in_(self.product_ids)))
        if self.report_ids:
            await self.s.execute(delete(ChatReport).where(ChatReport.id.in_(self.report_ids)))
        for key, value in self._settings.items():
            await set_setting(self.s, key, value)
        await self.s.commit()
        return False

    async def dialog(
        self,
        *,
        texts: list[tuple[str, str]],
        when: datetime,
        phone: str | None = None,
        paid_amount: int | None = None,
        channel: Channel = Channel.TELEGRAM,
        external_id: str | None = None,
        username: str | None = None,
    ) -> User:
        """Клиент с перепиской в заданный момент; при paid_amount — плюс оплаченный заказ."""
        n = random.randint(1, 10**9)
        user = User(
            channel=channel,
            external_id=external_id or str(n),
            telegram_id=n,
            username=username,
            first_name="Иван",
            last_name="Петров",
            phone=phone,
            created_at=when,
            # чтобы посеянные клиенты не попали в чужие тесты дожимов
            reminders_enabled=False,
        )
        self.s.add(user)
        await self.s.flush()
        self.user_ids.append(user.id)

        for i, (role, text) in enumerate(texts):
            self.s.add(
                Message(
                    user_id=user.id,
                    role=ConversationRole(role),
                    content=text,
                    created_at=when + timedelta(minutes=i * 2),
                )
            )
        if paid_amount:
            product = Product(sku=f"sku-{n}", title="Кирпич", price_kzt=paid_amount)
            self.s.add(product)
            await self.s.flush()
            self.product_ids.append(product.id)
            self.s.add(
                Order(
                    code=f"ORD{n}",
                    user_id=user.id,
                    product_id=product.id,
                    amount_kzt=paid_amount,
                    status=OrderStatus.PAID,
                    created_at=when,
                )
            )
        await self.s.flush()
        return user

    def track_report(self, report: ChatReport) -> None:
        self.report_ids.append(report.id)


def test_metrics_counted_in_python():
    """Числа берутся из базы, а не от модели: диалоги, ответы, конверсия, обрывы."""

    async def main():
        day = datetime(2019, 3, 15, 12, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Сколько стоит товар?"), ("assistant", "340 тенге за штуку")],
                when=day,
                phone="+77071112233",
                paid_amount=500_000,
            )
            # диалог, оборванный на реплике клиента — бот не ответил
            await box.dialog(texts=[("user", "А доставка в Павлодар есть?")], when=day)

            metrics, user_ids = await svc.collect_metrics(
                s, *svc.period_bounds(day.date(), day.date())
            )

            assert metrics["dialogs"] == 2
            assert metrics["messages"] == 3
            assert metrics["messages_client"] == 2
            assert metrics["orders_paid"] == 1
            assert metrics["revenue_kzt"] == 500_000
            assert metrics["with_phone"] == 1
            assert metrics["conversion_pct"] == 50.0
            assert metrics["unanswered_dialogs"] == 1
            assert metrics["unanswered_pct"] == 50.0
            assert metrics["avg_reply_seconds"] == 120  # две минуты между репликами
            assert len(user_ids) == 2

    asyncio.run(main())


def test_period_filter_excludes_other_days():
    """Диалоги вне выбранного периода в разбор не попадают."""

    async def main():
        day = datetime(2019, 4, 20, 10, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Старый вопрос"), ("assistant", "Старый ответ")], when=day
            )
            other = date(2019, 4, 25)
            metrics, user_ids = await svc.collect_metrics(s, *svc.period_bounds(other, other))
            assert metrics["dialogs"] == 0
            assert user_ids == []

    asyncio.run(main())


def test_sanitize_settings_blocks_secrets():
    """Модель не должна иметь возможности переписать ключи, токены и доступы."""
    clean = svc.sanitize_settings(
        {
            "welcome_text": "Здравствуйте!",
            "llm": {"api_key": "sk-украли"},
            "bot_token": "123:ABC",
            "meta": {"app_secret": "s3cret"},
            "followup": {"silence_hours": 5, "api_key": "нет"},
            "operator_chat_id": -100500,
        }
    )
    assert clean == {"welcome_text": "Здравствуйте!", "followup": {"silence_hours": 5}}
    assert "llm" not in clean and "bot_token" not in clean and "meta" not in clean
    # даже внутри разрешённой настройки посторонний подключ отфильтрован
    assert "api_key" not in clean["followup"]


def test_apply_and_rollback_restores_previous_values():
    """Применение правит настройки, откат возвращает ровно то, что было."""

    async def main():
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await set_setting(s, "sales_script", "СТАРЫЙ скрипт")
            await set_setting(s, "welcome_text", "Старое приветствие")

            report = ChatReport(
                period_from=date(2019, 5, 1),
                period_to=date(2019, 5, 31),
                metrics={},
                summary="",
                findings=[],
                proposals={
                    "sales_script": "НОВЫЙ скрипт",
                    "settings": {"welcome_text": "Новое приветствие"},
                    "segments": [],
                },
            )
            s.add(report)
            await s.flush()
            box.track_report(report)

            ok, msg = await svc.apply_report(s, report.id)
            assert ok, msg
            assert await get_setting(s, "sales_script") == "НОВЫЙ скрипт"
            assert await get_setting(s, "welcome_text") == "Новое приветствие"
            assert report.status == "applied"

            again, _ = await svc.apply_report(s, report.id)  # повторно нельзя
            assert again is False

            ok, _ = await svc.rollback_report(s, report.id)
            assert ok
            assert await get_setting(s, "sales_script") == "СТАРЫЙ скрипт"
            assert await get_setting(s, "welcome_text") == "Старое приветствие"
            assert report.status == "rolled_back"

    asyncio.run(main())


def test_generate_report_requires_llm():
    """Без настроенной модели разбор не запускается и объясняет причину."""

    async def main():
        day = datetime(2019, 6, 10, 9, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(texts=[("user", "Вопрос"), ("assistant", "Ответ")], when=day)
            await set_setting(s, "llm", {"base_url": "", "api_key": ""})
            report, error = await svc.generate_report(
                s, period_from=day.date(), period_to=day.date()
            )
            assert report is None
            assert "LLM не настроен" in error

    asyncio.run(main())


# ─────────────────────────── выгрузки ───────────────────────────


def test_detect_city_finds_kz_cities():
    assert cx.detect_city("Нужен товар в Павлодар") == ("Павлодар", "Павлодарская область")
    assert cx.detect_city("доставка до Усть-Каменогорска")[0] == "Усть-Каменогорск"
    assert cx.detect_city("Здравствуйте, сколько стоит?") == ("", "")


def test_normalize_phone_to_international():
    assert cx.normalize_phone("87071112233") == "+77071112233"
    assert cx.normalize_phone("7071112233") == "+77071112233"
    assert cx.normalize_phone("+7 (707) 111-22-33") == "+77071112233"
    assert cx.normalize_phone("") == ""
    assert cx.normalize_phone(None) == ""


def test_meta_csv_matches_template_header():
    """Шапка файла должна совпадать с шаблоном Meta до символа."""

    async def main():
        day = datetime(2019, 7, 7, 11, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(texts=[("user", "Нужен товар в Астану"), ("assistant", "Привезём")],
                when=day,
                phone="+77071112233",
                paid_amount=250_000,
            )
            contacts = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=True
            )
            assert len(contacts) == 1
            assert contacts[0]["city"] == "Астана"
            assert contacts[0]["value"] == 250_000

            text = cx.meta_audience_csv(contacts)
            rows = list(csv.reader(io.StringIO(text)))
            assert rows[0] == [
                "email", "email", "email",
                "phone", "phone", "phone",
                "madid", "fn", "ln", "zip", "ct", "st", "country",
                "dob", "doby", "gen", "age", "uid", "value",
            ]
            row = rows[1]
            assert row[3] == "+77071112233"   # phone
            assert row[7] == "Иван"            # fn
            assert row[10] == "Астана"         # ct
            assert row[12] == "KZ"             # country
            assert row[18] == "250000"         # value — сумма оплат
            assert not text.startswith("﻿")  # без BOM: Ads Manager иначе спотыкается

    asyncio.run(main())


def test_simple_csv_has_context_and_bom():
    """Упрощённая выгрузка: контекст диалога есть, кириллица открывается в Excel."""

    async def main():
        day = datetime(2019, 8, 8, 14, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(texts=[
                    ("user", "Здравствуйте, нужен товар на объект в Караганде"),
                    ("assistant", "Подскажу"),
                    ("user", "Сколько выйдет на 30 метров?"),
                ],
                when=day,
                phone="87071112233",
            )
            contacts = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=False
            )
            text = cx.simple_csv(contacts)

            assert text.startswith("﻿")  # BOM для Excel
            rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
            assert rows[0] == [
                "Имя", "Телефон", "Аккаунт", "Город", "Регион",
                "Контекст диалога", "Канал", "Оплачено, ₸",
            ]
            assert rows[1][1] == "+77071112233"
            assert rows[1][3] == "Караганда"
            # контекст собран из крайних реплик клиента
            assert "на объект" in rows[1][5] and "30 метров" in rows[1][5]

    asyncio.run(main())


def test_export_skips_contacts_without_phone_when_required():
    async def main():
        day = datetime(2019, 9, 9, 16, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Привет"), ("assistant", "Да")], when=day, channel=Channel.WHATSAPP,
                external_id="не-номер",
            )
            only_phone = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=True
            )
            everyone = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=False
            )
            assert only_phone == []
            assert len(everyone) == 1

    asyncio.run(main())


# ─────────────────── страница и выгрузки по HTTP ───────────────────


def _login(client):
    client.cookies.clear()
    client.post("/admin/login", data={"username": "admin", "password": "admin"})


def test_exports_download_with_correct_headers(client):
    """Обе выгрузки отдаются файлом, а не страницей, и с правильным именем."""
    _login(client)

    r = client.get("/admin/reports/export/meta?date_from=2019-01-01&date_to=2019-12-31")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "meta_audience" in r.headers["content-disposition"]
    # первая строка — шапка шаблона Meta
    assert r.text.splitlines()[0].startswith("email,email,email,phone")

    r = client.get("/admin/reports/export/simple?date_from=2019-01-01&date_to=2019-12-31&enrich=0")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    # имя на кириллице уходит только в filename*, иначе заголовок не закодируется
    assert "filename*=UTF-8''" in cd
    assert cd.split('filename="')[1].split('"')[0].isascii()
    assert r.text.lstrip("﻿").splitlines()[0].startswith("Имя;Телефон;Аккаунт;Город")


def test_reports_page_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin/reports", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/admin/login"


def test_docx_export_builds_real_document():
    """Word-файл собирается и содержит метрики, находки и предложения."""
    import zipfile

    from app.services.docx_report import build_report_docx

    report = ChatReport(
        period_from=date(2019, 10, 1),
        period_to=date(2019, 10, 31),
        dialogs_analyzed=12,
        messages_analyzed=340,
        metrics={"dialogs": 12, "orders_paid": 3, "revenue_kzt": 1_200_000, "conversion_pct": 25.0},
        summary="Бот теряет клиентов на вопросе о доставке.",
        findings=[
            {
                "title": "Не называет срок доставки",
                "severity": "high",
                "evidence": "А когда привезёте?",
                "recommendation": "Добавить срок в скрипт",
            }
        ],
        proposals={
            "sales_script": "1. Поздороваться\n2. Назвать срок доставки",
            "settings": {"welcome_text": "Здравствуйте!"},
            "segments": [{"name": "Спрашивали доставку", "criteria": "упомянули доставку",
                          "user_ids": [1, 2, 3]}],
        },
    )
    report.created_at = datetime(2019, 11, 1, 10, 0, 0)

    data = build_report_docx(report, shop_name="Магазин")

    assert data[:2] == b"PK"  # docx это zip
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Аналитический отчёт по перепискам" in xml
    assert "Не называет срок доставки" in xml
    assert "Бот теряет клиентов" in xml
    assert "Спрашивали доставку" in xml


def test_whatsapp_number_taken_from_channel_id():
    """В WhatsApp идентификатор пользователя и есть номер — он должен попадать в выгрузку.

    Регрессия: раньше выгружались только те, кто продиктовал номер в разговоре,
    и из 141 переписки в WhatsApp «находилось» 11 контактов.
    """

    async def main():
        day = datetime(2019, 10, 10, 10, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Здравствуйте"), ("assistant", "Добрый день")],
                when=day,
                channel=Channel.WHATSAPP,
                external_id="77016543210",   # wa_id = номер без плюса
                phone=None,                   # клиент номер НЕ диктовал
            )
            contacts = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=True
            )
            assert len(contacts) == 1, "клиент из WhatsApp обязан попасть в выгрузку"
            assert contacts[0]["phone"] == "+77016543210"

    asyncio.run(main())


def test_instagram_and_telegram_exported_by_account():
    """У Instagram и Telegram телефона нет — контактом служит аккаунт."""

    async def main():
        day = datetime(2019, 11, 11, 11, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Сколько стоит?"), ("assistant", "340 ₸")],
                when=day, channel=Channel.INSTAGRAM, external_id="17841400000000000",
                username="ivan_stroy",
            )
            await box.dialog(
                texts=[("user", "Есть доставка?"), ("assistant", "Есть")],
                when=day, channel=Channel.TELEGRAM, external_id="194012345", username=None,
            )
            everyone = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=False
            )
            accounts = sorted(c["account"] for c in everyone)
            assert accounts == ["@ivan_stroy", "tg:194012345"]
            # без телефона в файл Meta они не идут — там нужен идентификатор
            for_meta = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=True
            )
            assert for_meta == []

    asyncio.run(main())


def test_phone_from_channel_id_ignores_non_numeric():
    """Не всякий wa_id — номер: буквенный идентификатор телефоном считать нельзя."""

    async def main():
        day = datetime(2019, 12, 12, 12, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await box.dialog(
                texts=[("user", "Привет"), ("assistant", "Да")], when=day,
                channel=Channel.WHATSAPP, external_id="abc-not-a-phone",
            )
            for_meta = await cx.collect_contacts(
                s, period_from=day.date(), period_to=day.date(), only_with_phone=True
            )
            assert for_meta == []

    asyncio.run(main())


def test_period_bounds_shifted_to_utc():
    """Даты выбирают по местному времени, а в базе UTC — окно обязано сдвигаться.

    Регрессия: без поправки отчёт «за сегодня» на UTC+5 брал 05:00 сегодня —
    04:59 завтра, и ночные переписки уезжали в соседние сутки.
    """
    day = date(2026, 3, 15)
    naive_from, naive_to = svc.period_bounds(day, day, 0)
    utc_from, utc_to = svc.period_bounds(day, day, 5)

    assert naive_from == datetime(2026, 3, 15, 0, 0, 0)
    assert utc_from == datetime(2026, 3, 14, 19, 0, 0)   # 00:00 в Павлодаре = 19:00 UTC накануне
    assert utc_to.hour == 18 and utc_to.day == 15        # 23:59:59 местного = 18:59:59 UTC
    assert (naive_to - naive_from) == (utc_to - utc_from)  # длина окна не изменилась


def test_report_shape_validation_rejects_malformed():
    """Ответ модели неверной формы не должен превращаться в пустой «успешный» отчёт."""
    good = {"summary": "Бот теряет клиентов на цене", "findings": [{"title": "x"}]}
    assert svc._looks_like_report(good)

    # списки и словари подменены строками — раньше это молча давало пустой отчёт
    assert not svc._looks_like_report({"summary": "текст", "findings": "нет проблем"})
    assert not svc._looks_like_report({"summary": "текст", "settings": "выключено"})
    assert not svc._looks_like_report({"summary": 42})
    # пустой ответ без сводки и находок бессмыслен
    assert not svc._looks_like_report({"summary": "", "findings": []})
    # отсутствующие необязательные ключи допустимы
    assert svc._looks_like_report({"summary": "есть вывод"})


def test_period_used_by_report_respects_timezone():
    """Сообщение, отправленное ночью по местному времени, попадает в СВОЙ день."""

    async def main():
        # 01:30 по Павлодару 20.06.2019 = 20:30 UTC 19.06.2019
        utc_moment = datetime(2019, 6, 19, 20, 30, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            await set_setting(s, "tz_offset_hours", 5)
            await box.dialog(
                texts=[("user", "Ночной вопрос"), ("assistant", "Ответ")], when=utc_moment
            )
            local_day = date(2019, 6, 20)
            metrics, _ = await svc.collect_metrics(
                s, *svc.period_bounds(local_day, local_day, 5)
            )
            assert metrics["dialogs"] == 1, "ночная переписка должна считаться за местные сутки"

            # без поправки она бы «потерялась» из этого дня
            metrics_naive, _ = await svc.collect_metrics(
                s, *svc.period_bounds(local_day, local_day, 0)
            )
            assert metrics_naive["dialogs"] == 0

    asyncio.run(main())


def test_tz_offset_falls_back_to_configured_timezone():
    """Без явного tz_offset_hours смещение берётся из зоны приложения (Asia/Almaty = +5),
    чтобы отчёт «за сегодня» был корректен из коробки, без ручной настройки."""
    import asyncio as _aio

    from app.db.base import AsyncSessionLocal
    from app.services import chat_report as cr
    from app.services.settings_service import set_setting

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "tz_offset_hours", None)  # явной настройки нет
            derived = await cr.business_tz_offset(s)
            await set_setting(s, "tz_offset_hours", 3)  # явная имеет приоритет
            explicit = await cr.business_tz_offset(s)
            return derived, explicit

    derived, explicit = _aio.run(main())
    assert derived == 5.0   # Asia/Almaty
    assert explicit == 3.0  # explicit override wins


def test_generate_report_maps_every_dialog_then_reduces(monkeypatch):
    """Ключевое требование владельца: модель изучает ВСЕ чаты за период, не выборку."""
    import re as _re

    seen_ids: set[int] = set()
    calls = {"map": 0, "reduce": 0}

    async def fake_llm(*, messages, tools, config, response_format=None):
        system = messages[0]["content"]
        body = messages[1]["content"]
        if system == svc.MAP_SYSTEM:
            calls["map"] += 1
            ids = [int(x) for x in _re.findall(r"\[диалог #(\d+)\]", body)]
            seen_ids.update(ids)
            items = [{"id": i, "outcome": "тёплый", "city": "Павлодар",
                      "topic": "товар", "objection": "дорого", "issue": "не дожал",
                      "quote": "подумаю"} for i in ids]
            return {"content": _json({"items": items})}
        calls["reduce"] += 1
        assert "СВОДКИ ПО ВСЕМ ДИАЛОГАМ" in body
        return {"content": _json({
            "summary": "Итоги за период",
            "findings": [{"title": "Не дожимает", "severity": "high",
                          "evidence": "подумаю", "recommendation": "добавить закрытие"}],
            "sales_script": "НОВЫЙ СКРИПТ",
            "settings": {}, "segments": [],
        })}

    monkeypatch.setattr(svc, "chat_completion", fake_llm)

    async def main():
        day = datetime(2019, 3, 15, 10, 0, 0)
        async with AsyncSessionLocal() as s, _Sandbox(s) as box:
            expected = set()
            for _ in range(3):
                u = await box.dialog(
                    texts=[("user", "Здравствуйте, сколько стоит?"),
                           ("assistant", "Расскажу с радостью"),
                           ("user", "подумаю")],
                    when=day,
                )
                expected.add(u.id)
            await set_setting(s, "llm", {"base_url": "http://x", "api_key": "k", "model": "m"})
            report, error = await svc.generate_report(
                s, period_from=day.date(), period_to=day.date()
            )
            if report:
                box.track_report(report)
            return report, error, expected

    report, error, expected = asyncio.run(main())
    assert error is None and report is not None
    assert seen_ids == expected
    assert calls["map"] >= 1 and calls["reduce"] == 1
    assert report.dialogs_analyzed == len(expected)
    assert report.summary == "Итоги за период"
    assert report.proposals["sales_script"] == "НОВЫЙ СКРИПТ"


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_parse_json_recovers_multiline_string():
    """Сырой перевод строки внутри значения — самая частая поломка длинного ответа.

    Модель пишет многострочный скрипт продаж живым текстом, а по стандарту JSON
    перевод строки внутри значения обязан быть \n. Разбор обязан это пережить,
    иначе теряется весь разбор — десятки запросов к модели.
    """
    broken = (
        '{"summary": "Вывод по периоду",\n'
        ' "sales_script": "1. Поздороваться\n2. Назвать срок доставки\n3. Взять телефон",\n'
        ' "findings": [{"title": "Нет срока доставки"}]}'
    )
    data = svc._parse_json_block(broken)
    assert data, "разбор не должен возвращать пустой результат"
    assert data["summary"] == "Вывод по периоду"
    assert "Назвать срок доставки" in data["sales_script"]
    assert data["findings"][0]["title"] == "Нет срока доставки"


def test_parse_json_drops_trailing_comma():
    data = svc._parse_json_block('{"summary": "текст", "findings": [{"title": "a"},],}')
    assert data["summary"] == "текст"
    assert len(data["findings"]) == 1


def test_parse_json_keeps_escapes_intact():
    """Починка не должна ломать уже корректные ответы."""
    ok = '{"summary": "строка с \\"кавычками\\" и \n переводом", "findings": []}'
    data = svc._parse_json_block(ok)
    assert 'кавычками' in data["summary"]
    assert "\n" in data["summary"]


def test_parse_json_returns_empty_on_hopeless_input():
    assert svc._parse_json_block("совсем не json") == {}
    assert svc._parse_json_block("") == {}


# ── Скрипты, которые предлагает модель, тоже озвучиваются ──
# «216 ₸/шт» синтезатор читает как «двести шестнадцать тенге СЛЭШ шэ-тэ»: правило
# должно доезжать до модели ДО того, как она напишет новый скрипт.


def test_report_prompt_carries_voice_rules():
    assert svc.VOICE_RULES in svc.REPORT_SYSTEM
    for token in ("₸", "/шт", "%", "м3", "сокращений", "ссылки"):
        assert token in svc.VOICE_RULES, f"в правилах озвучки нет упоминания {token!r}"
    # правило должно требовать продублировать себя в самом скрипте продаж
    assert "sales_script" in svc.VOICE_RULES


def test_voice_rules_name_every_verbatim_setting():
    """Тексты из этих настроек уходят клиенту дословно — значит, читаются вслух."""
    for key in ("welcome_text", "silence_texts", "birthday_text", "checkin_text"):
        assert key in svc.VOICE_RULES, f"дословный текст {key} не упомянут в правилах озвучки"
