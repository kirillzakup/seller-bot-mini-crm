"""Phase 0 smoke tests: app boots, admin auth works, dashboard renders."""

from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_admin_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_login_page_renders(client):
    client.cookies.clear()
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "Войти" in r.text


def test_login_and_dashboard(client):
    client.cookies.clear()
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Session cookie now set on the client → dashboard is reachable
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Дашборд" in r.text
    # Seed created a sample product and manual payment method
    assert "Товары" in r.text


def test_payment_page(client):
    import asyncio
    import random

    from app.db.base import AsyncSessionLocal
    from app.db.models import Product, User
    from app.db.models.enums import Channel, FulfillmentType
    from app.services import orders as orders_svc

    async def setup() -> str:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            user = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="P")
            product = Product(
                sku=f"pg{n}",
                title="Страничный продукт",
                price_kzt=7000,
                fulfillment_type=FulfillmentType.TEXT_LINK,
                fulfillment_config={"text": "x"},
            )
            s.add_all([user, product])
            await s.flush()
            order = await orders_svc.create_order(s, user, product)
            await s.commit()
            return order.code

    code = asyncio.run(setup())

    r = client.get(f"/pay/{code}")
    assert r.status_code == 200
    assert "Страничный продукт" in r.text
    assert "7 000" in r.text  # amount formatted in tenge

    assert client.get("/pay/NOPE_NOPE_NOPE").status_code == 404


def test_bad_login_rejected(client):
    client.cookies.clear()
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def _login(client):
    client.cookies.clear()
    client.post("/admin/login", data={"username": "admin", "password": "admin"})


