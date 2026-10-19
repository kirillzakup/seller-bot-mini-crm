"""Meta adapter unit tests: signature verification + webhook parsing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import app.channels.meta as meta_mod
from app.channels.meta import (
    MetaMessenger,
    ig_signature_optional,
    parse_webhook,
    verify_signature,
    webhook_secret,
)
from app.db.models.enums import Channel


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


def test_whatsapp_send_voice_uploads_and_sends(monkeypatch):
    """WhatsApp voice reply: upload to /media, then send type=audio → True."""
    calls: list[str] = []
    sent_body: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, files=None, json=None, headers=None):
            calls.append(url)
            if url.endswith("/media"):
                return _Resp(200, {"id": "MEDIA_X"})  # upload → media id
            sent_body.update(json or {})  # capture the /messages payload
            return _Resp(200, {"messages": [{"id": "wamid.SENT"}]})  # send → message id

    monkeypatch.setattr(meta_mod.httpx, "AsyncClient", _FakeClient)

    wa = MetaMessenger(Channel.WHATSAPP, token="t", sender_id="PN1", graph_version="v21.0")
    ok = asyncio.run(wa.send_voice("770", b"audio", mime="audio/ogg", filename="reply.ogg"))
    assert ok is True
    assert any(u.endswith("/media") for u in calls)  # uploaded first
    assert any(u.endswith("/messages") for u in calls)  # then sent
    # Must go out as a PTT voice note (voice=True), else clients show "audio unavailable".
    assert sent_body["type"] == "audio"
    assert sent_body["audio"] == {"id": "MEDIA_X", "voice": True}

    # Instagram has no voice-NOTE upload → send_voice returns False (it speaks via
    # send_audio_by_url instead — see the next test).
    ig = MetaMessenger(Channel.INSTAGRAM, token="t", sender_id="IG1", graph_version="v21.0")
    assert asyncio.run(ig.send_voice("IG1", b"x", mime="audio/ogg", filename="r.ogg")) is False


def test_instagram_send_audio_by_url(monkeypatch):
    """IG can't upload voice-note bytes, but sends an audio attachment BY URL so the bot
    can speak on Instagram. WhatsApp returns False here (it uses the send_voice upload)."""
    posted: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["body"] = json
            return _Resp(200, {"message_id": "ig.AUDIO"})

    monkeypatch.setattr(meta_mod.httpx, "AsyncClient", _FakeClient)
    ig = MetaMessenger(Channel.INSTAGRAM, token="t", sender_id="IG1", graph_version="v21.0")
    ok = asyncio.run(ig.send_audio_by_url("IGSID", "https://x/media/reply.m4a", mime="audio/mp4"))
    assert ok is True
    att = posted["body"]["message"]["attachment"]
    assert att["type"] == "audio" and att["payload"]["url"].endswith("reply.m4a")

    wa = MetaMessenger(Channel.WHATSAPP, token="t", sender_id="PN1", graph_version="v21.0")
    assert asyncio.run(wa.send_audio_by_url("770", "https://x/a.m4a", mime="audio/mp4")) is False


def test_verify_signature_accepts_valid():
    secret = "s3cr3t"
    body = b'{"object":"whatsapp_business_account"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig) is True


def test_verify_signature_rejects_invalid_or_unconfigured():
    assert verify_signature("secret", b"body", "sha256=deadbeef") is False
    assert verify_signature("", b"body", "sha256=whatever") is False  # fails closed
    assert verify_signature("secret", b"body", None) is False
    assert verify_signature("secret", b"body", "md5=abc") is False


def test_parse_whatsapp_text():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "77001234567", "profile": {"name": "Аскар"}}],
                            "messages": [
                                {"from": "77001234567", "type": "text", "text": {"body": "привет"}}
                            ],
                        }
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(payload)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.channel == Channel.WHATSAPP
    assert m.external_id == "77001234567"
    assert m.text == "привет"
    assert m.name == "Аскар"


def test_parse_whatsapp_image_is_attachment():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "770", "type": "image", "image": {"id": "MEDIA1"}}
                            ]
                        }
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(payload)
    assert msgs[0].has_image
    assert msgs[0].attachments[0]["file_id"] == "MEDIA1"


def test_parse_instagram_text():
    payload = {
        "object": "instagram",
        "entry": [
            {"messaging": [{"sender": {"id": "IGID1"}, "message": {"text": "сколько стоит?"}}]}
        ],
    }
    msgs = parse_webhook(payload)
    assert len(msgs) == 1
    assert msgs[0].channel == Channel.INSTAGRAM
    assert msgs[0].external_id == "IGID1"
    assert msgs[0].text == "сколько стоит?"


def test_parse_instagram_echo_without_recipient_is_skipped():
    """An echo we can't attribute to a client (no recipient) is dropped, not mis-saved.
    Echoes WITH a recipient are captured — see test_parse_instagram_echo_reaction_story."""
    payload = {
        "object": "instagram",
        "entry": [
            {"messaging": [{"sender": {"id": "X"}, "message": {"text": "hi", "is_echo": True}}]}
        ],
    }
    assert parse_webhook(payload) == []


def test_ig_messages_url_uses_instagram_host():
    """The new Instagram API sends via graph.instagram.com/…/me — NOT facebook.

    Regression: the IGAA… token 400s on graph.facebook.com ('Cannot parse access
    token'), so every IG reply silently failed until the host was split per channel."""
    ig = MetaMessenger(Channel.INSTAGRAM, token="t", sender_id="1784140977", graph_version="v21.0")
    assert ig._messages_url() == "https://graph.instagram.com/v21.0/me/messages"


def test_wa_messages_url_uses_facebook_host():
    wa = MetaMessenger(Channel.WHATSAPP, token="t", sender_id="PN1", graph_version="v21.0")
    assert wa._messages_url() == "https://graph.facebook.com/v21.0/PN1/messages"


def test_ig_send_text_posts_to_instagram_host(monkeypatch):
    """End-to-end of the routing: IG send_text actually POSTs to graph.instagram.com."""
    seen: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, files=None, json=None, headers=None):
            seen.append(url)
            return _Resp(200, {})

    monkeypatch.setattr(meta_mod.httpx, "AsyncClient", _FakeClient)
    ig = MetaMessenger(Channel.INSTAGRAM, token="t", sender_id="1784140977", graph_version="v21.0")
    asyncio.run(ig.send_text("IGSID_123", "привет"))
    assert seen == ["https://graph.instagram.com/v21.0/me/messages"]


