"""Статус «🕓 В ожидании»: менеджер откладывает клиента на дату, бот возвращает карточку.

Схема уже проверена в работе.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.admin.routes.leads import _seller_rows
from app.db.base import AsyncSessionLocal
from app.db.models import Lead, User
from app.db.models.enums import Channel, LeadStatus
from app.services import leads_service as leads


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Обогащение карточки моделью не должно ходить в сеть: другие тесты оставляют в БД
    настройку llm с фейковым адресом."""

    async def fake_cc(**_kw):
        return {"content": ""}

    monkeypatch.setattr(leads, "chat_completion", fake_cc)


def _make_user(session, **kw) -> User:
    ext = kw.pop("ext", None) or f"7701{random.randint(10**6, 10**7 - 1)}"
    user = User(channel=kw.pop("channel", Channel.WHATSAPP), external_id=ext, **kw)
    session.add(user)
    return user


def _product(session, price=45_000):
    from app.db.models import Product
    from app.db.models.enums import FulfillmentType

    n = random.randint(1, 10**9)
    product = Product(
        sku=f"lead{n}",
        title="Python для детей",
        price_kzt=price,
        fulfillment_type=FulfillmentType.TEXT_LINK,
        fulfillment_config={"text": "Доступ открыт", "link": "https://example.com"},
    )
    session.add(product)
    return product


class _TgUser:
    """Минимум от aiogram-пользователя, который нужен реестру менеджеров."""

    def __init__(self, uid, full_name="", username=None):
        self.id = uid
        self.full_name = full_name
        self.username = username
        self.is_bot = False


_SALES_CHAT = -100500


class _FakeMsg:
    def __init__(self, chat_id=_SALES_CHAT, message_id=10, text="", reply_to=None,
                 from_user=None, chat_type="supergroup"):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.reply_to_message = reply_to
        self.from_user = from_user
        self.message_thread_id = None
        self.is_topic_message = None
        self.replies: list[str] = []
        self.answers: list[str] = []
        self.markup = None

    async def reply(self, text, **_kw):
        self.replies.append(text)

    async def answer(self, text, **_kw):
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, data, message, from_user):
        self.data = data
        self.message = message
        self.from_user = from_user
        self.alerts: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def _reply_to(question: str, text: str, who) -> _FakeMsg:
    parent = SimpleNamespace(from_user=SimpleNamespace(is_bot=True), text=question, caption=None)
    return _FakeMsg(message_id=99, text=text, reply_to=parent, from_user=who)


async def _new_lead(**kw) -> int:
    async with AsyncSessionLocal() as session:
        user = _make_user(session, first_name="Клиент")
        await session.flush()
        lead = Lead(
            user_id=user.id,
            status=kw.pop("status", LeadStatus.NEW),
            card_chat_id=_SALES_CHAT,
            card_message_id=10,
            **kw,
        )
        session.add(lead)
        await session.commit()
        return lead.id


async def _get(lead_id: int) -> Lead:
    async with AsyncSessionLocal() as session:
        return await session.get(Lead, lead_id)


def _login(client):
    client.cookies.clear()
    client.post("/admin/login", data={"username": "admin", "password": "admin"})


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_work_button_turns_into_waiting_on_the_card():
    """После «⏳ В работе» на карточке на её месте «🕓 В ожидании»; в сводке — нет."""
    lead = Lead(user_id=1, status=LeadStatus.IN_WORK)
    lead.id = 7
    assert _buttons(leads.card_kb(lead))[0] == "⏳ В работе", "сразу после «Забрал»"

    lead.work_started = True
    kb = leads.card_kb(lead)
    assert _buttons(kb) == ["🕓 В ожидании", "💰 Продажа", "❌ Отказ", "🚫 Не целевой"]
    assert kb.inline_keyboard[0][0].callback_data == "lead:wait:7"
    assert _buttons(leads.card_kb(lead, for_digest=True))[0] == "⏳ В работе", (
        "в утренней сводке «В работе» спрашивает, что сделано, — её не убираем"
    )

    parked = Lead(user_id=1, status=LeadStatus.WAITING)
    parked.id = 8
    assert _buttons(leads.card_kb(parked)) == [
        "🕓 В ожидании",
        "💰 Продажа",
        "❌ Отказ",
        "🚫 Не целевой",
    ], "в ожидании можно отложить ещё раз или закрыть раньше срока"


