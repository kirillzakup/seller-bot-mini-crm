"""Background job tests: subscription expiry, abandoned-cart nudge, reconcile."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import func, select

import app.services.jobs as jobs
from app.db.base import AsyncSessionLocal
from app.db.models import AccessGrant, Event, Followup, Order, PaymentMethod, Product, User
from app.db.models.enums import (
    Channel,
    FulfillmentType,
    OrderStatus,
    PaymentProviderType,
    PaymentStatus,
)
from app.services.settings_service import set_setting


class FakeMessenger:
    def __init__(self, channel: Channel = Channel.TELEGRAM) -> None:
        self.channel = channel
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, str, str]] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.sent.append((external_id, text))

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        self.images.append((external_id, url, caption))


def _patch_messenger(monkeypatch) -> FakeMessenger:
    fake = FakeMessenger()

    async def fake_get_messenger(session, channel):  # noqa: ANN001
        return fake

    monkeypatch.setattr(jobs, "get_messenger", fake_get_messenger)
    return fake


async def _user_product(session) -> tuple[User, Product]:
    n = random.randint(1, 10**9)
    user = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="J")
    product = Product(
        sku=f"job{n}",
        title="Job-товар",
        price_kzt=3000,
        fulfillment_type=FulfillmentType.TEXT_LINK,
        fulfillment_config={"text": "доступ"},
    )
    session.add_all([user, product])
    await session.flush()
    return user, product


def test_expire_subscriptions(monkeypatch):
    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _user_product(s)
            order = Order(
                code=f"EXP{random.randint(1, 99999)}",
                user_id=user.id,
                product_id=product.id,
                amount_kzt=product.price_kzt,
                status=OrderStatus.FULFILLED,
            )
            s.add(order)
            await s.flush()
            grant = AccessGrant(
                user_id=user.id,
                product_id=product.id,
                order_id=order.id,
                type=FulfillmentType.TEXT_LINK,
                expires_at=datetime.now(UTC) - timedelta(days=1),
                revoked=False,
            )
            s.add(grant)
            await s.commit()
            gid = grant.id

        n = await jobs.expire_subscriptions()
        assert n >= 1

        async with AsyncSessionLocal() as s:
            g = await s.get(AccessGrant, gid)
            assert g.revoked is True
            count = (
                await s.execute(select(func.count(Event.id)).where(Event.type == "access_revoked"))
            ).scalar_one()
            assert count >= 1
        assert fake.sent  # user was notified

    asyncio.run(main())


def test_nudge_is_idempotent(monkeypatch):
    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _user_product(s)
            order = Order(
                code=f"NDG{random.randint(1, 99999)}",
                user_id=user.id,
                product_id=product.id,
                amount_kzt=product.price_kzt,
                status=OrderStatus.AWAITING_PAYMENT,
                # От самой константы, а не от числа: порог напоминания уже поднимали
                # (сейчас сутки), и тест не должен краснеть от такой настройки.
                created_at=datetime.now(UTC) - timedelta(hours=jobs.NUDGE_AFTER_HOURS + 1),
            )
            s.add(order)
            await s.commit()
            code = order.code

        first = await jobs.nudge_abandoned_orders()
        await jobs.nudge_abandoned_orders()  # second run must NOT re-nudge

        assert first >= 1
        # Our order was nudged exactly once across both runs (idempotency)
        assert len([t for _, t in fake.sent if code in t]) == 1

        async with AsyncSessionLocal() as s:
            events = (
                (await s.execute(select(Event).where(Event.type == "order_nudged"))).scalars().all()
            )
            assert any(e.data.get("code") == code for e in events)

    asyncio.run(main())


def test_send_due_followups(monkeypatch):
    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user, _ = await _user_product(s)
            fu = Followup(
                user_id=user.id,
                kind="think_over",
                message="Определились по нашему курсу?",
                due_at=datetime.now(UTC) - timedelta(minutes=5),
                sent=False,
            )
            s.add(fu)
            await s.commit()
            fid = fu.id

        n = await jobs.send_due_followups()
        assert n >= 1
        assert any("Определились" in t for _, t in fake.sent)

        async with AsyncSessionLocal() as s:
            f = await s.get(Followup, fid)
            assert f.sent is True and f.sent_at is not None

    asyncio.run(main())


def test_silence_reminder_sequence(monkeypatch):
    """First reminder fires; the next waits for the interval; counter increments."""
    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(
                channel=Channel.TELEGRAM,
                external_id=str(n),
                telegram_id=n,
                first_name="Silent",
                last_seen=datetime.now(UTC) - timedelta(hours=5),
                reminders_sent=0,
            )
            s.add(user)
            await set_setting(
                s,
                "followup",
                {
                    "silence_enabled": True,
                    "silence_hours": 3,
                    "silence_interval_hours": 24,
                    "silence_texts": ["ПЕРВОЕ_НАПОМИНАНИЕ", "ВТОРОЕ_НАПОМИНАНИЕ"],
                    "think_default_hours": 24,
                },
            )
            await s.commit()
            uid = user.id

        first = await jobs.nudge_silent_leads()
        await jobs.nudge_silent_leads()  # interval (24h) not passed → no 2nd reminder yet

        assert first >= 1
        assert len([t for _, t in fake.sent if "ПЕРВОЕ_НАПОМИНАНИЕ" in t]) == 1
        assert not any("ВТОРОЕ_НАПОМИНАНИЕ" in t for _, t in fake.sent)

        async with AsyncSessionLocal() as s:
            u = await s.get(User, uid)
            assert u.reminders_sent == 1

    asyncio.run(main())


def test_step_due_uses_local_send_time():
    """A reminder step fires ``day`` days later at the configured local HH:MM."""
    tz = timezone(timedelta(hours=5))  # Kazakhstan (UTC+5)
    last = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)  # 13:00 local

    # day=1 at 10:00 local → 2026-07-02 10:00 +05 = 05:00 UTC
    assert jobs._step_due(last, {"day": 1, "at": "10:00", "text": "x"}, tz) == datetime(
        2026, 7, 2, 5, 0, tzinfo=UTC
    )
    # day=0 at 10:00 but they went quiet at 13:00 → rolls to next day (never in the past)
    assert jobs._step_due(last, {"day": 0, "at": "10:00", "text": "x"}, tz) == datetime(
        2026, 7, 2, 5, 0, tzinfo=UTC
    )
    # day=0 at 20:00 → same evening, 20:00 local = 15:00 UTC
    assert jobs._step_due(last, {"day": 0, "at": "20:00", "text": "x"}, tz) == datetime(
        2026, 7, 1, 15, 0, tzinfo=UTC
    )


def test_silence_steps_sequence(monkeypatch):
    """The step sequence sends the first reminder now and defers a far-future one."""
    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(
                channel=Channel.TELEGRAM,
                external_id=str(n),
                telegram_id=n,
                first_name="Steps",
                last_seen=datetime.now(UTC) - timedelta(days=2),
                reminders_sent=0,
            )
            s.add(user)
            await set_setting(
                s,
                "followup",
                {
                    "silence_enabled": True,
                    "silence_steps": [
                        {"day": 0, "at": "00:00", "text": "STEP_ONE"},
                        {"day": 10, "at": "09:00", "text": "STEP_TWO"},
                    ],
                },
            )
            await s.commit()
            uid = user.id

        first = await jobs.nudge_silent_leads()
        await jobs.nudge_silent_leads()  # step 2 is 10 days out → not yet

        assert first >= 1
        assert len([t for _, t in fake.sent if "STEP_ONE" in t]) == 1
        assert not any("STEP_TWO" in t for _, t in fake.sent)

        async with AsyncSessionLocal() as s:
            assert (await s.get(User, uid)).reminders_sent == 1

    asyncio.run(main())


def test_silence_reminder_attaches_meme(monkeypatch):
    """A «тишина/ждун» meme in the library rides along with the silence reminder as an
    image (caption = reminder text) instead of plain text — otherwise those memes,
    tagged for silent clients, could never fire (the AI never runs on silence)."""
    from app.db.models import MediaAsset

    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(
                channel=Channel.TELEGRAM,
                external_id=str(n),
                telegram_id=n,
                first_name="Quiet",
                last_seen=datetime.now(UTC) - timedelta(hours=5),
                reminders_sent=0,
            )
            s.add(user)
            meme = MediaAsset(
                title="Ждун",
                filename="zhdun_job_test.jpg",
                tags="тишина, игнор, молчит",
                is_active=True,
            )
            s.add(meme)
            await set_setting(
                s,
                "followup",
                {
                    "silence_enabled": True,
                    "silence_hours": 3,
                    "silence_interval_hours": 24,
                    "silence_texts": ["ВЕРНИТЕСЬ_К_НАМ_ТЕСТ"],
                },
            )
            await s.commit()
            meme_id = meme.id

        await jobs.nudge_silent_leads()

        async with AsyncSessionLocal() as s2:  # keep the shared test DB clean
            m = await s2.get(MediaAsset, meme_id)
            if m is not None:
                await s2.delete(m)
                await s2.commit()

        # the reminder went out as an IMAGE (meme) with the text as caption, not plain text
        assert fake.images, "silence reminder should be sent as a meme image"
        assert any("ВЕРНИТЕСЬ_К_НАМ_ТЕСТ" in cap for _, _, cap in fake.images)
        assert not any("ВЕРНИТЕСЬ_К_НАМ_ТЕСТ" in t for _, t in fake.sent)

    asyncio.run(main())


def test_silence_reminder_uses_step_image(monkeypatch):
    """A reminder step with a pinned image_id sends THAT image (text becomes caption)."""
    from app.db.models import MediaAsset

    fake = _patch_messenger(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(
                channel=Channel.TELEGRAM,
                external_id=str(n),
                telegram_id=n,
                first_name="Pinned",
                last_seen=datetime.now(UTC) - timedelta(days=2),
                reminders_sent=0,
            )
            s.add(user)
            asset = MediaAsset(title="Шаг-картинка", filename="step_img_test.jpg", is_active=True)
            s.add(asset)
            await s.flush()
            aid = asset.id
            await set_setting(
                s,
                "followup",
                {
                    "silence_enabled": True,
                    "silence_steps": [
                        {"day": 0, "at": "00:00", "text": "ШАГ_С_КАРТИНКОЙ", "image_id": aid},
                    ],
                },
            )
            await s.commit()

        await jobs.nudge_silent_leads()

        async with AsyncSessionLocal() as s2:  # keep the shared test DB clean
            m = await s2.get(MediaAsset, aid)
            if m is not None:
                await s2.delete(m)
                await s2.commit()

        assert any(
            "step_img_test.jpg" in url and "ШАГ_С_КАРТИНКОЙ" in cap
            for _, url, cap in fake.images
        )

    asyncio.run(main())


def test_greet_birthdays(monkeypatch):
    fake = _patch_messenger(monkeypatch)

    async def main():
        today = datetime.now(timezone(timedelta(hours=5))).date()  # matches job's default tz
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(
                channel=Channel.TELEGRAM,
                external_id=str(n),
                telegram_id=n,
                first_name="Bday",
                birthday=date(1990, today.month, today.day),
                birthday_greet=True,
            )
            s.add(user)
            await set_setting(
                s,
                "loyalty",
                {"birthday_enabled": True, "birthday_text": "МАРКЕР_ДР 🎉", "checkin_text": "x"},
            )
            await s.commit()
            uid = user.id

        first = await jobs.greet_birthdays()
        await jobs.greet_birthdays()  # dedup: not twice the same year

        assert first >= 1
        assert len([t for _, t in fake.sent if "МАРКЕР_ДР" in t]) == 1

        async with AsyncSessionLocal() as s:
            events = (
                (await s.execute(select(Event).where(Event.type == "birthday_greeted")))
                .scalars()
                .all()
            )
            assert any(e.user_id == uid for e in events)

    asyncio.run(main())


def test_reconcile_confirms_paid_order(monkeypatch):
    _patch_messenger(monkeypatch)

    class FakeProvider:
        async def poll_status(self, external_id):  # noqa: ANN001
            return PaymentStatus.CONFIRMED

    monkeypatch.setattr(jobs, "build_provider", lambda method: FakeProvider())

    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _user_product(s)
            s.add(PaymentMethod(type=PaymentProviderType.YOOKASSA, title="YooKassa", enabled=True))
            order = Order(
                code=f"RC{random.randint(1, 99999)}",
                user_id=user.id,
                product_id=product.id,
                amount_kzt=product.price_kzt,
                status=OrderStatus.AWAITING_PAYMENT,
                provider_type=PaymentProviderType.YOOKASSA,
                external_id="pay_test_1",
            )
            s.add(order)
            await s.commit()
            oid = order.id

        n = await jobs.reconcile_pending_payments()
        assert n >= 1

        async with AsyncSessionLocal() as s:
            o = await s.get(Order, oid)
            assert o.status == OrderStatus.FULFILLED

    asyncio.run(main())


def test_auto_analyze_script_gated_by_enabled_and_interval(monkeypatch):
    """Auto script analysis runs only when enabled, and not again before the interval."""
    from app.db.models import ScriptProposal
    from app.services import learning
    from app.services.settings_service import get_setting

    async def main():
        # Clean slate: no leftover pending proposals from other tests.
        async with AsyncSessionLocal() as s:
            for p in await learning.pending_script_proposals(s):
                p.status = "rejected"
            await set_setting(s, "learning", {"auto_analyze_enabled": False})
            await s.commit()

        calls = {"n": 0}

        async def fake_analyze(session, *, max_dialogs=None):
            calls["n"] += 1
            p = ScriptProposal(proposed_text="улучшенный скрипт", rationale="r", based_on=4)
            session.add(p)
            await session.flush()
            return p, None

        async def fake_notify(session, text):
            return False

        monkeypatch.setattr(learning, "analyze_successful_dialogs", fake_analyze)
        monkeypatch.setattr(jobs, "send_operator_message", fake_notify)

        assert await jobs.auto_analyze_script() is False  # disabled → skip
        assert calls["n"] == 0

        async with AsyncSessionLocal() as s:
            await set_setting(s, "learning", {"auto_analyze_enabled": True, "auto_analyze_days": 7})
            await s.commit()
        assert await jobs.auto_analyze_script() is True  # enabled → runs once
        assert calls["n"] == 1
        async with AsyncSessionLocal() as s:
            assert (await get_setting(s, "learning")).get("last_auto_analyze_at")  # stamped

        assert await jobs.auto_analyze_script() is False  # immediately again → not due
        assert calls["n"] == 1

    asyncio.run(main())


# ── Инцидент 03-04.08.2026: дожимы «подумаю, напомните через неделю» уходили в
# закрытое окно WhatsApp, Meta отклоняла их (131047), а строка помечалась
# отправленной — самый тёплый лид терялся молча.


class WaMessenger(FakeMessenger):
    """WhatsApp-канал: send_text возвращает mid, есть отправка шаблона."""

    def __init__(self) -> None:
        super().__init__(Channel.WHATSAPP)
        self.templates: list[str] = []

    async def send_text(self, external_id: str, text: str):
        self.sent.append((external_id, text))
        return "wamid.TEST"

    async def send_template(self, external_id: str, *, name: str, language: str = "ru"):
        self.templates.append(name)
        return "wamid.TMPL"


def _patch_wa(monkeypatch) -> WaMessenger:
    fake = WaMessenger()

    async def fake_get_messenger(session, channel):  # noqa: ANN001
        return fake

    monkeypatch.setattr(jobs, "get_messenger", fake_get_messenger)
    return fake


async def _wa_lead(session, *, last_seen_hours_ago: float):
    """WhatsApp-клиент, чьё последнее сообщение было N часов назад."""
    from app.db.models import Message
    from app.db.models.enums import ConversationRole

    n = random.randint(1, 10**9)
    user = User(channel=Channel.WHATSAPP, external_id=str(7700000000 + n % 10**8), first_name="W")
    session.add(user)
    await session.flush()
    session.add(
        Message(
            user_id=user.id,
            role=ConversationRole.USER,
            content="подумаю",
            created_at=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(hours=last_seen_hours_ago),
        )
    )
    await session.flush()
    return user


def test_followup_waits_when_whatsapp_window_closed(monkeypatch):
    fake = _patch_wa(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _wa_lead(s, last_seen_hours_ago=72)  # окно давно закрыто
            fu = Followup(
                user_id=user.id,
                kind="think_over",
                message="Определились с выбором?",
                due_at=datetime.now(UTC) - timedelta(minutes=5),
                sent=False,
            )
            s.add(fu)
            await s.commit()
            fid = fu.id

        await jobs.send_due_followups()

        async with AsyncSessionLocal() as s:
            f = await s.get(Followup, fid)
            assert f.sent is False, "дожим не должен считаться доставленным"
            assert jobs._as_utc(f.due_at) > datetime.now(UTC), "дожим перенесён, а не потерян"
        assert not any("Определились" in t for _, t in fake.sent), "текст не должен уходить"

    asyncio.run(main())


def test_followup_sent_when_whatsapp_window_open(monkeypatch):
    fake = _patch_wa(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _wa_lead(s, last_seen_hours_ago=2)  # клиент писал недавно
            fu = Followup(
                user_id=user.id,
                kind="think_over",
                message="Определились с выбором?",
                due_at=datetime.now(UTC) - timedelta(minutes=5),
                sent=False,
            )
            s.add(fu)
            await s.commit()
            fid = fu.id

        await jobs.send_due_followups()

        async with AsyncSessionLocal() as s:
            f = await s.get(Followup, fid)
            assert f.sent is True and f.sent_at is not None
        assert any("Определились" in t for _, t in fake.sent)

    asyncio.run(main())


def test_followup_gives_up_after_ttl(monkeypatch):
    """Через две недели ожидания дожим уже неактуален — закрываем с отдельным событием."""
    _patch_wa(monkeypatch)

    async def main():
        async with AsyncSessionLocal() as s:
            user = await _wa_lead(s, last_seen_hours_ago=72)
            old = datetime.now(UTC) - timedelta(days=jobs._FOLLOWUP_TTL_DAYS + 1)
            fu = Followup(
                user_id=user.id,
                kind="think_over",
                message="Определились?",
                due_at=datetime.now(UTC) - timedelta(minutes=5),
                sent=False,
                created_at=old,
            )
            s.add(fu)
            await s.commit()
            fid, uid = fu.id, user.id

        await jobs.send_due_followups()

        async with AsyncSessionLocal() as s:
            f = await s.get(Followup, fid)
            assert f.sent is True, "просроченный дожим должен закрываться, а не висеть вечно"
            events = (
                await s.execute(
                    select(Event).where(Event.type == "followup_expired", Event.user_id == uid)
                )
            ).scalars().all()
            assert events, "закрытие по сроку должно попадать в события"

    asyncio.run(main())