def test_webhook_secret_prefers_instagram_secret():
    """IG webhooks are signed with the Instagram App Secret; WA with the Meta one."""
    cfg = {"app_secret": "meta_sec", "instagram": {"app_secret": "ig_sec"}}
    assert webhook_secret(cfg, "instagram") == "ig_sec"
    assert webhook_secret(cfg, "whatsapp_business_account") == "meta_sec"


def test_webhook_secret_falls_back_to_meta_secret():
    # IG secret not set yet → fall back to the Meta app secret; None object → Meta.
    assert webhook_secret({"app_secret": "meta_sec"}, "instagram") == "meta_sec"
    assert webhook_secret({"app_secret": "meta_sec"}, None) == "meta_sec"


def test_ig_signature_optional_only_without_secret():
    """IG signature verification is skippable ONLY until the Instagram App Secret is
    set — once set, it returns False so the router enforces the HMAC again."""
    assert ig_signature_optional({"app_secret": "meta"}) is True  # no instagram cfg at all
    assert ig_signature_optional({"instagram": {}}) is True  # instagram cfg, no secret
    assert ig_signature_optional({"instagram": {"app_secret": "  "}}) is True  # blank secret
    assert ig_signature_optional({"instagram": {"app_secret": "igsec"}}) is False  # set → strict


def test_parse_comments_extracts_top_level_comment():
    from app.channels.meta import parse_comments

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "17841400000000001",
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "COMMENT1",
                            "text": "Сколько стоит курс?",
                            "from": {"id": "IGSID9", "username": "ivan"},
                            "media": {"id": "MEDIA1"},
                        },
                    }
                ],
            }
        ],
    }
    comments = parse_comments(payload)
    assert len(comments) == 1
    c = comments[0]
    assert c.comment_id == "COMMENT1"
    assert c.text == "Сколько стоит курс?"
    assert c.from_username == "ivan"
    assert c.media_id == "MEDIA1"
    assert c.is_reply is False


def test_parse_comments_ignores_dms_and_other_objects():
    from app.channels.meta import parse_comments

    dm = {
        "object": "instagram",
        "entry": [{"messaging": [{"sender": {"id": "X"}, "message": {"text": "hi"}}]}],
    }
    assert parse_comments(dm) == []
    assert parse_comments({"object": "whatsapp_business_account", "entry": []}) == []