def test_parse_remind_at_understands_how_managers_write_dates():
    from datetime import timezone

    from app.bot.handlers.leads import parse_remind_at

    tz = timezone(timedelta(hours=5))
    now = datetime(2026, 10, 8, 11, 30, tzinfo=tz)  # четверг, 11:30

    def at(text):
        when = parse_remind_at(text, now)
        return None if when is None else when.strftime("%d.%m.%Y %H:%M")

    assert at("15.10") == "15.10.2026 10:00", "без времени — 10:00"
    assert at("15.10 14:00") == "15.10.2026 14:00"
    assert at("в 14:00 15.10") == "15.10.2026 14:00", "время в любом месте фразы"
    assert at("15.10.2026") == "15.10.2026 10:00"
    assert at("15 октября") == "15.10.2026 10:00"
    assert at("1 мая") == "01.05.2027 10:00", "месяц уже прошёл — следующий год"
    assert at("10.01") == "10.01.2027 10:00"
    assert at("Завтра") == "09.10.2026 10:00"
    assert at("клиент просил завтра в 9:30") == "09.10.2026 09:30"
    assert at("послезавтра") == "10.10.2026 10:00"
    assert at("через 3 дня") == "11.10.2026 10:00"
    assert at("через 1 день") == "09.10.2026 10:00"
    assert at("через два дня") == "10.10.2026 10:00"
    assert at("через неделю") == "15.10.2026 10:00"
    assert at("через 2 недели") == "22.10.2026 10:00"
    assert at("через месяц") == "08.11.2026 10:00"
    assert at("в понедельник") == "12.10.2026 10:00"
    assert at("в субботу 12:00") == "10.10.2026 12:00"
    assert at("в четверг") == "15.10.2026 10:00", "сегодня четверг — значит, следующий"
    assert at("сегодня 18:00") == "08.10.2026 18:00"
    assert at("18:00") == "08.10.2026 18:00", "только время — сегодня"
    assert parse_remind_at("завтра", now).tzinfo is tz, "в рабочем поясе"

    # Прошедшее — переспросить, а не напомнить «вчера».
    assert at("сегодня 09:00") is None
    assert at("08.10") is None, "сегодня в 10:00 уже прошло"
    assert at("07.10.2026") is None
    # Мусор и несуществующие даты.
    assert at("позже") is None
    assert at("") is None
    assert at("31.02") is None
    assert at("завтра 25:00") is None


