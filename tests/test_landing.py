"""Landing page + creatives admin tests."""

from __future__ import annotations

import asyncio
import random

from app.db.base import AsyncSessionLocal
from app.db.models import Creative
from app.services.settings_service import set_setting


def _add_creative(code: str, *, headline: str = "Скидка на первый заказ") -> None:
    async def setup():
        async with AsyncSessionLocal() as s:
            s.add(
                Creative(
                    code=code,
                    title="Тест-креатив",
                    headline=headline,
                    subtext="Приходи учиться",
                    is_active=True,
                )
            )
            await set_setting(s, "bot_username", "ShopSellerBot")
            await s.commit()

    asyncio.run(setup())


def test_landing_renders_with_deeplink(client):
    code = f"cr{random.randint(1, 10**9)}"
    _add_creative(code)
    r = client.get(f"/l/{code}")
    assert r.status_code == 200
    assert "Скидка на первый заказ" in r.text
    assert f"t.me/ShopSellerBot?start={code}" in r.text


def test_landing_unknown_is_404(client):
    assert client.get("/l/does-not-exist-zzz").status_code == 404


def test_creatives_admin_list_renders(client):
    client.cookies.clear()
    client.post("/admin/login", data={"username": "admin", "password": "admin"})
    code = f"adm{random.randint(1, 10**9)}"

    async def setup():
        async with AsyncSessionLocal() as s:
            s.add(Creative(code=code, title="Реклама IG", is_active=True))
            await s.commit()

    asyncio.run(setup())
    r = client.get("/admin/creatives")
    assert r.status_code == 200
    assert code in r.text  # the stats join query ran without error