def test_admin_pages_render(client):
    _login(client)
    for path in [
        "/admin",
        "/admin/setup",
        "/admin/products",
        "/admin/payment-methods",
        "/admin/orders",
        "/admin/dialogs",
        "/admin/clients",
        "/admin/kb",
        "/admin/learning",
        "/admin/reports",
        "/admin/media",
        "/admin/documents",
        "/admin/creatives",
        "/admin/channels",
        "/admin/channels/whatsapp/connect",
        "/admin/settings",
        "/admin/help",
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_media_upload_and_delete(client):
    """Uploading an image creates an asset; deleting it removes the row + file."""
    import asyncio

    from app.db.base import AsyncSessionLocal

    _login(client)
    png = bytes.fromhex("89504e470d0a1a0a")
    r = client.post(
        "/admin/media/upload",
        data={"title": "smoke мем", "tags": "smoke"},
        files={"file": ("s.png", png, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def _find():
        async with AsyncSessionLocal() as s:
            from app.services.media import list_media

            return [i for i in await list_media(s) if i.title == "smoke мем"]

    items = asyncio.run(_find())
    assert items
    r2 = client.post(f"/admin/media/{items[0].id}/delete", follow_redirects=False)
    assert r2.status_code == 303


def test_operator_send_voice_file(client):
    """Operator sends a recorded voice note (as the admin mic recorder does) — must
    not crash, and the message is saved with a playable media_url/media_kind."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel

    _login(client)

    async def _mkuser() -> int:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Г")
            s.add(u)
            await s.commit()
            return u.id

    uid = asyncio.run(_mkuser())

    r = client.post(
        f"/admin/dialogs/{uid}/send",
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert r.status_code == 200

    async def _msg():
        async with AsyncSessionLocal() as s:
            return (
                (await s.execute(select(Message).where(Message.user_id == uid))).scalars().first()
            )

    msg = asyncio.run(_msg())
    assert msg is not None
    assert msg.media_kind == "voice"
    assert msg.media_url


def test_operator_send_revoices_when_active_and_toggle(client):
    """Operator reply while the bot is ACTIVE → the bot re-voices it (saved as 'assistant'),
    keeps the admin's exact wording as an 'operator' note, AND takes over (pauses) so it
    never ALSO auto-answers the same client message. While paused a further reply goes raw."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel

    _login(client)

    async def _mkuser() -> int:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Д")
            s.add(u)
            await s.commit()
            return u.id

    uid = asyncio.run(_mkuser())

    async def _state():
        async with AsyncSessionLocal() as s:
            u = await s.get(User, uid)
            msgs = (
                (await s.execute(select(Message).where(Message.user_id == uid))).scalars().all()
            )
            return u.bot_paused, [(m.role.value, m.content) for m in msgs]

    # Bot active → operator reply is re-voiced by the bot AND the bot takes over (pauses),
    # so it never ALSO auto-answers the same client message (the double-reply bug).
    r = client.post(f"/admin/dialogs/{uid}/send", data={"text": "Привет от оператора"})
    assert r.status_code == 200
    paused, msgs = asyncio.run(_state())
    assert paused is True  # operator answered → takeover, bot stops double-replying
    assert ("operator", "Привет от оператора") in msgs  # admin's exact wording kept as a note
    assert any(role == "assistant" for role, _ in msgs)  # the bot's re-voiced reply

    # Now paused → a further operator reply goes RAW (verbatim), no re-voice.
    client.post(f"/admin/dialogs/{uid}/send", data={"text": "жёстко как есть"})
    assert ("operator", "жёстко как есть") in asyncio.run(_state())[1]

    # «Включить бота» resumes autoreplies (toggle bot_paused back off).
    assert client.post(f"/admin/dialogs/{uid}/bot", follow_redirects=False).status_code == 303
    assert asyncio.run(_state())[0] is False


def test_operator_send_undelivered_shows_warning_and_failed_status(client):
    """No Telegram bot is configured in tests → the relay genuinely can't deliver.
    The operator must see a warning banner (never a silent "sent"), and the saved
    turn's status must be 'failed' so it isn't later repainted as read/delivered."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel

    _login(client)

    async def _mkuser() -> int:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Н")
            s.add(u)
            await s.commit()
            return u.id

    uid = asyncio.run(_mkuser())
    # Pause the bot first so the message goes raw (not re-voiced) when send fails
    client.post(f"/admin/dialogs/{uid}/bot")
    r = client.post(f"/admin/dialogs/{uid}/send", data={"text": "не дойдёт"})
    assert r.status_code == 200
    assert "не удалось доставить" in r.text  # the transient error banner

    async def _status() -> str | None:
        async with AsyncSessionLocal() as s:
            m = (
                (await s.execute(select(Message).where(Message.user_id == uid)))
                .scalars()
                .first()
            )
            return m.status if m else None

    assert asyncio.run(_status()) == "failed"


def test_delete_dialog_message(client):
    """Deleting a message drops it from the thread partial + the DB (the bot's memory);
    a mismatched user_id in the path is a no-op (ownership guard)."""
    import asyncio
    import random

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel, ConversationRole

    _login(client)

    async def _seed() -> tuple[int, int]:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Del")
            s.add(u)
            await s.flush()
            m = Message(user_id=u.id, role=ConversationRole.USER, content="сотри меня")
            s.add(m)
            await s.commit()
            return u.id, m.id

    uid, mid = asyncio.run(_seed())

    async def _get_msg():
        async with AsyncSessionLocal() as s:
            return await s.get(Message, mid)

    # Ownership guard: deleting via a different user's path must NOT remove it.
    client.post(f"/admin/dialogs/{uid + 987654}/message/{mid}/delete")
    assert asyncio.run(_get_msg()) is not None

    # Real delete removes it from the returned thread partial and the DB.
    r = client.post(f"/admin/dialogs/{uid}/message/{mid}/delete")
    assert r.status_code == 200
    assert "сотри меня" not in r.text
    assert asyncio.run(_get_msg()) is None


def test_pin_and_mark_unread(client):
    """Pin toggles Message.pinned; mark-unread sets User.unread; opening the chat clears it."""
    import asyncio
    import random

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel, ConversationRole

    _login(client)

    async def _seed() -> tuple[int, int]:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Pin")
            s.add(u)
            await s.flush()
            m = Message(user_id=u.id, role=ConversationRole.USER, content="закрепи меня")
            s.add(m)
            await s.commit()
            return u.id, m.id

    uid, mid = asyncio.run(_seed())

    async def _pinned() -> bool:
        async with AsyncSessionLocal() as s:
            return (await s.get(Message, mid)).pinned

    assert client.post(f"/admin/dialogs/{uid}/message/{mid}/pin").status_code == 200
    assert asyncio.run(_pinned()) is True
    client.post(f"/admin/dialogs/{uid}/message/{mid}/pin")  # toggle off
    assert asyncio.run(_pinned()) is False

    async def _unread() -> bool:
        async with AsyncSessionLocal() as s:
            return (await s.get(User, uid)).unread

    client.post(f"/admin/dialogs/{uid}/unread", follow_redirects=False)
    assert asyncio.run(_unread()) is True
    client.get(f"/admin/dialogs?u={uid}")  # opening the chat clears unread
    assert asyncio.run(_unread()) is False


def test_react_message(client):
    """Reacting sets Message.reaction; tapping the same emoji again clears it."""
    import asyncio
    import random

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel, ConversationRole

    _login(client)

    async def _seed() -> tuple[int, int]:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Re")
            s.add(u)
            await s.flush()
            m = Message(user_id=u.id, role=ConversationRole.USER, content="реакция")
            s.add(m)
            await s.commit()
            return u.id, m.id

    uid, mid = asyncio.run(_seed())

    async def _reaction() -> str:
        async with AsyncSessionLocal() as s:
            return (await s.get(Message, mid)).reaction or ""

    client.post(f"/admin/dialogs/{uid}/message/{mid}/react", data={"emoji": "🔥"})
    assert asyncio.run(_reaction()) == "🔥"
    client.post(f"/admin/dialogs/{uid}/message/{mid}/react", data={"emoji": "🔥"})  # toggle off
    assert asyncio.run(_reaction()) == ""


def test_forward_message(client):
    """Forwarding records an operator turn (with the source text) in every target chat."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel, ConversationRole

    _login(client)

    async def _seed() -> tuple[int, int, int]:
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)

            def _u(i: int, name: str) -> User:
                return User(
                    channel=Channel.TELEGRAM,
                    external_id=str(n + i),
                    telegram_id=n + i,
                    first_name=name,
                )

            src, t1, t2 = _u(0, "Src"), _u(1, "T1"), _u(2, "T2")
            s.add_all([src, t1, t2])
            await s.flush()
            m = Message(user_id=src.id, role=ConversationRole.USER, content="перешли меня")
            s.add(m)
            await s.commit()
            return m.id, t1.id, t2.id

    mid, t1, t2 = asyncio.run(_seed())
    r = client.post(
        "/admin/dialogs/forward", data={"message_id": str(mid), "targets": [str(t1), str(t2)]}
    )
    # No Telegram bot is configured in tests (BOT_TOKEN=""), so the relay genuinely
    # can't deliver — the endpoint must say so rather than falsely claiming success.
    assert r.status_code == 200 and "Не удалось переслать" in r.text

    async def _msgs(tid: int):
        async with AsyncSessionLocal() as s:
            return (
                (await s.execute(select(Message).where(Message.user_id == tid))).scalars().all()
            )

    for tid in (t1, t2):
        msgs = asyncio.run(_msgs(tid))
        # The turn is still recorded (so history/KB stay consistent) but marked
        # "failed" — never silently repainted as delivered.
        assert any("перешли меня" in m.content and m.status == "failed" for m in msgs)


def test_pwa_assets_served(client):
    """Service worker (root scope) + manifest are reachable for the PWA/notifier."""
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers.get("content-type", "")
    assert client.get("/static/manifest.webmanifest").status_code == 200


def test_push_key_and_subscribe(client):
    """VAPID public key is served + a push subscription is accepted/stored."""
    import random

    _login(client)
    r = client.get("/admin/push/key")
    assert r.status_code == 200
    key = r.json()["public_key"]
    assert key and len(key) > 20  # base64url VAPID public key

    sub = {
        "endpoint": "https://example.com/ep/" + str(random.randint(1, 10**9)),
        "keys": {"p256dh": "abc", "auth": "def"},
    }
    r2 = client.post("/admin/push/subscribe", json=sub)
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_dialogs_ping_counts_new_inbound(client):
    """The notifier poll reports newest id + count of new inbound (client) messages."""
    import asyncio
    import random

    from app.db.base import AsyncSessionLocal
    from app.db.models import Message, User
    from app.db.models.enums import Channel, ConversationRole

    _login(client)
    base_max = client.get("/admin/dialogs/ping").json()["max_id"]

    async def _add():
        async with AsyncSessionLocal() as s:
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Png")
            s.add(u)
            await s.flush()
            s.add(Message(user_id=u.id, role=ConversationRole.USER, content="новое входящее"))
            await s.commit()

    asyncio.run(_add())

    d = client.get(f"/admin/dialogs/ping?since={base_max}").json()
    assert d["max_id"] > base_max
    assert d["new"] >= 1


def test_create_product_via_admin(client):
    _login(client)
    r = client.post(
        "/admin/products/new",
        data={
            "sku": "smoke1",
            "title": "Смоук-товар",
            "description": "описание",
            "price_kzt": "2500",
            "fulfillment_type": "text_link",
            "subscription_days": "",
            "sort_order": "0",
            "is_active": "on",
            "ff_text": "Доступ",
            "ff_link": "https://example.com",
            "ff_chat_id": "",
            "ff_member_limit": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get("/admin/products")
    assert "Смоук-товар" in r.text


def _product_form(**over):
    base = {
        "sku": "",
        "title": "Товар",
        "description": "",
        "price_kzt": "1000",
        "fulfillment_type": "text_link",
        "subscription_days": "",
        "sort_order": "0",
        "is_active": "on",
        "ff_text": "Доступ",
        "ff_link": "",
        "ff_chat_id": "",
        "ff_member_limit": "1",
    }
    base.update(over)
    return base


def test_duplicate_sku_does_not_500(client):
    """A duplicate SKU must NOT crash — it gets auto-uniquified (303, not 500)."""
    _login(client)
    r1 = client.post(
        "/admin/products/new", data=_product_form(sku="dupe", title="Первый"),
        follow_redirects=False,
    )
    assert r1.status_code == 303
    r2 = client.post(
        "/admin/products/new", data=_product_form(sku="dupe", title="Второй"),
        follow_redirects=False,
    )
    assert r2.status_code == 303  # was 500 before the fix
    assert "Второй" in client.get("/admin/products").text


def test_blank_sku_autogenerates(client):
    """An empty SKU is allowed and auto-generated."""
    _login(client)
    r = client.post(
        "/admin/products/new", data=_product_form(sku="", title="Без артикула"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Без артикула" in client.get("/admin/products").text


def test_whatsapp_embedded_exchange(client, monkeypatch):
    """Embedded Signup: code → permanent token, stored into the WhatsApp config."""
    import asyncio

    import app.admin.routes.channels as ch
    from app.db.base import AsyncSessionLocal
    from app.services.settings_service import get_setting, set_setting

    _login(client)

    async def _seed():
        async with AsyncSessionLocal() as s:
            meta = await get_setting(s, "meta") or {}
            meta["app_id"] = "111222"
            meta["app_secret"] = "shh"
            await set_setting(s, "meta", meta)
            await s.commit()

    asyncio.run(_seed())

    class _Resp:
        def json(self):
            return {"access_token": "TKN-PERMANENT"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(ch.httpx, "AsyncClient", _FakeClient)

    r = client.post(
        "/admin/channels/whatsapp/exchange",
        json={"code": "abc", "phone_number_id": "PN1", "waba_id": "WB1"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    async def _fetch():
        async with AsyncSessionLocal() as s:
            return await get_setting(s, "meta")

    meta = asyncio.run(_fetch())
    assert meta["whatsapp"]["access_token"] == "TKN-PERMANENT"
    assert meta["whatsapp"]["phone_number_id"] == "PN1"
    assert meta["whatsapp"]["enabled"] is True


def test_start_keyboard_has_more_button():
    """/start greeting carries the «Хочу узнать подробнее» inline CTA."""
    from app.bot.keyboards import start_kb

    buttons = [b for row in start_kb().inline_keyboard for b in row]
    assert any(b.callback_data == "start:more" for b in buttons)


def test_delete_product_with_orders_hides_it(client):
    """A product referenced by an order is hidden (soft-deleted), never a 500."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Product, User
    from app.db.models.enums import Channel
    from app.services import orders as orders_svc

    _login(client)
    sku = f"delme{random.randint(1, 10**9)}"
    client.post(
        "/admin/products/new", data=_product_form(sku=sku, title="С заказом"),
        follow_redirects=False,
    )

    async def _make_order() -> int:
        async with AsyncSessionLocal() as s:
            p = (await s.execute(select(Product).where(Product.sku == sku))).scalars().first()
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="T")
            s.add(u)
            await s.flush()
            await orders_svc.create_order(s, u, p)
            await s.commit()
            return p.id

    pid = asyncio.run(_make_order())

    r = client.post(f"/admin/products/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303  # not 500

    async def _fetch():
        async with AsyncSessionLocal() as s:
            return await s.get(Product, pid)

    product = asyncio.run(_fetch())
    assert product is not None  # not hard-deleted (order still references it)
    assert product.is_active is False  # hidden instead


def test_delete_order(client):
    """Deleting an order removes it (and dependents) — no FK 500."""
    import asyncio
    import random

    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.db.models import Order, Product, User
    from app.db.models.enums import Channel
    from app.services import orders as orders_svc

    _login(client)
    sku = f"ord{random.randint(1, 10**9)}"
    client.post(
        "/admin/products/new", data=_product_form(sku=sku, title="Товар-заказ"),
        follow_redirects=False,
    )

    async def _make_order() -> int:
        async with AsyncSessionLocal() as s:
            p = (await s.execute(select(Product).where(Product.sku == sku))).scalars().first()
            n = random.randint(1, 10**9)
            u = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="T")
            s.add(u)
            await s.flush()
            o = await orders_svc.create_order(s, u, p)
            await s.commit()
            return o.id

    oid = asyncio.run(_make_order())

    r = client.post(f"/admin/orders/{oid}/delete", follow_redirects=False)
    assert r.status_code == 303

    async def _fetch():
        async with AsyncSessionLocal() as s:
            return await s.get(Order, oid)

    assert asyncio.run(_fetch()) is None  # gone