def test_waiting_button_asks_the_date_and_parks_the_lead(client, monkeypatch):
    """«В работе» → кнопка «В ожидании» → дата → заявка ждёт, карточка это показывает."""
    from app.bot.handlers import leads as h
    from app.services.settings_service import get_local_tz

    async def sales_chats(_session):
        return {_SALES_CHAT}

    edits: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kw):
            edits.append(kw)

    monkeypatch.setattr(h, "_sales_chat_ids", sales_chats)
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: FakeBot())
    seller = _TgUser(4250, full_name="Асем")

    async def run():
        lead_id = await _new_lead(status=LeadStatus.IN_WORK, taken_by_name="Асем")

        on_card = _FakeCallback(f"lead:work:{lead_id}", _FakeMsg(message_id=10), seller)
        await h.still_working(on_card)
        assert (await _get(lead_id)).work_started is True
        assert _buttons(edits[-1]["reply_markup"])[0] == "🕓 В ожидании", "кнопка сменилась"

        stranger = _FakeCallback(f"lead:wait:{lead_id}", _FakeMsg(chat_id=-1), seller)
        await h.waiting(stranger)
        assert stranger.alerts == ["Заявка не найдена"], "только из чата продаж"

        cb = _FakeCallback(f"lead:wait:{lead_id}", _FakeMsg(), seller)
        await h.waiting(cb)
        question = cb.message.answers[0]
        assert f"⟦заявка #{lead_id} ожидание⟧" in question
        assert "о чём договорились" in question and "через неделю" in question
        assert (await _get(lead_id)).status == LeadStatus.IN_WORK, "без даты статус не меняем"

        bad = _reply_to(question, "когда-нибудь потом", seller)
        assert h._is_ask_reply(bad)
        await h.answer_to_ask(bad)
        assert "Не понял дату" in bad.replies[0]
        assert f"⟦заявка #{lead_id} ожидание⟧" in bad.replies[0], "можно ответить на переспрос"
        lead = await _get(lead_id)
        assert lead.status == LeadStatus.IN_WORK and lead.remind_at is None

        good = _reply_to(question, "завтра 14:00", seller)
        await h.answer_to_ask(good)
        async with AsyncSessionLocal() as session:
            tz = await get_local_tz(session)
        expected = (datetime.now(tz) + timedelta(days=1)).strftime("%d.%m") + " 14:00"
        lead = await _get(lead_id)
        assert lead.status == LeadStatus.WAITING and lead.remind_at is not None
        assert leads.remind_label(lead.remind_at, tz) == expected
        assert f"ожидание до {expected}" in lead.work_note
        assert good.replies[0].startswith(f"🕓 Напомню о заявке #{lead_id} ")
        assert "в 14:00" in good.replies[0]
        card = edits[-1]
        assert f"🕓 В ожидании до {expected}" in card["text"]
        assert _buttons(card["reply_markup"]) == [
            "🕓 В ожидании",
            "💰 Продажа",
            "❌ Отказ",
            "🚫 Не целевой",
        ]

        # Незабранную отложили сразу — заявка всё равно на том, кто отложил.
        fresh_id = await _new_lead()
        cb = _FakeCallback(f"lead:wait:{fresh_id}", _FakeMsg(), seller)
        await h.waiting(cb)
        await h.answer_to_ask(_reply_to(cb.message.answers[0], "через 3 дня", seller))
        fresh = await _get(fresh_id)
        assert fresh.status == LeadStatus.WAITING and fresh.taken_by_name == "Асем"

        # Закрыли раньше срока — напоминание снимается.
        cb = _FakeCallback(f"lead:reject:{fresh_id}", _FakeMsg(), seller)
        await h.reject(cb)
        await h.answer_to_ask(_reply_to(cb.message.answers[0], "передумали", seller))
        fresh = await _get(fresh_id)
        assert fresh.status == LeadStatus.REJECT and fresh.remind_at is None

    asyncio.run(run())


def test_card_shows_the_client_order_number(client):
    """Номер заказа клиента — прямо в карточке, с суммой и статусом."""
    from app.services import orders as orders_svc

    async def run():
        async with AsyncSessionLocal() as session:
            user = _make_user(session, first_name="Дана", channel=Channel.TELEGRAM)
            product = _product(session, price=50_000)
            await session.flush()
            lead = Lead(user_id=user.id, status=LeadStatus.IN_WORK)
            session.add(lead)
            await session.flush()
            assert "🧾" not in await leads.render_card(session, lead, user), "заказа ещё нет"

            order = await orders_svc.create_order(session, user, product)
            text = await leads.render_card(session, lead, user)
            assert f"🧾 Заказ: <code>{order.code}</code> · 50 000 ₸ · ждёт оплату" in text
            assert "🧾" not in leads.card_text(lead, user), "card_text без заказа — как раньше"
            await session.rollback()

    asyncio.run(run())