def test_dispatch_comment_loop_guards(monkeypatch):
    """Acts on a stranger's top-level comment, but never on its own comment or on a
    reply-comment (so the bot can't answer its own replies forever)."""
    from app.channels import dispatch
    from app.channels.base import IncomingComment
    from app.db.base import AsyncSessionLocal
    from app.services.settings_service import set_setting

    acted = {"reply": 0, "dm": 0, "hide": 0}
    captured = {"caption": "unset"}

    class _FakeMsgr:
        async def reply_comment(self, cid, text):
            acted["reply"] += 1
            return True

        async def private_reply(self, cid, text):
            acted["dm"] += 1
            return True

        async def hide_comment(self, cid, hide=True):
            acted["hide"] += 1
            return True

        async def fetch_media_caption(self, media_id):
            return "Отрывок интервью с Галиной, ученицей нашего курса" if media_id else None

    async def fake_decide(session, text, post_caption=None, thread=None):
        captured["caption"] = post_caption
        return {"is_spam": False, "reply": "спасибо!", "dm": "напишите нам 🙂"}

    async def fake_get_messenger(session, channel):
        return _FakeMsgr()

    monkeypatch.setattr(dispatch.comments_svc, "decide", fake_decide)
    monkeypatch.setattr(dispatch, "get_messenger", fake_get_messenger)

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(
                s,
                "meta",
                {"instagram": {"ig_id": "17841400000000001", "username": "shop_owner"}},
            )
            await set_setting(
                s,
                "comments",
                {"enabled": True, "reply_enabled": True, "dm_enabled": True, "hide_spam": True},
            )
            await s.commit()

            own = IncomingComment(
                channel=Channel.INSTAGRAM, comment_id="C1", text="x", from_id="17841400000000001"
            )
            reply = IncomingComment(
                channel=Channel.INSTAGRAM, comment_id="C2", text="x",
                from_id="OTHER", parent_id="P1",
            )
            await dispatch.dispatch_comment(s, own)
            await dispatch.dispatch_comment(s, reply)
            assert acted == {"reply": 0, "dm": 0, "hide": 0}  # both skipped by loop guards

            real = IncomingComment(
                channel=Channel.INSTAGRAM,
                comment_id="C3",
                text="сколько стоит",
                from_id="STRANGER",
                from_username="someone",
                media_id="M1",
            )
            await dispatch.dispatch_comment(s, real)
            assert acted["reply"] == 1 and acted["dm"] == 1  # replied publicly + DM'd
            # the post's caption reached the decision → bot answers in context
            assert captured["caption"] == "Отрывок интервью с Галиной, ученицей нашего курса"

    asyncio.run(main())


def test_comment_decide_reads_the_whole_thread(monkeypatch):
    """decide() feeds the post caption AND every other comment to the model, so the bot
    answers in context and doesn't repeat what the thread already covered."""
    import app.sales.comments as comments
    from app.db.base import AsyncSessionLocal
    from app.services.settings_service import set_setting

    captured: dict = {}

    async def fake_cc(*, messages, tools, config):
        captured["user"] = messages[-1]["content"]
        return {"content": '{"is_spam": false, "reply": "ок", "dm": ""}'}

    monkeypatch.setattr(comments, "chat_completion", fake_cc)

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "llm", {"base_url": "http://x", "api_key": "k"})
            thread = [
                {"from": "aigerim", "text": "а есть рассрочка?"},
                {"from": "school", "text": "да, через Kaspi Red"},
            ]
            await comments.decide(
                s, "а сколько стоит?", post_caption="Пост про интенсив по нейросетям", thread=thread
            )
            await set_setting(s, "llm", {})

    asyncio.run(main())
    assert "а есть рассрочка?" in captured["user"]
    assert "да, через Kaspi Red" in captured["user"]
    assert "Пост про интенсив" in captured["user"]
    assert "а сколько стоит?" in captured["user"]


