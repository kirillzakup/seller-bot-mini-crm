"""Commerce core tests: the PAID gate, delivery, and idempotent fulfillment.

Channel-agnostic: delivery goes through a fake Messenger; the fake Telegram bot
is only used by the channel-invite provider to mint a link.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

from sqlalchemy import func, select

from app.db.base import AsyncSessionLocal
from app.db.models import AccessGrant, PaymentMethod, Product, User
from app.db.models.enums import (
    Channel,
    FulfillmentType,
    OrderStatus,
    PaymentProviderType,
)
from app.services import orders as svc


class FakeMessenger:
    """Records outbound messages instead of hitting a real platform."""

    def __init__(self, channel: Channel = Channel.TELEGRAM) -> None:
        self.channel = channel
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.sent.append((external_id, text))


class FakeBot:
    """Stands in for aiogram.Bot for invite-link creation."""

    async def create_chat_invite_link(self, chat_id, member_limit=1, name=None, **kw):  # noqa: ANN001
        return SimpleNamespace(invite_link=f"https://t.me/+fake_{chat_id}")


async def _make_user_product(session, ft=FulfillmentType.TEXT_LINK, cfg=None):
    n = random.randint(1, 10**9)
    user = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Тест")
    product = Product(
        sku=f"t{n}",
        title="Тест-товар",
        price_kzt=1000,
        fulfillment_type=ft,
        fulfillment_config=cfg or {"text": "Доступ открыт", "link": "https://example.com"},
    )
    session.add_all([user, product])
    await session.flush()
    return user, product


def test_fulfill_refuses_unpaid_order():
    async def main():
        async with AsyncSessionLocal() as s:
            user, product = await _make_user_product(s)
            order = await svc.create_order(s, user, product)  # AWAITING_PAYMENT
            await s.commit()
            raised = False
            try:
                await svc.fulfill(s, order, messenger=FakeMessenger(), tg_bot=FakeBot())
            except PermissionError:
                raised = True
            assert raised, "fulfill must refuse an unpaid order"

    asyncio.run(main())


def test_confirm_delivers_and_is_idempotent():
    async def main():
        msg = FakeMessenger()
        async with AsyncSessionLocal() as s:
            user, product = await _make_user_product(s)
            order = await svc.create_order(s, user, product)
            await s.commit()

            grant1 = await svc.confirm_payment(s, order, by="test", messenger=msg, tg_bot=FakeBot())
            await s.commit()

            assert order.status == OrderStatus.FULFILLED
            assert any("Доступ открыт" in text for _, text in msg.sent)

            grant2 = await svc.fulfill(s, order, messenger=msg, tg_bot=FakeBot())
            assert grant1.id == grant2.id

            count = (
                await s.execute(
                    select(func.count(AccessGrant.id)).where(AccessGrant.order_id == order.id)
                )
            ).scalar_one()
            assert count == 1

    asyncio.run(main())


def test_manual_payment_flow():
    async def main():
        msg = FakeMessenger()
        async with AsyncSessionLocal() as s:
            method = PaymentMethod(
                type=PaymentProviderType.MANUAL, title="Kaspi", instructions="реквизиты"
            )
            s.add(method)
            user, product = await _make_user_product(s)
            await s.flush()

            order = await svc.create_order(s, user, product, method)
            payment = await svc.attach_receipt(s, order, "tg_file_123")
            assert order.status == OrderStatus.PENDING_CONFIRMATION
            assert payment.receipt_file_id == "tg_file_123"

            await svc.confirm_payment(s, order, by="operator", messenger=msg, tg_bot=FakeBot())
            await s.commit()
            assert order.status == OrderStatus.FULFILLED

    asyncio.run(main())


def test_channel_invite_fulfillment_creates_link():
    async def main():
        msg = FakeMessenger()
        async with AsyncSessionLocal() as s:
            user, product = await _make_user_product(
                s,
                ft=FulfillmentType.CHANNEL_INVITE,
                cfg={"chat_id": -1001234567890, "member_limit": 1, "text": "Добро пожаловать"},
            )
            order = await svc.create_order(s, user, product)
            await s.commit()
            await svc.confirm_payment(s, order, by="test", messenger=msg, tg_bot=FakeBot())
            await s.commit()

            grant = (
                await s.execute(select(AccessGrant).where(AccessGrant.order_id == order.id))
            ).scalar_one()
            assert grant.invite_link and grant.invite_link.startswith("https://t.me/+fake_")
            assert any("Добро пожаловать" in text for _, text in msg.sent)

    asyncio.run(main())