def test_due_waiting_lead_comes_back_as_a_fresh_card(client, monkeypatch):
    """В названный день — новая карточка в теме заявок, с тем же номером и кнопками."""
    from app.services import jobs, leads_reminders
    from app.services import orders as orders_svc

    sent: list[dict] = []

    class FakeBot:
        async def send_message(
            self, chat_id, text, reply_markup=None, message_thread_id=None, **_kw
        ):
            sent.append({"text": text, "kb": reply_markup, "thread": message_thread_id})
            return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=555)

    async def targets(session):
        return [(-100123, 7)]

    bot: dict = {"bot": None}
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: bot["bot"])
    monkeypatch.setattr(leads, "leads_targets", targets)
    monkeypatch.setattr(leads_reminders, "_SEND_PAUSE_S", 0)

    def mine():
        return [m for m in sent if f"Заявка #{due_id}</b>" in m["text"]]

    async def run():
        nonlocal due_id
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            user = _make_user(session, first_name="Ержан", channel=Channel.TELEGRAM)
            product = _product(session, price=50_000)
            await session.flush()
            order = await orders_svc.create_order(session, user, product)
            due = Lead(
                user_id=user.id,
                status=LeadStatus.WAITING,
                taken_by_name="Асем",
                work_started=True,
                remind_at=now - timedelta(minutes=1),
                card_chat_id=_SALES_CHAT,
                card_message_id=10,
            )
            later = Lead(
                user_id=user.id,
                status=LeadStatus.WAITING,
                taken_by_name="Асем",
                work_started=True,
                remind_at=now + timedelta(days=2),
            )
            session.add_all([due, later])
            await session.commit()
            code, due_id, later_id = order.code, due.id, later.id

        # Бот ещё не запущен — ничего не трогаем, следующий запуск попробует снова.
        assert await jobs.remind_waiting_leads() == 0
        assert (await _get(due_id)).status == LeadStatus.WAITING

        bot["bot"] = FakeBot()
        assert await jobs.remind_waiting_leads() >= 1
        assert len(mine()) == 1, "одна свежая карточка"
        card = mine()[0]
        assert card["text"].startswith("🔔") and "клиент просил связаться сегодня" in card["text"]
        assert code in card["text"], "номер заказа в напоминании"
        assert card["thread"] == 7, "в тему заявок"
        assert _buttons(card["kb"]) == ["🕓 В ожидании", "💰 Продажа", "❌ Отказ", "🚫 Не целевой"]
        assert not any(f"Заявка #{later_id}</b>" in m["text"] for m in sent), "срок не настал"

        lead = await _get(due_id)
        assert lead.status == LeadStatus.IN_WORK and lead.work_started is True
        assert lead.remind_at is None
        assert (lead.card_chat_id, lead.card_message_id) == (-100123, 555), (
            "дальше нажатия правят новую карточку"
        )
        assert (await _get(later_id)).status == LeadStatus.WAITING

        await jobs.remind_waiting_leads()
        assert len(mine()) == 1, "второй раз та же заявка не приходит"

    due_id = 0
    asyncio.run(run())


def test_waiting_lead_stays_open_but_skips_the_morning_digest(client, monkeypatch):
    """Отложенная заявка открыта (новая передача — в неё же), но в сводку не попадает."""
    from app.services import leads_reminders

    sent: list[str] = []

    class FakeBot:
        async def send_message(
            self, chat_id, text, reply_markup=None, message_thread_id=None, **_kw
        ):
            sent.append(text)

    async def targets(session):
        return [(-100123, 7)]

    async def fake_post(session, lead, user):
        raise AssertionError("вторая карточка по отложенному клиенту")

    monkeypatch.setattr(leads, "leads_targets", targets)
    monkeypatch.setattr(leads, "post_card", fake_post)
    monkeypatch.setattr(leads_reminders, "_SEND_PAUSE_S", 0)
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: None)

    async def run():
        async with AsyncSessionLocal() as session:
            user = _make_user(session, first_name="Айдос")
            await session.flush()
            parked = Lead(
                user_id=user.id,
                status=LeadStatus.WAITING,
                taken_by_name="Асем",
                remind_at=datetime.now(UTC) + timedelta(days=3),
                card_chat_id=_SALES_CHAT,
                card_message_id=10,
            )
            session.add(parked)
            await session.flush()

            assert (await leads.open_lead(session, user)).id == parked.id
            again = await leads.record_handoff(session, user, reason="Снова пишет")
            assert again.id == parked.id, "та же заявка, без дубля"

            assert parked.id not in {lead.id for lead in await leads_reminders._open_leads(session)}
            await leads.remind_leads_in_work(session, bot=FakeBot())
            assert not any(f"#{parked.id}" in text for text in sent)
            await session.rollback()

    asyncio.run(run())