def test_echo_dedup_db_backstop_when_cache_misses():
    """When the in-memory cache misses (restart / eviction / race), the bot's own echo is
    still recognised from persisted history and NOT duplicated as a phantom 'operator'
    message — while a genuinely NEW business message (a human in the IG app) IS saved."""
    from sqlalchemy import select

    import app.channels.meta as meta
    from app.channels import dispatch
    from app.channels.base import IncomingMessage
    from app.db.base import AsyncSessionLocal
    from app.db.models.content import Message
    from app.db.models.enums import Channel
    from app.sales.conversation import save_message
    from app.services.users import get_or_create_contact

    reply = "Спасибо, номер сохранила ✅"

    async def main():
        async with AsyncSessionLocal() as s:
            user = await get_or_create_contact(s, channel=Channel.INSTAGRAM, external_id="ECHOCLI")
            await save_message(s, user.id, "assistant", reply)
            await s.commit()

            meta._recent_sent.clear()  # simulate a restart / cache miss
            own_echo = IncomingMessage(
                channel=Channel.INSTAGRAM, external_id="ECHOCLI", text=reply, is_echo=True
            )
            await dispatch.dispatch_incoming(s, own_echo)
            await s.commit()

            rows = (
                await s.execute(select(Message).where(Message.user_id == user.id))
            ).scalars().all()
            assert [str(m.role) for m in rows].count("operator") == 0
            assert sum(1 for m in rows if (m.content or "").strip() == reply) == 1

            human = IncomingMessage(
                channel=Channel.INSTAGRAM, external_id="ECHOCLI",
                text="Это менеджер, наберу вас через 5 минут", is_echo=True,
            )
            await dispatch.dispatch_incoming(s, human)
            await s.commit()
            ops = (
                await s.execute(
                    select(Message).where(Message.user_id == user.id, Message.role == "operator")
                )
            ).scalars().all()
            assert len(ops) == 1 and ops[0].content == "Это менеджер, наберу вас через 5 минут"

    asyncio.run(main())


def test_parse_instagram_echo_reaction_story():
    """Echoes, reactions and story replies are parsed distinctly from normal DMs."""
    echo = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "BIZ"},
                        "recipient": {"id": "CLIENT"},
                        "message": {"is_echo": True, "text": "Здравствуйте!"},
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(echo)
    assert len(msgs) == 1
    assert msgs[0].is_echo and msgs[0].external_id == "CLIENT"  # keyed by the client
    assert msgs[0].text == "Здравствуйте!"

    react = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "CLIENT"},
                        "recipient": {"id": "BIZ"},
                        "reaction": {"action": "react", "emoji": "❤️"},
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(react)
    assert len(msgs) == 1 and msgs[0].reaction == "❤️" and msgs[0].external_id == "CLIENT"

    unreact = {
        "object": "instagram",
        "entry": [{"messaging": [{"sender": {"id": "C"}, "reaction": {"action": "unreact"}}]}],
    }
    assert parse_webhook(unreact) == []  # un-reacts are ignored

    story = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "CLIENT"},
                        "recipient": {"id": "BIZ"},
                        "message": {"text": "а цена?", "reply_to": {"story": {"id": "S1"}}},
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(story)
    assert len(msgs) == 1 and msgs[0].story_reply and msgs[0].text == "а цена?"

    mention = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "CLIENT"},
                        "recipient": {"id": "BIZ"},
                        "message": {"attachments": [{"type": "story_mention", "payload": {}}]},
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(mention)
    assert len(msgs) == 1 and msgs[0].story_reply


def test_echo_dedup_recognises_own_sends():
    """A bot send is remembered so its echo webhook isn't mistaken for a human reply."""
    from app.channels.meta import note_sent, was_bot_sent

    note_sent("CLIENT", "Здравствуйте!")
    assert was_bot_sent("CLIENT", "Здравствуйте!")
    assert was_bot_sent("CLIENT", "  Здравствуйте!  ")  # trimmed match
    assert not was_bot_sent("CLIENT", "другой текст")
    assert not was_bot_sent("OTHER", "Здравствуйте!")  # different recipient


def test_receipt_photo_downloads_whatsapp_and_instagram(monkeypatch):
    """The receipt image is fetched via the channel messenger — WA media id AND IG URL."""
    from aiogram.types import BufferedInputFile

    import app.channels.registry as registry
    from app.db.models.enums import Channel
    from app.services.notify import _receipt_photo

    seen = {}

    class _Msgr:
        async def download_media(self, ref):
            seen["ref"] = ref
            return b"\xff\xd8jpegbytes"

    async def fake_get_messenger(session, channel):
        return _Msgr()

    monkeypatch.setattr(registry, "get_messenger", fake_get_messenger)

    class _WA:
        channel = Channel.WHATSAPP

    class _IG:
        channel = Channel.INSTAGRAM

    async def main():
        wa = await _receipt_photo(None, _WA(), "WA_MEDIA_1")  # media id → file_id ref
        assert isinstance(wa, BufferedInputFile) and seen["ref"] == {"file_id": "WA_MEDIA_1"}
        ig = await _receipt_photo(None, _IG(), "https://cdn/x.jpg")  # URL → url ref
        assert isinstance(ig, BufferedInputFile) and seen["ref"] == {"url": "https://cdn/x.jpg"}

    asyncio.run(main())


