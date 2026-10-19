"""Meta click-to-message ads: capture which ad brought the lead (attribution)."""

from __future__ import annotations

import asyncio

from app.channels import dispatch
from app.channels.base import IncomingMessage
from app.db.models.enums import Channel


def test_capture_ad_referral_first_touch_wins(client):
    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.services.notify import client_block

    ref1 = {
        "source_id": "120210",
        "source_type": "ad",
        "headline": "Новинки со скидкой",
        "ctwa_clid": "abc123",
    }
    ref2 = {"source_id": "999", "headline": "Другое объявление"}

    async def run():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.WHATSAPP, external_id="77010000001")
            s.add(u)
            await s.flush()
            m1 = IncomingMessage(
                channel=Channel.WHATSAPP, external_id="77010000001",
                text="Здравствуйте", raw={"referral": ref1},
            )
            await dispatch._capture_ad_referral(s, u, m1)
            first = (u.ad_ref, u.source)
            m2 = IncomingMessage(
                channel=Channel.WHATSAPP, external_id="77010000001",
                text="ещё", raw={"referral": ref2},
            )
            await dispatch._capture_ad_referral(s, u, m2)
            await s.commit()
            return first, (u.ad_ref, u.source), client_block(u)

    first, second, block = asyncio.run(run())
    assert "Новинки со скидкой" in first[0]
    assert "ad 120210" in first[0]
    assert first[1] == "ad:120210"
    assert second == first  # a later ad must not steal the credit
    assert "Из рекламы" in block  # operator sees the source in every escalation


def test_capture_ad_referral_ignores_plain_message(client):
    from app.db.base import AsyncSessionLocal
    from app.db.models import User

    async def run():
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.WHATSAPP, external_id="77010000002")
            s.add(u)
            await s.flush()
            m = IncomingMessage(
                channel=Channel.WHATSAPP, external_id="77010000002",
                text="просто вопрос", raw={"id": "wamid.x"},
            )
            await dispatch._capture_ad_referral(s, u, m)
            await s.commit()
            return u.ad_ref

    assert asyncio.run(run()) is None