def test_waiting_status_is_known_to_the_admin_and_export(client):
    """Новый статус не роняет страницу и выгрузку, а в сводке считается «в работе»."""
    from app.services import leads_export

    lead = Lead(user_id=1, status=LeadStatus.WAITING, taken_by_name="Асем")
    lead.id = 5
    assert leads_export.STATUS_LABEL[LeadStatus.WAITING] == "В ожидании"
    assert "🕓 В ожидании" in leads.card_text(lead, None)
    assert _seller_rows([lead])[0]["taken"] == 1

    async def seed():
        async with AsyncSessionLocal() as session:
            user = _make_user(session, first_name="Ожидающий")
            await session.flush()
            session.add(
                Lead(
                    user_id=user.id,
                    status=LeadStatus.WAITING,
                    remind_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await session.commit()

    asyncio.run(seed())
    _login(client)
    r = client.get("/admin/leads?status=waiting")
    assert r.status_code == 200 and "В ожидании" in r.text and "напомнить" in r.text
    r = client.get("/admin/leads/export.xlsx?status=waiting")
    assert r.status_code == 200 and r.content[:2] == b"PK"


@pytest.mark.parametrize(
    ("reply", "agreement"),
    [
        ("20.10 14:00 перезвонить, ждёт расчёт доставки", "перезвонить, ждёт расчёт доставки"),
        ("завтра в 14:00 созвонимся по цвету", "созвонимся по цвету"),
        ("перезвонить в пятницу", "перезвонить"),
        ("через неделю, жена посмотрит фото", "жена посмотрит фото"),
        ("15 октября приедет в офис", "приедет в офис"),
        ("В понедельник отправить счёт", "отправить счёт"),
        ("завтра 14:00", ""),
    ],
)
def test_agreement_is_the_reply_without_the_date(reply, agreement):
    from app.bot.handlers.leads import agreement_text

    assert agreement_text(reply) == agreement


def test_waiting_records_next_contact_and_what_was_agreed(client, monkeypatch):
    """«В ожидании» → срок и договорённость одним сообщением; без договорённости бот
    её дозапрашивает. Договорённость видна в карточке и в истории работы."""
    from app.bot.handlers import leads as h

    async def sales_chats(_session):
        return {_SALES_CHAT}

    edits: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kw):
            edits.append(kw)

    monkeypatch.setattr(h, "_sales_chat_ids", sales_chats)
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: FakeBot())
    seller = _TgUser(4251, full_name="Ксения")

    async def run():
        lead_id = await _new_lead(status=LeadStatus.IN_WORK, taken_by_name="Ксения")
        cb = _FakeCallback(f"lead:wait:{lead_id}", _FakeMsg(), seller)
        await h.waiting(cb)
        question = cb.message.answers[0]

        both = _reply_to(question, "через 3 дня 14:00 перезвонить, ждёт расчёт доставки", seller)
        await h.answer_to_ask(both)
        lead = await _get(lead_id)
        assert lead.status == LeadStatus.WAITING
        assert lead.wait_note == "перезвонить, ждёт расчёт доставки"
        assert "договорились: перезвонить, ждёт расчёт доставки" in lead.work_note
        assert "🤝 Договорились: перезвонить, ждёт расчёт доставки" in both.replies[0]
        assert "🤝 Договорились: перезвонить, ждёт расчёт доставки" in edits[-1]["text"]

        # Только дата — заявку откладываем и отдельно спрашиваем договорённость.
        other_id = await _new_lead(status=LeadStatus.IN_WORK, taken_by_name="Ксения")
        cb = _FakeCallback(f"lead:wait:{other_id}", _FakeMsg(), seller)
        await h.waiting(cb)
        only_date = _reply_to(cb.message.answers[0], "завтра 14:00", seller)
        await h.answer_to_ask(only_date)
        assert (await _get(other_id)).status == LeadStatus.WAITING
        ask = only_date.replies[1]
        assert "О чём договорились" in ask and f"⟦заявка #{other_id} договор⟧" in ask
        answer = _reply_to(ask, "пришлёт размеры дома", seller)
        assert h._is_ask_reply(answer)
        await h.answer_to_ask(answer)
        other = await _get(other_id)
        assert other.wait_note == "пришлёт размеры дома"
        assert "🤝 Записал договорённость" in answer.replies[0]
        assert "🤝 Договорились: пришлёт размеры дома" in edits[-1]["text"]

    asyncio.run(run())