def test_analyze_receipt_classifies_and_reads_amount(monkeypatch):
    """analyze_receipt parses the model's JSON into is_receipt + amount, handles ```-fenced
    JSON and a bare да/нет fallback, degrades to (None, None) on missing bytes / LLM error,
    and sends the image as an image_url part."""
    import app.sales.vision as vision
    from app.db.base import AsyncSessionLocal
    from app.services.settings_service import set_setting

    box = {"answer": "", "raise": False}

    async def fake_cc(*, messages, tools, config):
        box["messages"] = messages
        if box["raise"]:
            from app.sales.llm import LLMError

            raise LLMError("boom")
        return {"content": box["answer"]}

    monkeypatch.setattr(vision, "chat_completion", fake_cc)

    async def main():
        async with AsyncSessionLocal() as s:
            await set_setting(s, "llm", {"base_url": "http://x", "api_key": "k", "model": "m"})
            await s.commit()
            box["answer"] = '{"is_receipt": true, "amount": 50000}'
            yes = await vision.analyze_receipt(s, b"imgbytes")
            box["answer"] = '```json\n{"is_receipt": false, "amount": null}\n```'
            no = await vision.analyze_receipt(s, b"imgbytes")
            box["answer"] = "да"  # model ignored JSON → fallback
            fb = await vision.analyze_receipt(s, b"imgbytes")
            empty = await vision.analyze_receipt(s, b"")
            box["raise"] = True
            err = await vision.analyze_receipt(s, b"imgbytes")
            return yes, no, fb, empty, err, box["messages"]

    yes, no, fb, empty, err, messages = asyncio.run(main())
    assert yes.is_receipt is True and yes.amount == 50000
    assert no.is_receipt is False and no.amount is None  # ```-fenced JSON parsed
    assert fb.is_receipt is True and fb.amount is None  # да → True, no amount
    assert empty.is_receipt is None
    assert err.is_receipt is None  # LLM error → degrade safely
    img = next(p for p in messages[-1]["content"] if p.get("type") == "image_url")
    assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_receipt_image_rejected_when_not_a_receipt(monkeypatch):
    """A non-receipt photo during a pending order is NOT accepted: the order stays awaiting
    payment (not «на проверке») and the client is asked to send a real receipt."""
    import random as _r

    import app.channels.dispatch as disp
    from app.channels.base import IncomingMessage
    from app.db.base import AsyncSessionLocal
    from app.db.models import Order, Product, User
    from app.db.models.enums import Channel, FulfillmentType, OrderStatus
    from app.services import orders as orders_svc

    class _Msgr:
        channel = Channel.WHATSAPP

        def __init__(self):
            self.sent: list[str] = []

        async def download_media(self, ref):
            return b"selfiebytes"

        async def download_audio(self, ref):
            return None

        async def send_text(self, ext, text):
            self.sent.append(text)

        async def send_typing(self, ext, *, message_id=None):
            pass

        async def fetch_profile(self, ext):
            return None

    from app.sales.vision import ReceiptAnalysis

    fake = _Msgr()

    async def _noop(*a, **k):
        return None

    async def _get_msgr(s, c):
        return fake

    async def _not_receipt(session, image_bytes, mime="image/jpeg"):
        return ReceiptAnalysis(is_receipt=False, amount=None)

    monkeypatch.setattr(disp, "get_messenger", _get_msgr)
    monkeypatch.setattr(disp.vision, "analyze_receipt", _not_receipt)
    monkeypatch.setattr(disp, "notify_manual_receipt", _noop)
    monkeypatch.setattr(disp, "_incoming_photo", _noop)
    monkeypatch.setattr(disp.inbox_svc, "mirror_incoming", _noop)

    async def main():
        async with AsyncSessionLocal() as s:
            ext = str(_r.randint(1, 10**9))
            u = User(channel=Channel.WHATSAPP, external_id=ext)
            p = Product(
                sku=f"s{ext}", title="Товар", price_kzt=1000,
                fulfillment_type=FulfillmentType.TEXT_LINK, fulfillment_config={"text": "ok"},
            )
            s.add_all([u, p])
            await s.flush()
            order = await orders_svc.create_order(s, u, p)
            await s.commit()
            oid = order.id
            msg = IncomingMessage(
                channel=Channel.WHATSAPP, external_id=ext,
                attachments=[{"kind": "image", "url": "https://x/pic.jpg"}],
            )
            await disp.dispatch_incoming(s, msg)
            await s.commit()
            return (await s.get(Order, oid)).status, fake.sent

    status, sent = asyncio.run(main())
    assert status == OrderStatus.AWAITING_PAYMENT  # NOT moved to «на проверке»
    assert any("не похоже на чек" in t for t in sent)