def test_only_the_owner_can_press_or_answer_on_a_taken_lead(client, monkeypatch):
    """Евгений жмёт на заявку Ксении — бот отвечает «не ваша» и ничего не меняет."""
    from app.bot.handlers import leads as h

    async def sales_chats(_session):
        return {_SALES_CHAT}

    class FakeBot:
        async def edit_message_text(self, **kw):
            pass

    monkeypatch.setattr(h, "_sales_chat_ids", sales_chats)
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: FakeBot())
    ksenia, evgeny = _TgUser(430138994, "Ксения"), _TgUser(365932857, "Yevgeniy")

    async def run():
        lead_id = await _new_lead(
            status=LeadStatus.IN_WORK, taken_by_name="Ксения", taken_by_tg_id=ksenia.id
        )
        for action, handler in (
            ("work", h.still_working),
            ("wait", h.waiting),
            ("sale", h.sale),
            ("reject", h.reject),
            ("nt", h.not_target),
            ("back", h.back_to_work),
        ):
            cb = _FakeCallback(f"lead:{action}:{lead_id}", _FakeMsg(), evgeny)
            await handler(cb)
            assert cb.alerts == ["⛔ Это не ваша заявка — её ведёт Ксения."], action
            assert cb.message.answers == [], f"{action}: вопрос чужому не задаём"
        lead = await _get(lead_id)
        assert lead.status == LeadStatus.IN_WORK and not lead.work_started

        # Переслал себе вопрос Ксении и ответил — тоже нет.
        question = f"Ксения, напишите сумму продаж\n⟦заявка #{lead_id} сумма⟧"
        sneaky = _reply_to(question, "500000", evgeny)
        await h.answer_to_ask(sneaky)
        assert "не ваша заявка" in sneaky.replies[0]
        assert (await _get(lead_id)).status == LeadStatus.IN_WORK

        # Сама Ксения — пожалуйста.
        own = _FakeCallback(f"lead:work:{lead_id}", _FakeMsg(), ksenia)
        await h.still_working(own)
        assert own.alerts == ["Отмечено: в работе"]

        # Незабранную может взять любой.
        free_id = await _new_lead()
        cb = _FakeCallback(f"lead:take:{free_id}", _FakeMsg(), evgeny)
        await h.take(cb)
        assert (await _get(free_id)).taken_by_tg_id == evgeny.id

    asyncio.run(run())


def test_work_button_on_a_reminder_turns_into_waiting_there_too(client, monkeypatch):
    """На напоминании из сводки «⏳ В работе» после нажатия тоже меняется на «🕓 В ожидании»."""
    from app.bot.handlers import leads as h

    async def sales_chats(_session):
        return {_SALES_CHAT}

    class FakeBot:
        async def edit_message_text(self, **kw):
            pass

    monkeypatch.setattr(h, "_sales_chat_ids", sales_chats)
    monkeypatch.setattr("app.bot.runner.get_bot", lambda: FakeBot())
    seller = _TgUser(4252, full_name="Ксения")

    async def run():
        lead_id = await _new_lead(status=LeadStatus.IN_WORK, taken_by_name="Ксения")
        reminder = _FakeMsg(message_id=555)  # не карточка, а напоминание из сводки
        await h.still_working(_FakeCallback(f"lead:work:{lead_id}", reminder, seller))
        assert reminder.markup is not None
        assert _buttons(reminder.markup)[0] == "🕓 В ожидании"
        assert "что уже сделано" in reminder.answers[0]

    asyncio.run(run())