def test_notify_receipt_flags_amount_mismatch(monkeypatch):
    """The operator note shows ✅ when the receipt amount matches the order, and a ⚠️ when
    it doesn't — so a wrong / partial / fake receipt is caught before «Подтвердить»."""
    import random as _r

    import app.bot.runner as runner
    from app.db.base import AsyncSessionLocal
    from app.db.models import Product, User
    from app.db.models.enums import Channel, FulfillmentType
    from app.services import notify as notify_mod
    from app.services import orders as orders_svc
    from app.services.settings_service import set_setting

    notes: list[str] = []

    class _Bot:
        async def send_message(self, cid, text=None, **kw):
            notes.append(text)

        async def send_photo(self, cid, photo=None, caption=None, **kw):
            notes.append(caption)

    monkeypatch.setattr(runner, "get_bot", lambda: _Bot())

    async def main():
        async with AsyncSessionLocal() as s:
            ext = str(_r.randint(1, 10**9))
            u = User(channel=Channel.WHATSAPP, external_id=ext)
            p = Product(
                sku=f"s{ext}", title="Товар", price_kzt=50000,
                fulfillment_type=FulfillmentType.TEXT_LINK, fulfillment_config={"text": "ok"},
            )
            s.add_all([u, p])
            await s.flush()
            order = await orders_svc.create_order(s, u, p)
            await set_setting(s, "admin_chat_id", 111)  # an operator chat to notify
            await s.commit()
            notes.clear()
            await notify_mod.notify_manual_receipt(s, order, None, receipt_amount=50000)
            await notify_mod.notify_manual_receipt(s, order, None, receipt_amount=40000)
            return list(notes)

    out = asyncio.run(main())
    assert any("✅ совпадает" in n for n in out)  # exact match flagged OK
    assert any("НЕ совпадает" in n for n in out)  # mismatch flagged ⚠️


def test_parse_receipts_whatsapp_and_instagram():
    """WhatsApp per-message statuses and Instagram read events become receipt dicts."""
    from app.channels.meta import parse_receipts

    wa = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"id": "wamid.1", "status": "delivered"},
                                {"id": "wamid.2", "status": "read"},
                                {
                                    "id": "wamid.3",
                                    "status": "failed",
                                    "errors": [{"code": 131047, "title": "24h window"}],
                                },
                            ]
                        }
                    }
                ]
            }
        ],
    }
    recs = parse_receipts(wa)
    assert {"mid": "wamid.1", "status": "delivered"} in recs
    assert {"mid": "wamid.2", "status": "read"} in recs
    assert {"mid": "wamid.3", "status": "failed"} in recs  # undelivered must surface too

    ig = {
        "object": "instagram",
        "entry": [
            {"messaging": [{"sender": {"id": "CLIENT"}, "read": {"mid": "m1"}}]}
        ],
    }
    assert parse_receipts(ig) == [{"read_by": "CLIENT"}]

    normal = {
        "object": "instagram",
        "entry": [{"messaging": [{"sender": {"id": "C"}, "message": {"text": "hi"}}]}],
    }
    assert parse_receipts(normal) == []  # a normal message is not a receipt


def test_sent_message_id_extracts_platform_id():
    """The outbound message id is pulled from IG (message_id) and WA (messages[0].id)."""
    from app.channels.meta import _sent_message_id

    class _R:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    assert _sent_message_id(_R({"message_id": "aWd_IG"})) == "aWd_IG"  # Instagram
    assert _sent_message_id(_R({"messages": [{"id": "wamid.X"}]})) == "wamid.X"  # WhatsApp
    assert _sent_message_id(_R({})) is None


def test_save_operator_file_types_and_limits():
    """Operator uploads: allowed types classified by kind, junk/oversize rejected."""
    import app.services.media as media

    assert media.save_operator_file(b"x", "application/x-evil", "a.exe") is None  # blocked
    too_big = b"x" * (media.operator_max_bytes() + 1)
    assert media.save_operator_file(too_big, "image/png", "a.png") is None

    cases = [("image/png", "p.png", "photo"), ("video/mp4", "c.mp4", "video"),
             ("audio/ogg", "v.ogg", "voice"), ("application/pdf", "f.pdf", "document")]
    for ctype, name, kind in cases:
        saved = media.save_operator_file(b"\x00\x01", ctype, name)
        assert saved is not None and saved["kind"] == kind
        assert "/media/" in saved["url"]
        (media.MEDIA_DIR / saved["filename"]).unlink(missing_ok=True)  # cleanup

    # unknown content-type but a known extension is still accepted (browser omitted it)
    byext = media.save_operator_file(b"\x00", "", "clip.mp4")
    assert byext is not None and byext["kind"] == "video"
    (media.MEDIA_DIR / byext["filename"]).unlink(missing_ok=True)


def test_telegram_send_media_routes_by_kind():
    """Telegram picks send_photo/video/voice/audio/document by kind + mime, returns id."""
    from app.channels.telegram import TelegramMessenger

    calls = []

    class _Msg:
        message_id = 42

    class _Bot:
        async def send_photo(self, cid, url, caption=None):
            calls.append(("photo", url))
            return _Msg()

        async def send_video(self, cid, url, caption=None):
            calls.append(("video", url))
            return _Msg()

        async def send_voice(self, cid, url, caption=None):
            calls.append(("voice", url))
            return _Msg()

        async def send_audio(self, cid, url, caption=None):
            calls.append(("audio", url))
            return _Msg()

        async def send_document(self, cid, url, caption=None):
            calls.append(("document", url))
            return _Msg()

    tg = TelegramMessenger(_Bot())

    async def main():
        await tg.send_media("1", url="u1", mime="image/png", kind="photo")
        await tg.send_media("1", url="u2", mime="video/mp4", kind="video")
        await tg.send_media("1", url="u3", mime="audio/ogg", kind="voice")  # ogg → voice bubble
        await tg.send_media("1", url="u4", mime="audio/mpeg", kind="voice")  # mp3 → audio
        return await tg.send_media("1", url="u5", mime="application/pdf", kind="document")

    assert asyncio.run(main()) == "42"
    assert calls == [
        ("photo", "u1"), ("video", "u2"), ("voice", "u3"), ("audio", "u4"), ("document", "u5"),
    ]


def test_meta_send_media_payloads(monkeypatch):
    """WhatsApp uses a media link (with caption); Instagram an attachment; IG docs fall back."""
    posts = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"message_id": "mid1"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            posts.append(json)
            return _Resp()

    monkeypatch.setattr(meta_mod.httpx, "AsyncClient", _FakeClient)
    wa = MetaMessenger(Channel.WHATSAPP, token="t", sender_id="PN", graph_version="v21.0")
    ig = MetaMessenger(Channel.INSTAGRAM, token="t", sender_id="IG", graph_version="v21.0")

    async def main():
        await wa.send_media(
            "770", url="http://x/a.mp4", mime="video/mp4", kind="video", caption="гляди"
        )
        await ig.send_media("IGSID", url="http://x/a.jpg", mime="image/jpeg", kind="photo")
        await ig.send_media(
            "IGSID", url="http://x/a.pdf", mime="application/pdf", kind="document"
        )

    asyncio.run(main())
    assert posts[0]["type"] == "video" and posts[0]["video"]["link"] == "http://x/a.mp4"
    assert posts[0]["video"]["caption"] == "гляди"  # WA inlines the caption
    assert posts[1]["message"]["attachment"]["type"] == "image"  # IG attachment
    assert posts[1]["message"]["attachment"]["payload"]["url"] == "http://x/a.jpg"
    assert posts[2]["message"]["text"] == "http://x/a.pdf"  # IG can't DM a file → link


def test_dispatch_comment_funnel_dms_keyword(monkeypatch):
    """A comment containing a funnel keyword → deterministic DM (skips the LLM path)."""
    from app.channels import dispatch
    from app.channels.base import IncomingComment
    from app.db.base import AsyncSessionLocal
    from app.db.models import CommentFunnel

    acted = {"dm": None, "public": None, "decide_called": False}

    class _FakeMsgr:
        async def private_reply(self, cid, text):
            acted["dm"] = text
            return True

        async def reply_comment(self, cid, text):
            acted["public"] = text
            return True

        async def hide_comment(self, cid, hide=True):
            return True

        async def fetch_media_caption(self, media_id):
            return None

    async def fake_decide(session, text, post_caption=None, thread=None):
        acted["decide_called"] = True
        return {"is_spam": False, "reply": "x", "dm": "y"}

    async def fake_get_messenger(session, channel):
        return _FakeMsgr()

    monkeypatch.setattr(dispatch, "get_messenger", fake_get_messenger)
    monkeypatch.setattr(dispatch.comments_svc, "decide", fake_decide)

    async def main():
        async with AsyncSessionLocal() as s:
            f = CommentFunnel(
                keyword="хочу", reply_type="link",
                reply_value="Держи 👉 https://x.kz", public_reply="В личке! 📩", enabled=True,
            )
            s.add(f)
            await s.commit()
            fid = f.id
            c = IncomingComment(
                channel=Channel.INSTAGRAM, comment_id="F1", text="да, я ХОЧУ это!",
                from_id="STRANGER", from_username="someone",
            )
            await dispatch.dispatch_comment(s, c)
            assert acted["dm"] == "Держи 👉 https://x.kz"  # DM sent
            assert acted["public"] == "В личке! 📩"  # public reply sent
            assert acted["decide_called"] is False  # funnel short-circuits the LLM
            assert f.triggered_count == 1  # incremented in-memory (caller commits in prod)
            await s.delete(await s.get(CommentFunnel, fid))  # cleanup
            await s.commit()

    asyncio.run(main())


def test_match_funnel_prefers_most_specific_keyword():
    """A «ХОЧУ БОТА» comment must trigger the «ХОЧУ БОТА» funnel, not the generic «ХОЧУ» one
    — even though the generic was created first (lower id) and «хочу» is a substring of it."""
    from app.db.base import AsyncSessionLocal
    from app.db.models import CommentFunnel
    from app.sales.comments import match_funnel

    async def main():
        async with AsyncSessionLocal() as s:
            generic = CommentFunnel(
                keyword="ХОЧУТЕСТ", reply_type="link", reply_value="g", enabled=True
            )
            s.add(generic)
            await s.commit()
            specific = CommentFunnel(
                keyword="ХОЧУТЕСТ БОТА", reply_type="link", reply_value="b", enabled=True
            )
            s.add(specific)
            await s.commit()
            gid, sid = generic.id, specific.id
            try:
                m1 = await match_funnel(s, "да, я ХОЧУТЕСТ БОТА обязательно!")
                assert m1 is not None and m1.id == sid  # specific keyword wins
                m2 = await match_funnel(s, "просто хочутест сейчас")
                assert m2 is not None and m2.id == gid  # generic still matches a plain «хочутест»
            finally:
                for fid in (gid, sid):
                    obj = await s.get(CommentFunnel, fid)
                    if obj is not None:
                        await s.delete(obj)
                await s.commit()

    asyncio.run(main())


def test_dispatch_comment_funnel_dms_image(monkeypatch):
    """A funnel with an attached image DMs the picture to the commenter after the text."""
    from app.channels import dispatch
    from app.channels.base import IncomingComment
    from app.db.base import AsyncSessionLocal
    from app.db.models import CommentFunnel, MediaAsset

    acted = {"dm": None, "image_url": None}

    class _FakeMsgr:
        async def private_reply(self, cid, text):
            acted["dm"] = text
            return True

        async def private_reply_image(self, cid, url):
            acted["image_url"] = url
            return True

        async def reply_comment(self, cid, text):
            return True

    async def fake_get_messenger(session, channel):
        return _FakeMsgr()

    monkeypatch.setattr(dispatch, "get_messenger", fake_get_messenger)

    async def main():
        async with AsyncSessionLocal() as s:
            asset = MediaAsset(title="Промо", filename="promo_funnel_test.jpg", is_active=True)
            s.add(asset)
            await s.flush()
            f = CommentFunnel(
                keyword="дай",
                reply_type="text",
                reply_value="Держите подарок!",
                image_id=asset.id,
                enabled=True,
            )
            s.add(f)
            await s.commit()
            fid, aid = f.id, asset.id
            c = IncomingComment(
                channel=Channel.INSTAGRAM,
                comment_id="F2",
                text="ДАЙ пожалуйста",
                from_id="STRANGER2",
                from_username="someone2",
            )
            await dispatch.dispatch_comment(s, c)
            assert acted["dm"] == "Держите подарок!"  # text DM went first
            assert acted["image_url"] and "promo_funnel_test.jpg" in acted["image_url"]
            await s.delete(await s.get(CommentFunnel, fid))
            await s.delete(await s.get(MediaAsset, aid))
            await s.commit()

    asyncio.run(main())
